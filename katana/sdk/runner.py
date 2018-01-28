"""
Python 2 SDK for the KATANA(tm) Framework (http://katana.kusanagi.io)

Copyright (c) 2016-2017 KUSANAGI S.L. All rights reserved.

Distributed under the MIT license.

For the full copyright and license information, please view the LICENSE
file that was distributed with this source code.

"""
from __future__ import absolute_import

import functools
import inspect
import json
import logging
import os
import signal

import click
import gevent
import katana.payload
import zmq.green

from ..errors import KatanaError
from ..logging import setup_katana_logging
from ..utils import EXIT_ERROR
from ..utils import EXIT_OK
from ..utils import ipc
from ..utils import tcp

__license__ = "MIT"
__copyright__ = "Copyright (c) 2016-2017 KUSANAGI S.L. (http://kusanagi.io)"

LOG = logging.getLogger(__name__)

# Mappings between Syslog numeric severity levels and python logging levels
LEVELS = {
    0: logging.CRITICAL,  # Emergency
    1: logging.CRITICAL,  # Alert
    2: logging.CRITICAL,  # Critical
    3: logging.ERROR,     # Error
    4: logging.WARNING,   # Notice
    5: logging.WARNING,   # Warning
    6: logging.INFO,      # Info
    7: logging.DEBUG,     # Debug
    }


def key_value_strings_callback(ctx, param, values):
    """Option callback to validate a list of key/value arguments.

    Converts 'NAME=VALUE' cli parameters to a dictionary.

    :rtype: dict

    """

    params = {}
    if not values:
        return params

    for value in values:
        parts = value.split('=', 1)
        if len(parts) != 2:
            raise click.BadParameter('Invalid parameter format')

        param_name, param_value = parts
        params[param_name] = param_value

    return params


def apply_cli_options(run_method):
    """Decorator to apply command line options to `run` method.

    Run is called after all command line options are parsed and validated.

    """

    @functools.wraps(run_method)
    def wrapper(self):
        # Create a command object to run the SDK component.
        # Component caller source file name is used as command name.
        caller_frame = inspect.getouterframes(inspect.currentframe())[2]
        self.source_file = caller_frame[1]
        command = click.command(name=self.source_file, help=self.help)

        # Run method is called when command line options are valid
        start_component = command(functools.partial(run_method, self))

        # Apply CLI options to command
        for option in self.get_argument_options():
            start_component = option(start_component)

        if not os.environ.get('TESTING'):
            # Run SDK component
            start_component()
        else:
            # Allow unit tests to properly parse CLI arguments
            return start_component

    return wrapper


class ComponentRunner(object):
    """Component runner.

    This class allows to isolate Component implementation details and
    keep the Component itself consisten with KATANA SDK specifications.

    """

    def __init__(self, component, server_cls, help):
        """Constructor.

        :param component: The component to run.
        :type component: Component
        :param server_cls: Class for the component server.
        :param server_cls: ComponentServer
        :param help: Help text for the CLI command.
        :type help: str

        """

        self.__startup_callback = None
        self.__shutdown_callback = None
        self.__error_callback = None
        self._args = {}
        self.component = component
        self.source_file = None
        self.callbacks = None
        self.server_cls = server_cls
        self.help = help

    @property
    def args(self):
        """Command line arguments.

        Command line arguments are initialized during `run`
        with the values used to run the component.

        :rtype: dict

        """

        return self._args

    @property
    def socket_name(self):
        """IPC socket name.

        :rtype: str

        """

        return self._args.get('socket') or self.get_default_socket_name()

    @property
    def tcp_port(self):
        """TCP port number.

        :rtype: str or None

        """

        return self._args.get('tcp')

    @property
    def name(self):
        """Component name.

        :rtype: str

        """

        return self._args['name']

    @property
    def version(self):
        """Component version.

        :rtype: str

        """

        return self._args['version']

    @property
    def component_type(self):
        """Component type.

        :rtype: str

        """

        return self._args['component']

    @property
    def debug(self):
        """Check if debug is enabled for current component.

        :rtype: bool

        """

        return self._args.get('debug', False)

    @property
    def compact_names(self):
        """Check if payloads should use compact names.

        :rtype: bool

        """

        return not self._args.get('disable_compact_names', False)

    def get_default_socket_name(self):
        """Get a default socket name to use when socket name is missing.

        :rtype: str

        """

        # Remove 'ipc://' from string to get socket name
        return ipc(self.component_type, self.name, self.version)[6:]

    def get_argument_options(self):
        """Get command line argument options.

        :rtype: list.

        """

        return [
            click.option(
                '-A', '--action',
                help=(
                    'Name of the action to call when request message '
                    'is given as JSON through stdin.'
                    ),
                ),
            click.option(
                '-c', '--component',
                type=click.Choice(['service', 'middleware']),
                help='Component type.',
                required=True,
                ),
            click.option(
                '-d', '--disable-compact-names',
                is_flag=True,
                help='Use full property names in payloads.',
                ),
            click.option(
                '-D', '--debug',
                is_flag=True,
                help='Enable component debug.',
                ),
            click.option(
                '-L', '--log-level',
                help=(
                    'Enable a logging using a numeric Syslog severity '
                    'value to set the level.'
                    ),
                type=click.IntRange(0, 7, clamp=True),
                ),
            click.option(
                '-n', '--name',
                required=True,
                help='Component name.',
                ),
            click.option(
                '-p', '--framework-version',
                required=True,
                help='KATANA framework version.',
                ),
            click.option(
                '-s', '--socket',
                help='IPC socket name.',
                ),
            click.option(
                '-t', '--tcp',
                help='TCP port to use when IPC socket is not used.',
                type=click.INT,
                ),
            click.option(
                '-T', '--timeout',
                help='Process execution timeout per request in milliseconds.',
                type=click.INT,
                default=30000,
                ),
            click.option(
                '-v', '--version',
                required=True,
                help='Component version.',
                ),
            click.option(
                '-V', '--var',
                multiple=True,
                callback=key_value_strings_callback,
                help='Component variables.',
                ),
            ]

    def set_startup_callback(self, callback):
        """Set a callback to be run during startup.

        :param callback: A callback to run on startup.
        :type callback: function

        """

        self.__startup_callback = callback

    def set_shutdown_callback(self, callback):
        """Set a callback to be run during shutdown.

        :param callback: A callback to run on shutdown.
        :type callback: function

        """

        self.__shutdown_callback = callback

    def set_error_callback(self, callback):
        """Set a callback to be run on message callback errors.

        :param callback: A callback to run on message callback errors.
        :type callback: function

        """

        self.__error_callback = callback

    def set_callbacks(self, callbacks):
        """Set message callbacks for each component action.

        :params callbacks: Callbacks for each action.
        :type callbacks: dict

        """

        self.callbacks = callbacks

    @apply_cli_options
    def run(self, **kwargs):
        """Run SDK component server.

        Calling this method checks command line arguments before
        component server starts, and then blocks the caller script
        until component server finishes.

        """

        self._args = kwargs

        # Standard input is read only when action name is given
        message = {}
        if kwargs.get('action'):
            contents = click.get_text_stream('stdin', encoding='utf8').read()

            # Add JSON file contents to message
            try:
                message['payload'] = json.loads(contents)
            except:
                LOG.exception('Stdin input value is not valid JSON')
                os._exit(EXIT_ERROR)

            # Add action name to message
            message['action'] = kwargs['action']

        # When compact mode is enabled use long payload field names
        if not self.compact_names:
            katana.payload.DISABLE_FIELD_MAPPINGS = True

        # Create component server
        server = self.server_cls(
            self.callbacks,
            self.args,
            debug=self.debug,
            source_file=self.source_file,
            error_callback=self.__error_callback,
            )

        # Initialize component logging
        log_level = kwargs.get('log_level')
        if log_level in LEVELS:
            setup_katana_logging(
                self.server_cls.get_type(),
                server.component_name,
                server.component_version,
                server.framework_version,
                LEVELS[log_level],
                )

        LOG.debug('Using PID: "%s"', os.getpid())

        if not message:
            # Create channel for TCP or IPC conections
            if self.tcp_port:
                channel = tcp('127.0.0.1:{}'.format(self.tcp_port))
            else:
                # Abstract domain unix socket
                channel = 'ipc://{}'.format(self.socket_name)

        # By default exit successfully
        exit_code = EXIT_OK

        # Call startup callback
        if self.__startup_callback:
            LOG.info('Running startup callback ...')
            try:
                self.__startup_callback(self.component)
            except:
                LOG.exception('Startup callback failed')
                LOG.error('Component failed')
                exit_code = EXIT_ERROR

        # Run component server
        if exit_code != EXIT_ERROR:
            try:
                # Create a greenlet to run server
                if message:
                    greenlet = gevent.spawn(server.process_input, message)
                else:
                    greenlet = gevent.spawn(server.listen, channel)

                # Listen for SIGTERM and SIGINT
                gevent.signal(signal.SIGTERM, greenlet.kill)
                gevent.signal(signal.SIGINT, greenlet.kill)
                # Run server
                greenlet.join()
            except KatanaError as err:
                exit_code = EXIT_ERROR
                LOG.error(err)
                LOG.error('Component failed')
            except zmq.error.ZMQError as err:
                exit_code = EXIT_ERROR
                if err.errno == 98:
                    LOG.error('Address unavailable: "%s"', self.socket_name)
                else:
                    LOG.error(err.strerror)

                LOG.error('Component failed')
            except Exception:
                exit_code = EXIT_ERROR
                LOG.exception('Component failed')

        # Call shutdown callback
        if self.__shutdown_callback:
            LOG.info('Running shutdown callback ...')
            try:
                self.__shutdown_callback(self.component)
            except:
                LOG.exception('Shutdown callback failed')
                LOG.error('Component failed')
                exit_code = EXIT_ERROR

        if exit_code == EXIT_OK:
            LOG.info('Operation complete')

        os._exit(exit_code)
