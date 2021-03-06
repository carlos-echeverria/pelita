#!/usr/bin/env python3

from collections import namedtuple
import contextlib
import io
import json
import logging
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
from tempfile import TemporaryFile
import uuid

import zmq

from .simplesetup import RemoteTeamPlayer, SimpleController, SimplePublisher, SimpleServer

_logger = logging.getLogger(__name__)
_mswindows = (sys.platform == "win32")

TeamSpec = namedtuple("TeamSpec", ["module", "address"])
ModuleSpec = namedtuple("ModuleSpec", ["prefix", "module"])


def get_python_process():
    py_proc = sys.executable
    if not py_proc:
        raise RuntimeError("Cannot retrieve current Python executable.")
    return py_proc


def shlex_unsplit(cmd):
    """
    Translates a list of command arguments into bash-like ‘human’ readable form.
    Pseudo-reverses shlex.split()

    Example
    -------
        >>> shlex_unsplit(["command", "-f", "Hello World"])
        "command -f 'Hello World'"

    Parameters
    ----------
    cmd : list of string
        command + parameter list

    Returns
    -------
    string
    """
    return " ".join(shlex.quote(arg) for arg in cmd)


def firstNN(*args):
    """
    Return the first argument not None.

    Example
    -------
        >>> firstNN(None, False, True)
        False
        >>> firstNN(True, False, True)
        True
        >>> firstNN(None, None, True)
        True
        >>> firstNN(None, 2, True)
        2
        >>> firstNN(None, None, None)
        None
        >>> firstNN()
        None
    """
    return next(filter(lambda x: x is not None, args), None)


def start_logging(filename):
    if not filename or filename == '-':
        hdlr = logging.StreamHandler()
    else:
        hdlr = logging.FileHandler(filename, mode='w')
    logger = logging.getLogger('pelita')
    FORMAT = '[%(relativeCreated)06d %(name)s:%(levelname).1s][%(funcName)s] %(message)s'
    formatter = logging.Formatter(FORMAT)
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)
    logger.setLevel(logging.DEBUG)


class ModuleRunner:
    def __init__(self, team_spec):
        self.team_spec = team_spec

class DefaultRunner(ModuleRunner):
    def call_args(self, addr):
        player = 'pelita.scripts.pelita_player'
        external_call = [get_python_process(),
                         '-m',
                         player,
                         self.team_spec,
                         addr,
                         '--color',
                         self.color]
        return external_call

class BinRunner(ModuleRunner):
    def call_args(self, addr):
        external_call = [self.team_spec,
                         addr]
        return external_call

@contextlib.contextmanager
def _call_pelita_player(module_spec, address, color='', dump=None):
    """ Context manager version of `call_pelita_player`.

    Runs `call_pelita_player` as long as the `with` statement is executed
    and automatically terminates it afterwards. This is useful, if one
    just needs to send a few commands to a player.
    """

    proc = None
    try:
        proc, stdout, stderr = call_pelita_player(module_spec, address, color, dump)
        yield proc
    except KeyboardInterrupt:
        pass
    finally:
        # we close stdout, stderr before terminating
        # this hopefully means that it will do some flushing
        if stdout:
            stdout.close()
        if stderr:
            stderr.close()
        if proc is None:
            print("Problem running pelita player.")
        else:
            _logger.debug("Terminating proc %r", proc)
            proc.terminate()
            proc.wait()
            _logger.debug("%r terminated.", proc)


def call_pelita_player(module_spec, address, color='', dump=None):
    """ Starts another process with the same Python executable,
    the same start script (pelitagame) and runs `team_spec`
    as a standalone client on URL `addr`.
    """
    defined_runners = {
        "py": DefaultRunner,
        "bin": BinRunner,
    }

    if module_spec.prefix is not None:
        try:
            runner = defined_runners[module_spec.prefix]
        except KeyError:
            raise ValueError("Unknown runner: {}:".format(module_spec.prefix))
    else:
        runner = DefaultRunner
    runner_inst = runner(module_spec.module)
    runner_inst.color = color
    call_args = runner_inst.call_args(address)
    _logger.debug("Executing: %r", call_args)
    if dump:
        stdout = Path(dump + '.' + (color or module_spec) + '.out').open('w')
        stderr = Path(dump + '.' + (color or module_spec) + '.err').open('w')
        return (subprocess.Popen(call_args, stdout=stdout, stderr=stderr), stdout, stderr)
    else:
        return (subprocess.Popen(call_args), None, None)


@contextlib.contextmanager
def run_and_terminate_process(args, **kwargs):
    """ This serves as a contextmanager around `subprocess.Popen`, ensuring that
    after the body of the with-clause has finished, the process itself (and the
    process’s children) terminates as well.

    On Unix we try sending a SIGTERM before killing the process group but as
    afterwards we only wait on the first child, this means that the grand children
    do not get the chance to properly terminate.
    In cases where the first child has children that should properly close, the
    first child should catch SIGTERM with a signal handler and wait on its children.

    On Windows we send a CTRL_BREAK_EVENT to the whole process group and
    hope for the best. :)
    """

    _logger.debug("Executing: {}".format(shlex_unsplit(args)))

    try:
        if _mswindows:
            p = subprocess.Popen(args, **kwargs, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            p = subprocess.Popen(args, **kwargs, preexec_fn=os.setsid)
        yield p
    finally:
        if _mswindows:
            _logger.debug("Sending CTRL_BREAK_EVENT to {proc} with pid {pid}.".format(proc=p, pid=p.pid))
            os.kill(p.pid, signal.CTRL_BREAK_EVENT)
        else:
            try:
                pgid = os.getpgid(p.pid)
                _logger.debug("Sending SIGTERM to pgid {pgid}.".format(pgid=pgid))
                os.killpg(pgid, signal.SIGTERM) # send sigterm, or ...
                try:
                    # It would be nicer to wait with os.waitid on the process group
                    # but that does not seem to exist on macOS.
                    p.wait(3)
                except subprocess.TimeoutExpired:
                    _logger.debug("Sending SIGKILL to pgid {pgid}.".format(pgid=pgid))
                    os.killpg(pgid, signal.SIGKILL) # send sigkill, or ...
            except ProcessLookupError:
                # did our process group vanish?
                # we try killing only the child process then
                _logger.debug("Sending SIGTERM to pid {pid}.".format(pid=p.pid))
                p.terminate()
                try:
                    p.wait(3)
                except subprocess.TimeoutExpired:
                    _logger.debug("Sending SIGKILL to pid {pid}.".format(pid=p.pid))
                    p.kill()


def call_pelita(team_specs, *, rounds, filter, viewer, dump, seed):
    """ Starts a new process with the given command line arguments and waits until finished.

    Returns
    =======
    tuple of (game_state, stdout, stderr)
    """
    team1, team2 = team_specs

    ctx = zmq.Context()
    reply_sock = ctx.socket(zmq.PAIR)

    if os.name.upper() == 'POSIX':
        filename = 'pelita-reply.{uuid}'.format(pid=os.getpid(), uuid=uuid.uuid4())
        path = os.path.join(tempfile.gettempdir(), filename)
        reply_addr = 'ipc://' + path
        reply_sock.bind(reply_addr)
    else:
        addr = 'tcp://127.0.0.1'
        reply_port = reply_sock.bind_to_random_port(addr)
        reply_addr = 'tcp://127.0.0.1' + ':' + str(reply_port)

    rounds = ['--rounds', str(rounds)] if rounds else []
    filter = ['--filter', filter] if filter else []
    viewer = ['--' + viewer] if viewer else []
    dump = ['--dump', dump] if dump else []
    seed = ['--seed', seed] if seed else []

    cmd = [get_python_process(), '-m', 'pelita.scripts.pelita_main',
           team1, team2,
           '--reply-to', reply_addr,
           *seed,
           *dump,
           *filter,
           *rounds,
           *viewer]

    # We need to run a process in the background in order to await the zmq events
    # stdout and stderr are written to temporary files in order to be more portable
    with TemporaryFile(mode='w+t') as stdout_buf, TemporaryFile(mode='w+t') as stderr_buf:
        # We use the environment variable PYTHONUNBUFFERED here to retrieve stdout without buffering
        with run_and_terminate_process(cmd, stdout=stdout_buf, stderr=stderr_buf,
                                            universal_newlines=True,
                                            env=dict(os.environ, PYTHONUNBUFFERED='x')) as proc:

            poll = zmq.Poller()
            poll.register(reply_sock, zmq.POLLIN)

            final_game_state = None

            while True:
                evts = dict(poll.poll(1000))

                if not evts and proc.poll() is not None:
                    # no more events and proc has finished.
                    # we break the loop
                    break

                socket_ready = evts.get(reply_sock, False)
                if socket_ready:
                    try:
                        pelita_status = json.loads(reply_sock.recv_string())
                        game_state = pelita_status['__data__']['game_state']
                        finished = game_state.get("finished", None)
                        team_wins = game_state.get("team_wins", None)
                        game_draw = game_state.get("game_draw", None)
                        if finished:
                            final_game_state = game_state
                            break
                    except ValueError:  # JSONDecodeError
                        pass
                    except KeyError:
                        pass

        stdout_buf.seek(0)
        stderr_buf.seek(0)
        return (final_game_state, stdout_buf.read(), stderr_buf.read())


def check_team(team_spec):
    ctx = zmq.Context()
    socket = ctx.socket(zmq.PAIR)

    if team_spec.module is None:
        _logger.info("Binding zmq.PAIR to %s", team_spec.address)
        socket.bind(team_spec.address)

    else:
        _logger.info("Binding zmq.PAIR to %s", team_spec.address)
        socket_port = socket.bind_to_random_port(team_spec.address)
        team_spec = team_spec._replace(address="%s:%d" % (team_spec.address, socket_port))

    team_player = RemoteTeamPlayer(socket)

    if team_spec.module:
        with _call_pelita_player(team_spec.module, team_spec.address):
            name = team_player.team_name()
    else:
        name = team_player.team_name()

    return name

def strip_module_prefix(module):
    if "@" in module:
        try:
            prefix, module = module.split("@")
            return ModuleSpec(prefix=prefix, module=module)
        except ValueError:
            raise ValueError("Bad module definition: {}.".format(module))
    else:
        return ModuleSpec(prefix=None, module=module)

def prepare_team(team_spec):
    # check, if team_spec is a move function
    if callable(team_spec):
        return TeamSpec(module=None, address=team_spec)
    # check if we've been given an address which a remote
    # player wants to connect to
    if "://" in team_spec:
        module = None
        address = team_spec
    else:
        module = strip_module_prefix(team_spec)
        address = "tcp://127.0.0.1"
    return TeamSpec(module, address)

def run_game(team_specs, *, rounds, layout, layout_name="", seed=None, dump=False,
                            max_timeouts=5, timeout_length=3,
                            viewers=None, controller=None, publisher=None):

    if viewers is None:
        viewers = []

    teams = [prepare_team(team_spec) for team_spec in team_specs]

    server = SimpleServer(layout_string=layout,
                          rounds=rounds,
                          bind_addrs=[team.address for team in teams],
                          max_timeouts=max_timeouts,
                          timeout_length=timeout_length,
                          layout_name=layout_name,
                          seed=seed)

    # Update our teams with the bound addresses
    teams = [
        team._replace(address=address)
        for team, address in zip(teams, server.bind_addresses)
    ]

    color = {}
    for idx, team in enumerate(teams):
        if idx == 0:
            color[team] = 'Blue'
        elif idx == 1:
            color[team] = 'Red'
        else:
            color[team] = ''
        if team.module is None:
            print("Waiting for external team %d to connect to %s." % (idx, team.address))

    external_players = [
        call_pelita_player(team.module, team.address, color[team], dump=dump)
        for team in teams
        if team.module
    ]

    for viewer in viewers:
        server.game_master.register_viewer(viewer)

    if publisher:
        server.game_master.register_viewer(publisher)

    with autoclose_subprocesses(external_players):
        if controller is not None:
            if controller.game_master is None:
                controller.game_master = server.game_master
            controller.run()
            server.exit_teams()
        else:
            server.run()
        return server.game_master.game_state

@contextlib.contextmanager
def tk_viewer(publish_to=None, geometry=None, delay=None):
    if publish_to is None:
        publish_to = "tcp://127.0.0.1:*"
    publisher = SimplePublisher(publish_to)
    controller = SimpleController(None, "tcp://127.0.0.1:*")

    viewer = run_external_viewer(publisher.socket_addr, controller.socket_addr,
                                 geometry=geometry, delay=delay)
    yield { "publisher": publisher, "controller": controller }


@contextlib.contextmanager
def channel_setup(publish_to=None, reply_to=None):
    if publish_to is None:
        publish_to = "tcp://127.0.0.1:*"
    publisher = SimplePublisher(publish_to)
    controller = SimpleController(None, "tcp://127.0.0.1:*", reply_to=reply_to)

    yield { "publisher": publisher, "controller": controller }


def run_external_viewer(subscribe_sock, controller, geometry, delay, stop_after):
    # Something on OS X prevents Tk from running in a forked process.
    # Therefore we cannot use multiprocessing here. subprocess works, though.
    viewer_args = [ str(subscribe_sock) ]
    if controller:
        viewer_args += ["--controller-address", str(controller)]
    if geometry:
        viewer_args += ["--geometry", "{0}x{1}".format(*geometry)]
    if delay:
        viewer_args += ["--delay", str(delay)]
    if stop_after is not None:
        viewer_args += ["--stop-after", str(stop_after)]

    tkviewer = 'pelita.scripts.pelita_tkviewer'
    external_call = [get_python_process(),
                     '-m',
                     tkviewer] + viewer_args
    _logger.debug("Executing: %r", external_call)
    # os.setsid will keep the viewer from closing when the main process exits
    # a better solution might be to decouple the viewer from the main process
    if _mswindows:
        p = subprocess.Popen(external_call, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        p = subprocess.Popen(external_call, preexec_fn=os.setsid)
    return p

@contextlib.contextmanager
def autoclose_subprocesses(subprocesses):
    """
    Automatically close subprocesses when the context ends.
    This needs to be done to shut down misbehaving bots
    when the main program finishes.
    """
    try:
        yield
    except KeyboardInterrupt:
        pass
    finally:
        # we close stdout, stderr before terminating
        # this hopefully means that it will do some flushing
        for (sp, stdout, stderr) in subprocesses:
            if stdout:
                stdout.close()
            if stderr:
                stderr.close()
        # kill all client processes. NOW!
        # (is ths too early?)
        for (sp, stdout, stderr) in subprocesses:
            _logger.debug("Attempting to terminate %r.", sp)
            sp.terminate()
        for (sp, stdout, stderr) in subprocesses:
            sp.wait()
            _logger.debug("%r terminated.", sp)
