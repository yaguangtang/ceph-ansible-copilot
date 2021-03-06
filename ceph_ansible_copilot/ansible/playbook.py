

# Embedding Ansible through python API - requires ansible 2.4 or above
# ref: http://docs.ansible.com/ansible/latest/dev_guide/developing_api.html

import logging

from collections import namedtuple

from ansible.cli import CLI as cli
from ansible.parsing.dataloader import DataLoader
from ansible.vars.manager import VariableManager
from ansible.inventory.manager import InventoryManager
from ansible.playbook.play import Play
from ansible.executor.task_queue_manager import TaskQueueManager
from ansible.executor.playbook_executor import PlaybookExecutor
from ansible.plugins.callback import CallbackBase


class ResultCallback(CallbackBase):
    """ Callback plugin to act on results as they are emitted """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'pb_results'

    def __init__(self, pb_callout=None, logger=None):

        self.logger = logger

        self.stats = {'task_state': {
                                     'success': 0,
                                     'failed': 0,
                                     'skipped': 0,
                                     'unreachable': 0
                                     },
                      'failures': {},
                      'successes': {},
                      'task_name': ''
                      }

        self.done = 0
        self.pb_callout = pb_callout

        CallbackBase.__init__(self)

    def _log_msg(self, result, msg_type='info'):

        msg = "{} : {}".format(result._host.name,
                               result._result)

        if msg_type == 'info':
            self.logger.info(msg)
        else:
            self.logger.error(msg)

    def _handle_warnings(self, res):
        """ display warnings, by default these end up in stdout interfering
            with the UI. So instead of that, we log them
        """

        # if C.COMMAND_WARNINGS:
        if 'warnings' in res and res['warnings']:
            for warning in res['warnings']:
                self.logger.warning(warning)
            del res['warnings']
        if 'deprecations' in res and res['deprecations']:
            for warning in res['deprecations']:
                self.logger.warning(**warning)
            del res['deprecations']

    def v2_runner_on_ok(self, result, **kwargs):
        host = result._host.name

        self._handle_warnings(result._result)

        # Hold output of last command
        self.stats['successes'][host] = result._result
        # self.host_ok[host] = result._result

        # if self.logger:
        #     self._log_msg(result)

        self.stats['task_state']['success'] += 1
        if self.pb_callout:
            self.pb_callout(self.stats)

    def v2_runner_on_failed(self, result, **kwargs):
        # receive TaskResult object

        host = result._host.name

        self._handle_warnings(result._result)

        if host in self.stats['failures'].keys():
            self.stats['failures'][host].append(result._result)
        else:
            self.stats['failures'][host] = [result._result]

        if self.logger:
            self._log_msg(result, msg_type='error')

        self.stats['task_state']['failed'] += 1
        if self.pb_callout:
            self.pb_callout(self.stats)

    def v2_runner_on_unreachable(self, result, **kwargs):
        host = result._host.name

        self._handle_warnings(result._result)
        self.stats['task_state']['unreachable'] += 1
        if self.pb_callout:
            self.pb_callout(self.stats)

    def v2_runner_on_skipped(self, result, **kwargs):
        host = result._host.name
        self._handle_warnings(result._result)

        self.stats['task_state']['skipped'] += 1
        if self.pb_callout:
            self.pb_callout(self.stats)

    def playbook_on_task_start(self, name, is_conditional):

        self.stats['task_name'] = name
        if self.pb_callout:
            pass


class CoPilotPlaybookError(Exception):
    pass


class CoPilotPlayBook(object):

    def __init__(self, host_list, callback=None):

        Options = namedtuple('Options',
                             ['connection', 'module_path', 'forks', 'become',
                              'become_method', 'become_user', 'check', 'diff',
                              'listtags', 'listtasks', 'listhosts', 'syntax']
                             )

        self.logger = logging.getLogger('copilot')

        # initialize needed objects
        self.loader = DataLoader()

        self.options = Options(
                               syntax=False,
                               listtags=False,
                               listtasks=False,
                               listhosts=False,
                               connection='ssh',
                               module_path='',
                               forks=100,
                               become=True,
                               become_method='sudo',
                               become_user='root',
                               check=False,
                               diff=False
                       )

        # create inventory and pass to variable manager
        self.inventory = InventoryManager(loader=self.loader,
                                          sources=host_list)

        self.host_list = host_list

        self.variable_manager = VariableManager(loader=self.loader,
                                                inventory=self.inventory)

        # from ansible 2.4 the ansible_version is set in the cli module, and
        # since we're using the api we need to set it explicitly to make it
        # available to any playbooks we're asked to run
        self.variable_manager.extra_vars = {
            "ansible_version": cli.version_info(gitinfo=False)
        }

        self.callback = callback
        self.pb_file = None
        self.playbook = None
        self.rc = 0

    def setup(self):
        raise CoPilotPlaybookError("Missing 'setup' method override")

    def run(self):
        raise CoPilotPlaybookError("Missing 'run' method override")


class DynamicPlaybook(CoPilotPlayBook):

    def setup(self, pb_name="Dynamic playbook",
              pb_tasks=None):

        if not pb_tasks or not isinstance(pb_tasks, list):
            raise CoPilotPlaybookError("Dynamic Playbook created with "
                                       "missing/invalid tasks")

        # define the playbook
        play_src = dict(
            name=pb_name,
            hosts='all',
            gather_facts="no",
            tasks=pb_tasks
        )

        self.playbook = Play().load(play_src,
                                    variable_manager=self.variable_manager,
                                    loader=self.loader)

    def run(self):
        # running the playbook
        tqm = None
        try:
            tqm = TaskQueueManager(
                    inventory=self.inventory,
                    variable_manager=self.variable_manager,
                    loader=self.loader,
                    options=self.options,
                    passwords={},
                    # stdout_callback="default",
                    stdout_callback=self.callback,
                )
            self.rc = tqm.run(self.playbook)

        finally:
            if tqm is not None:
                tqm.cleanup()


class StaticPlaybook(CoPilotPlayBook):

    def setup(self, pb_file):

        self.pb_file = pb_file
        self.playbook = PlaybookExecutor(playbooks=[self.pb_file],
                                         inventory=self.inventory,
                                         variable_manager=self.variable_manager,
                                         loader=self.loader,
                                         options=self.options,
                                         passwords={})

    def run(self):

        self.playbook._tqm._stdout_callback = self.callback

        self.rc = self.playbook.run()

