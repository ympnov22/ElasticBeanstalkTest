# Copyright 2015 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

import os
import re
import time
from datetime import datetime, timedelta
import platform

from ebcli.core.fileoperations import _marker

from cement.utils.misc import minimal_logger
from cement.utils.shell import exec_cmd
from botocore.compat import six

from ebcli.operations import buildspecops
from ebcli.core import fileoperations, io
from ebcli.lib import aws, ec2, elasticbeanstalk, heuristics, iam, s3, utils, codecommit
from ebcli.lib.aws import InvalidParameterValueError
from ebcli.objects.exceptions import *
from ebcli.objects.sourcecontrol import SourceControl, NoSC
from ebcli.resources.strings import strings, responses, prompts
from ebcli.resources.statics import iam_documents, iam_attributes

LOG = minimal_logger(__name__)


def wait_for_success_events(request_id, timeout_in_minutes=None,
                            sleep_time=5, stream_events=True, can_abort=False,
                            streamer=None, app_name=None, env_name=None, version_label=None,
                            platform_arn=None, timeout_error_message=None):
    if timeout_in_minutes == 0:
        return
    if timeout_in_minutes is None:
        timeout_in_minutes = 10

    start = datetime.utcnow()
    timediff = timedelta(seconds=timeout_in_minutes * 60)

    # default to now, will update if request_id is provided
    last_time = start

    if streamer is None:
        streamer = io.get_event_streamer()

    if can_abort:
        streamer.prompt += strings['events.abortmessage']

    events = []

    # If the even stream is terminated before we finish streaming application version events we will not
    #   be able to continue the command so we must warn the user it is not safe to quit.
    safe_to_quit = True
    if version_label is not None and request_id is None:
        safe_to_quit = False

    try:
        # Get first event in order to get start time
        if request_id:
            while not events:
                events = elasticbeanstalk.get_new_events(
                    app_name,
                    env_name,
                    request_id,
                    last_event_time=None,
                    platform_arn=platform_arn,
                    version_label=version_label
                )

                if len(events) > 0:
                    event = events[-1]
                    app_name = event.app_name
                    env_name = event.environment_name

                    if stream_events:
                        streamer.stream_event(get_event_string(event, long_format=True), safe_to_quit=safe_to_quit)

                    _raise_if_error_event(event.message)
                    if _is_success_event(event.message):
                        return
                    last_time = event.event_date
                else:
                    _sleep(sleep_time)

        # Get remaining events
        while not _timeout_reached(start, timediff):
            _sleep(sleep_time)

            events = elasticbeanstalk.get_new_events(
                app_name,
                env_name,
                request_id,
                last_event_time=last_time,
                platform_arn=platform_arn,
                version_label=version_label
            )

            if events:
                events = filter_events(
                    events,
                    env_name=env_name,
                    request_id=request_id,
                    version_label=version_label
                )

            for event in reversed(events):
                if stream_events:
                    streamer.stream_event(get_event_string(event, long_format=True), safe_to_quit=safe_to_quit)
                    # We dont need to update last_time if we are not printing.
                    # This can solve timing issues
                    last_time = event.event_date

                _raise_if_error_event(event.message)
                if _is_success_event(event.message):
                    return
    finally:
        streamer.end_stream()
    # We have timed out

    if not timeout_error_message:
        timeout_error_message = strings['timeout.error'].format(timeout_in_minutes=timeout_in_minutes)

    raise TimeoutError(timeout_error_message)


def filter_events(events, version_label=None, request_id=None, env_name=None):
    """
    Method filters events by their version_label, request_id, or env_name if supplied,
    or any combination if multiple are specified.

    :param events: A list of `events` returned by the `DescribeEvents` API
    :param version_label: An optional `version_label` of the environment to filter by
    :param request_id: An optional `request_id` of the operation being waited on to filter by
    :param env_name: An optional `environment_name` of the environment that is being waited on
    :return: A new list of events filtered as per the rules above.
    """
    filtered_events = []

    for event in events:
        if version_label and event.version_label and (event.version_label != version_label):
            continue
        if request_id and event.request_id and (event.request_id != request_id):
            continue
        if env_name and event.environment_name and (event.environment_name != env_name):
            continue

        filtered_events.append(event)

    return filtered_events


def wait_for_compose_events(request_id, app_name, grouped_envs, timeout_in_minutes=None,
                            sleep_time=5, stream_events=True,
                            can_abort=False):
    if timeout_in_minutes == 0:
        return
    if timeout_in_minutes is None:
        timeout_in_minutes = 15

    start = datetime.utcnow()
    timediff = timedelta(seconds=timeout_in_minutes * 60)

    last_times = []
    events_matrix = []
    successes = []

    last_time_compose = datetime.utcnow()
    compose_events = []

    for i in range(len(grouped_envs)):
        # Like indices of last_times and events_matrix correspond
        # to the same environment
        last_times.append(datetime.utcnow())
        events_matrix.append([])
        successes.append(False)

    streamer = io.get_event_streamer()
    if can_abort:
        streamer.prompt += strings['events.abortmessage']

    try:
        # Poll for events from all environments
        while not _timeout_reached(start, timediff):
            # Check for success from all environments
            if all(successes):
                return

            # Poll for ComposeEnvironments events
            compose_events = elasticbeanstalk.get_new_events(app_name=app_name,
                                                             env_name=None,
                                                             request_id=request_id,
                                                             last_event_time=last_time_compose)
            for event in reversed(compose_events):
                if stream_events:
                    streamer.stream_event(get_compose_event_string(event))
                    last_time_compose = event.event_date

            for index in range(len(grouped_envs)):
                if successes[index]:
                    continue

                _sleep(sleep_time)

                events_matrix[index] = elasticbeanstalk.get_new_events(
                    app_name, grouped_envs[index], None,
                    last_event_time=last_times[index]
                )

                for event in reversed(events_matrix[index]):
                    if stream_events:
                        streamer.stream_event(get_env_event_string(event))
                        last_times[index] = event.event_date

                    if _is_success_event(event.message):
                        successes[index] = True
    finally:
        streamer.end_stream()

    # TODO: Must raise TimeoutError here
    io.log_error(strings['timeout.error'])


def _raise_if_error_event(message):
    if message == responses['event.redmessage']:
        raise ServiceError(message)
    if message == responses['event.failedlaunch']:
        raise ServiceError(message)
    if message == responses['event.faileddeploy']:
        raise ServiceError(message)
    if message == responses['event.failedupdate']:
        raise ServiceError(message)
    if message == responses['event.updatefailed']:
        raise ServiceError(message)
    if message.startswith(responses['event.launchbad']):
        raise ServiceError(message)
    if message.startswith(responses['event.updatebad']):
        raise ServiceError(message)
    if message.startswith(responses['logs.fail']):
        raise ServiceError(message)
    if message.startswith(responses['create.ecsdockerrun1']):
        raise NotSupportedError(prompts['create.dockerrunupgrade'])
    if message.startswith(responses['appversion.finished']) and message.endswith('FAILED.'):
        raise ServiceError(message)


def _is_success_event(message):
    if message == responses['logs.pulled']:
        return True
    if message == responses['env.terminated']:
        return True
    if message == responses['env.updatesuccess']:
        return True
    if message == responses['env.configsuccess']:
        return True
    if message == responses['app.deletesuccess']:
        return True
    if message == responses['event.greenmessage']:
        return True
    if responses['logs.successtail'] in message:
        return True
    if responses['logs.successbundle'] in message:
        return True
    if responses['tags.tag_update_successful'] in message:
        return True
    if responses['tags.no_tags_to_update'] in message:
        return True
    if message.startswith(responses['event.completewitherrors']):
        return True
    if message.startswith(responses['event.launched_environment']):
        return True
    if message.startswith(responses['event.platformdeletesuccess']):
        return True
    if message.startswith(responses['event.platformdeletefailed']):
        return True
    if message.startswith(responses['event.platformcreatefailed']):
        return True
    if message.startswith(responses['event.platform_ami_region_service_region_mismatch']):
        return True
    if message.startswith(responses['event.platformcreatesuccess']):
        return True
    if message.startswith(responses['event.launchsuccess']):
        return True
    if message.startswith(responses['swap.success']):
        return True
    if message.startswith(responses['appversion.finished']) and message.endswith('PROCESSED.'):
        return True
    return False


def get_event_string(event, long_format=False):
    message = event.message
    severity = event.severity
    date = event.event_date
    if long_format:
        return u'{0} {1} {2}'.format(
            date.strftime("%Y-%m-%d %H:%M:%S").ljust(22),
            severity.ljust(7),
            message)
    else:
        return u'{0}: {1}'.format(severity, message)


def get_compose_event_string(event, long_format=False):
    app_name = event.app_name
    message = event.message
    severity = event.severity
    date = event.event_date
    if long_format:
        return u'{0} - {1} {2} {3}'.format(
            app_name,
            date.strftime("%Y-%m-%d %H:%M:%S").ljust(22),
            severity.ljust(7),
            message
        )
    else:
        return u'{0} - {1}: {2}'.format(app_name, severity, message)


def get_env_event_string(event, long_format=False):
    environment = event.environment_name
    message = event.message
    severity = event.severity
    date = event.event_date
    if long_format:
        return u'{0} - {1} {2} {3}'.format(
            environment.rjust(40),

            date.strftime("%Y-%m-%d %H:%M:%S").ljust(22),
            severity.ljust(7),
            message)
    else:
        return u'{0} - {1}: {2}'.format(environment.rjust(40), severity, message)


def get_app_version_s3_location(app_name, version_label):
    s3_key, s3_bucket = None, None
    app_version = elasticbeanstalk.application_version_exists(app_name, version_label)

    if app_version:
        s3_bucket = app_version['SourceBundle']['S3Bucket']
        s3_key = app_version['SourceBundle']['S3Key']
        io.log_info("Application Version '{0}' exists. Source from S3: {1}/{2}.".format(version_label, s3_bucket, s3_key))

    return s3_bucket, s3_key


def create_app(app_name, default_env=None):
    # Attempt to create app
    try:
        io.log_info('Creating application: ' + app_name)
        elasticbeanstalk.create_application(
            app_name,
            strings['app.description']
        )

        set_environment_for_current_branch(None)
        set_group_suffix_for_current_branch(None)

        io.echo('Application', app_name,
                'has been created.')
        return None, None

    except AlreadyExistsError:
        io.log_info('Application already exists.')
        return pull_down_app_info(app_name, default_env=default_env)


def pull_down_app_info(app_name, default_env=None):
    # App exists, set up default environment
    envs = elasticbeanstalk.get_app_environments(app_name)
    if len(envs) == 0:
        # no envs, set None as default to override
        set_environment_for_current_branch(None)
        return None, None
    elif len(envs) == 1:
        # Set only env as default
        env = envs[0]
        io.log_info('Setting only environment "' +
                    env.name + '" as default')
    elif len(envs) > 1:
        if default_env:
            if default_env == '/ni':
                env = envs[0]
            else:
                env = next((env for env in envs if env.name == default_env),
                           None)
        if not default_env or env is None:
            # Prompt for default
            io.echo(prompts['init.selectdefaultenv'])
            env = utils.prompt_for_item_in_list(envs)

    set_environment_for_current_branch(env.name)

    io.log_info('Pulling down defaults from environment ' + env.name)
    # Get keyname
    keyname = elasticbeanstalk.get_specific_configuration_for_env(
        app_name, env.name, 'aws:autoscaling:launchconfiguration',
        'EC2KeyName'
    )
    if keyname is None:
        keyname = -1

    return env.platform.arn, keyname


def open_webpage_in_browser(url, ssl=False):
    io.log_info('Opening webpage with default browser.')
    if not url.startswith('http'):
        if ssl:
            url = 'https://' + url
        else:
            url = 'http://' + url
    LOG.debug('url={}'.format(url))
    if utils.is_ssh() or platform.system().startswith('Win'):
        # Prefered way for ssh or windows
        # Windows cant do a fork so we have to do inline
        LOG.debug('Running webbrowser inline.')
        import webbrowser
        webbrowser.open_new_tab(url)
    else:
        # this is the prefered way to open a web browser on *nix.
        # It squashes all output which can be typical on *nix.
        LOG.debug('Running webbrowser as subprocess.')
        from subprocess import Popen, PIPE

        p = Popen(['{python} -m webbrowser \'{url}\''
                  .format(python=sys.executable, url=url)],
                  stderr=PIPE, stdout=PIPE, shell=True)
        '''
         We need to fork the process for various reasons
            1. Calling p.communicate waits for the thread. Some browsers
                (if opening a new window) dont return to the thread until
                 the browser closes. We dont want the terminal to hang in
                 this case
            2. If we dont call p.communicate, there is a race condition. If
                the main process terminates before the browser call is made,
                the call never gets made and the browser doesn't open.
            Therefor the solution is to fork, then wait for the child
            in the backround.
         '''
        pid = os.fork()
        if pid == 0:  # Is child
            p.communicate()
        # Else exit


def create_dummy_app_version(app_name):
    version_label = 'Sample Application'
    return _create_application_version(app_name, version_label, None,
                                       None, None, warning=False)


def create_app_version(app_name, process=False, label=None, message=None, staged=False, build_config=None):
    cwd = os.getcwd()
    fileoperations._traverse_to_project_root()
    try:
        if heuristics.directory_is_empty():
            io.echo('NOTE: {}'.format(strings['appversion.none']))
            return None
    finally:
        os.chdir(cwd)

    source_control = SourceControl.get_source_control()
    if source_control.untracked_changes_exist():
        io.log_warning(strings['sc.unstagedchanges'])

    #get version_label
    if label:
        version_label = label
    else:
        version_label = source_control.get_version_label()
        if staged:
            # Make a unique version label
            timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
            version_label = version_label + '-stage-' + timestamp


    # get description
    if message:
        description = message
    else:
        description = source_control.get_message()

    if len(description) > 200:
        description = description[:195] + '...'

    # Check for zip or artifact deploy
    artifact = fileoperations.get_config_setting('deploy', 'artifact')
    if artifact:
        file_name, file_extension = os.path.splitext(artifact)
        file_name = version_label + file_extension
        file_path = artifact
        s3_key = None
        s3_bucket = None
    else:
        # Check if the app version already exists
        s3_bucket, s3_key = get_app_version_s3_location(app_name, version_label)

        # Create zip file if the application version doesn't exist
        if s3_bucket is None and s3_key is None:
            file_name, file_path = _zip_up_project(
                version_label, source_control, staged=staged)
        else:
            file_name = None
            file_path = None

    # Get s3 location
    bucket = elasticbeanstalk.get_storage_location() if s3_bucket is None else s3_bucket
    key = app_name + '/' + file_name if s3_key is None else s3_key

    # Upload to S3 if needed
    try:
        s3.get_object_info(bucket, key)
        io.log_info('S3 Object already exists. Skipping upload.')
    except NotFoundError:
        # If we got the bucket/key from the app version describe call and it doesn't exist then
        #   the application version must have been deleted out-of-band and we should throw an exception
        if file_name is None and file_path is None:
            raise NotFoundError('Application Version does not exist in the S3 bucket.'
                                ' Try uploading the Application Version again.')

        # Otherwise attempt to upload the local application version
        io.log_info('Uploading archive to s3 location: ' + key)
        s3.upload_application_version(bucket, key, file_path)

    fileoperations.delete_app_versions()
    io.log_info('Creating AppVersion ' + version_label)
    return _create_application_version(app_name, version_label, description,
                                       bucket, key, process, build_config=build_config)


def create_codecommit_app_version(app_name, process=False, label=None, message=None, build_config=None):
    cwd = os.getcwd()
    fileoperations._traverse_to_project_root()

    source_control = SourceControl.get_source_control()
    if source_control.get_current_commit() is None:
        io.log_warning('There are no commits for the current branch, attempting to create an empty commit and launching with the sample application')
        source_control.create_initial_commit()

    if source_control.untracked_changes_exist():
        io.log_warning(strings['sc.unstagedchanges'])

    #get version_label
    if label:
        version_label = label
    else:
        version_label = source_control.get_version_label()


    # get description
    if message:
        description = message
    else:
        description = source_control.get_message()

    if len(description) > 200:
        description = description[:195] + '...'

    # Push code with git
    try:
        source_control.push_codecommit_code()
    except CommandError as e:
        io.echo("Could not push code to the CodeCommit repository:")
        raise e

    # Get additional arguments for deploying code commit and poll
    #  for the commit to propagate to code commit.
    from ebcli.operations import gitops
    repository = gitops.get_default_repository()
    commit_id = source_control.get_current_commit()

    if repository is None or commit_id is None:
        raise ServiceError("Could not find repository or commit id to create an application version")

    # Deploy Application version with freshly pushed git commit

    io.log_info('Creating AppVersion ' + version_label)
    return _create_application_version(app_name, version_label, description,
                                       None, None, process, repository=repository, commit_id=commit_id,
                                       build_config=build_config)


def create_app_version_from_source(app_name, source, process=False, label=None, message=None, build_config=None):
    cwd = os.getcwd()
    fileoperations._traverse_to_project_root()
    try:
        if heuristics.directory_is_empty():
            io.echo('NOTE: {}'.format(strings['appversion.none']))
            return None
    finally:
        os.chdir(cwd)

    source_control = SourceControl.get_source_control()
    if source_control.untracked_changes_exist():
        io.log_warning(strings['sc.unstagedchanges'])

    # get version_label
    if label:
        version_label = label
    else:
        version_label = source_control.get_version_label()

    # get description
    if message:
        description = message
    else:
        description = source_control.get_message()

    if len(description) > 200:
        description = description[:195] + '...'

    # Parse the source and attempt to push via code commit
    source_location, repository, branch = utils.parse_source(source)

    if source_location == "codecommit":
        try:
            result = codecommit.get_branch(repository, branch)
        except ServiceError as ex:
            io.log_error("Could not get branch '{0}' for the repository '{1}' because of this error: {2}".format(branch,
                                                                                                                 repository,
                                                                                                                 ex.code))
            raise ex

        commit_id = result['branch']['commitId']
        if repository is None or commit_id is None:
            raise ServiceError("Could not find repository or commit id to create an application version")
    else:
        LOG.debug("Source location '{0}' is not supported".format(source_location))
        raise InvalidOptionsError("This command does not support the given source location: {0}".format(source_location))

    # Deploy Application version with freshly pushed git commit
    io.log_info('Creating AppVersion ' + version_label)
    return _create_application_version(app_name, version_label, description,
                                       None, None, process, repository=repository, commit_id=commit_id,
                                       build_config=build_config)


def _create_application_version(app_name, version_label, description,
                                bucket, key, process=False, warning=True,
                                repository=None, commit_id=None,
                                build_config=None):
    """
    A wrapper around elasticbeanstalk.create_application_version that
    handles certain error cases:
     * application doesnt exist
     * version already exists
     * validates BuildSpec files for CodeBuild
    """
    if build_config is not None:
        buildspecops.validate_build_config(build_config)
    while True:
        try:
            elasticbeanstalk.create_application_version(
                app_name, version_label, description, bucket, key, process, repository, commit_id, build_config
            )
            return version_label
        except InvalidParameterValueError as e:
            if e.message.startswith('Application Version ') and \
                        e.message.endswith(' already exists.'):
                # we must be deploying with an existing app version
                if warning:
                    io.log_warning('Deploying a previously deployed commit.')
                return version_label
            elif e.message == responses['app.notexists'].replace(
                        '{app-name}', '\'' + app_name + '\''):
                # App doesnt exist, must be a new region.
                ## Lets create the app in the region
                create_app(app_name)
            else:
                raise


def _zip_up_project(version_label, source_control, staged=False):
    # Create zip file
    file_name = version_label + '.zip'
    file_path = fileoperations.get_zip_location(file_name)

    # Check to see if file already exists from previous attempt
    if not fileoperations.file_exists(file_path):
        # If it doesn't already exist, create it
        io.echo(strings['appversion.create'].replace('{version}',
                                                     version_label))
        ignore_files = fileoperations.get_ebignore_list()
        if ignore_files is None:
            source_control.do_zip(file_path, staged)
        else:
            io.log_info('Found .ebignore, using system zip.')
            fileoperations.zip_up_project(file_path, ignore_list=ignore_files)
    return file_name, file_path


def update_environment(env_name, changes, nohang, remove=None,
                       template=None, timeout=None, template_body=None,
                       solution_stack_name=None, platform_arn=None):
    try:
        request_id = elasticbeanstalk.update_environment(
            env_name, changes, remove=remove, template=template,
            template_body=template_body,
            solution_stack_name=solution_stack_name, platform_arn=platform_arn)
    except InvalidStateError:
        io.log_error(prompts['update.invalidstate'])
        return
    except InvalidSyntaxError as e:
        io.log_error(prompts['update.invalidsyntax'] +
                     '\nError = ' + e.message)
        return

    if nohang:
        return

    io.echo('Printing Status:')

    wait_for_success_events(request_id, timeout_in_minutes=timeout,
                            can_abort=True)


# BRANCH-DEFAULTS FOR CONFIG FILE
def write_setting_to_current_branch(keyname, value):
    source_control = SourceControl.get_source_control()

    branch_name = source_control.get_current_branch()

    fileoperations.write_config_setting(
        'branch-defaults',
        branch_name,
        {keyname: value}
    )


def set_environment_for_current_branch(value):
    write_setting_to_current_branch('environment', value)


def set_group_suffix_for_current_branch(value):
    write_setting_to_current_branch('group_suffix', value)


def get_current_branch_environment():
    return get_setting_from_current_branch('environment')


def get_current_branch_group_suffix():
    return get_setting_from_current_branch('group_suffix')


def get_default_keyname():
    return get_config_setting_from_branch_or_default('default_ec2_keyname')


def get_default_profile(require_default=False):
    try:
        profile = get_config_setting_from_branch_or_default('profile')
        if profile is None and require_default:
            return "default"
        return profile
    except NotInitializedError:
        return None


def get_default_region():
    try:
        return get_config_setting_from_branch_or_default('default_region')
    except NotInitializedError:
        return None


def get_setting_from_current_branch(keyname):
    try:
        source_control = SourceControl.get_source_control()
        branch_name = source_control.get_current_branch()
    except CommandError as ex:
        LOG.debug("Git is not installed returning None for setting: %s".format(keyname))
        return None

    branch_dict = fileoperations.get_config_setting('branch-defaults', branch_name)

    if branch_dict is None:
        return None
    else:
        try:
            return branch_dict[keyname]
        except KeyError:
            return None


def get_config_setting_from_branch_or_default(key_name, default=_marker):
    setting = get_setting_from_current_branch(key_name)

    if setting is not None:
        return setting
    else:
        return fileoperations.get_config_setting('global', key_name, default=default)


def get_instance_ids(env_name):
    env = elasticbeanstalk.get_environment_resources(env_name)
    instances = [i['Id'] for i in env['EnvironmentResources']['Instances']]
    return instances


def upload_keypair_if_needed(keyname):
    keys = [k['KeyName'] for k in ec2.get_key_pairs()]
    if keyname in keys:
        return

    key_material = _get_public_ssh_key(keyname)

    try:
        ec2.import_key_pair(keyname, key_material)
    except AlreadyExistsError:
        return
    region = aws.get_region_name()
    io.log_warning(strings['ssh.uploaded'].replace('{keyname}', keyname)
                   .replace('{region}', region))


def _get_public_ssh_key(keypair_name):
    key_file = fileoperations.get_ssh_folder() + keypair_name
    if os.path.exists(key_file):
        file_name = key_file
    elif os.path.exists(key_file + '.pem'):
        file_name = key_file + '.pem'
    else:
        raise NotSupportedError(strings['ssh.filenotfound'].replace(
            '{key-name}', keypair_name))

    try:
        stdout, stderr, returncode = exec_cmd(['ssh-keygen', '-y', '-f',
                                           file_name])
        if returncode != 0:
            raise CommandError('An error occurred while trying '
                               'to get ssh public key')
        key_material = stdout
        return key_material
    except OSError:
        raise CommandError(strings['ssh.notpresent'])


def wait_for_processed_app_versions(app_name, version_labels, timeout=5):
    versions_to_check = list(version_labels)
    processed = {}
    failed = {}
    io.echo('--- Waiting for Application Versions to be pre-processed ---')
    for version in version_labels:
        processed[version] = False
        failed[version] = False
    start_time = datetime.utcnow()
    timediff = timedelta(seconds=timeout * 60)
    while not all([(processed[version] or failed[version]) for version in versions_to_check]):
        if _timeout_reached(start_time, timediff):
            io.log_error(strings['appversion.processtimeout'])
            return False
        io.LOG.debug('Retrieving app versions.')
        app_versions = elasticbeanstalk.get_application_versions(app_name, versions_to_check)["ApplicationVersions"]

        for v in app_versions:
            if v['Status'] == 'PROCESSED':
                processed[v['VersionLabel']] = True
                io.echo('Finished processing application version {}'
                        .format(v['VersionLabel']))
                versions_to_check.remove(v['VersionLabel'])

            elif v['Status'] == 'FAILED':
                failed[v['VersionLabel']] = True
                io.log_error(strings['appversion.processfailed'].replace('{app_version}',
                                                                         v['VersionLabel']))
                versions_to_check.remove(v['VersionLabel'])

        if all(processed.values()):
            return True

        _sleep(4)

    if any(failed.values()):
        io.log_error(strings['appversion.cannotdeploy'])
        return False
    return True


def create_default_instance_profile(profile_name=iam_attributes.DEFAULT_ROLE_NAME):
    """ Create default elasticbeanstalk IAM profile and return its name. """
    create_instance_profile(profile_name, iam_attributes.DEFAULT_ROLE_POLICIES)
    return profile_name


def create_instance_profile(profile_name, policy_arns, role_name=None, inline_policy_name=None, inline_policy_doc=None):
    """ Create instance profile and associated IAM role, and attach policy ARNs. 
        If role_name is omitted profile_name will be used as role name.
        Inline policy is optional. """
    try:
        name = iam.create_instance_profile(profile_name)
        if name:
            io.log_info('Created instance profile: {}.'.format(name))

        if not role_name:
            role_name = profile_name
        name = _create_instance_role(role_name, policy_arns)
        if name:
            io.log_info('Created instance role: {}.'.format(name))

        if inline_policy_name:
            iam.put_role_policy(role_name, inline_policy_name, inline_policy_doc)

        iam.add_role_to_profile(profile_name, role_name)

    except NotAuthorizedError:
        # Not a root account. Just assume role exists
        io.log_warning(strings['platformcreateiamdescribeerror.info'].format(profile_name=profile_name))

    return profile_name


def _create_instance_role(role_name, policy_arns):
    document = iam_documents.EC2_ASSUME_ROLE_PERMISSION
    ret = iam.create_role_with_policy(role_name, document, policy_arns)
    return ret


def _sleep(sleep_time):
    time.sleep(sleep_time)


def _timeout_reached(start, timediff):
    return (datetime.utcnow() - start) >= timediff