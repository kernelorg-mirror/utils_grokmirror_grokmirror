# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-pull's daemon socket listener.

The listener is how a push notification reaches a running daemon (see
contrib/pubsubv1.py for one producer), so a repo named on the socket has to
come back out of the manifest queue with its fingerprint cleared, which is
what forces a pull. It is also the one part of grok-pull that talks to the
outside world over a socket, so it has to survive whatever arrives on it: the
handler drops the connection on anything at all going wrong, and the daemon
keeps listening.
"""

from __future__ import annotations

import contextlib
import queue
import socket
import stat
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree

# Nothing here should take more than a few milliseconds; the timeouts are only
# so that a regression fails the test instead of hanging the whole suite.
TIMEOUT = 30


@contextlib.contextmanager
def listener(
    tree: GrokTree, manifest: grokmirror.Manifest
) -> Iterator[tuple[Path, queue.Queue[grokmirror.pull.ManiItem]]]:
    """Run a real listener on a unix socket, yielding it and its manifest queue."""
    grokmirror.write_manifest(str(tree.manifest), manifest)
    config = grokmirror.load_config_file(str(tree.write_config()))
    sockfile = tree.toplevel.parent / 'grok-pull.sock'

    server = grokmirror.pull.ThreadedUnixStreamServer(str(sockfile), grokmirror.pull.Handler)
    server.config = config
    server.q_mani = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield sockfile, server.q_mani
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=TIMEOUT)
        assert not thread.is_alive()


def tell(sockfile: Path, payload: bytes) -> None:
    """Send payload to the listener and wait for it to close the connection.

    Half-closing our end is what makes this deterministic: the handler's next
    readline() returns b'', which it treats as end of conversation and returns,
    so by the time recv() gives us b'' the queue is fully populated.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT)
        sock.connect(str(sockfile))
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(1) == b''


def test_a_known_repo_is_queued_for_a_pull(tree: GrokTree) -> None:
    with listener(tree, {'/test/one.git': {'fingerprint': 'deadbeef', 'modified': 5}}) as (sockfile, q_mani):
        tell(sockfile, b'/test/one.git\n')

        gitdir, repoinfo, action = q_mani.get(timeout=TIMEOUT)
        assert (gitdir, action) == ('/test/one.git', 'pull')
        # Cleared, because a fingerprint that still matches the repo on disk is
        # exactly how pull_repo() decides there is nothing to do.
        # The sentinel default distinguishes "set to None", which is what forces
        # the pull, from "the key never made it into the queued repoinfo".
        assert repoinfo.get('fingerprint', 'unset') is None
        assert q_mani.empty()


def test_several_repos_on_one_connection(tree: GrokTree) -> None:
    # The handler loops until the client stops talking, so one connection may
    # name any number of repos. A producer that batches its notifications must
    # not have every repo after the first silently dropped.
    manifest: grokmirror.Manifest = {
        '/test/one.git': {'fingerprint': 'aaa', 'modified': 5},
        '/test/two.git': {'fingerprint': 'bbb', 'modified': 5},
    }
    with listener(tree, manifest) as (sockfile, q_mani):
        tell(sockfile, b'/test/one.git\n/test/two.git\n')

        queued = [q_mani.get(timeout=TIMEOUT)[0] for _ in range(2)]
        assert sorted(queued) == ['/test/one.git', '/test/two.git']
        assert q_mani.empty()


@pytest.mark.parametrize(
    ('payload', 'why'),
    [
        (b'/test/nosuch.git\n', 'a repo that is not in the manifest'),
        (b'\xff\xfe\n', 'input that is not utf-8, which used to raise inside the handler'),
        (b'', 'a client that connects and says nothing at all'),
    ],
)
def test_junk_is_dropped_and_the_daemon_keeps_listening(
    tree: GrokTree, payload: bytes, why: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with listener(tree, {'/test/one.git': {'fingerprint': 'deadbeef', 'modified': 5}}) as (sockfile, q_mani):
        tell(sockfile, payload)
        assert q_mani.empty(), why

        # An exception that escapes handle() is not fatal -- socketserver
        # catches it, prints a traceback and closes the connection, so the
        # client cannot tell the difference. The daemon's log can: dropping
        # the connection is expected, a traceback in the daemon's stderr on
        # every malformed notification is not.
        assert 'Traceback' not in capsys.readouterr().err, why

        # The listener is a long-running daemon: one bad connection must not
        # cost it the next, good one.
        tell(sockfile, b'/test/one.git\n')
        assert q_mani.get(timeout=TIMEOUT)[0] == '/test/one.git'


# -- start_socket_listener() ---------------------------------------------------
#
# The tests above build the server/Handler by hand, bypassing
# start_socket_listener() itself. These drive that function directly, since it
# has its own decision logic (no-op when unconfigured, refuse a stale
# non-socket file, bind+chmod+spawn a daemon thread on success) that none of
# the above exercises.


def test_start_socket_listener_is_a_noop_when_socket_is_unconfigured(tree: GrokTree) -> None:
    # An empty [pull] section is the "no socket wanted" spelling: no sockfile
    # path was ever computed, so there is nothing to check for existence.
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {}})))

    grokmirror.pull.start_socket_listener(config, queue.Queue())


def test_start_socket_listener_refuses_a_stale_non_socket_file(tree: GrokTree) -> None:
    sockfile = tree.root / 'grok-pull.sock'
    # A leftover regular file at the configured path -- not a socket at all,
    # e.g. left behind by a crashed process that never got to bind it.
    sockfile.write_text('not a socket\n')
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {'socket': str(sockfile)}})))

    with pytest.raises(grokmirror.GrokError, match='File exists but is not a socket'):
        grokmirror.pull.start_socket_listener(config, queue.Queue())


def test_start_socket_listener_binds_and_serves(tree: GrokTree) -> None:
    sockfile = tree.root / 'grok-pull.sock'
    grokmirror.write_manifest(str(tree.manifest), {'/test/one.git': {'fingerprint': 'deadbeef', 'modified': 5}})
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {'socket': str(sockfile)}})))
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.start_socket_listener(config, q_mani)

    # Deliberately world-writable: anyone able to reach the socket may ask the
    # daemon to check a repository.
    assert stat.S_IMODE(sockfile.stat().st_mode) == 0o777
    tell(sockfile, b'/test/one.git\n')
    assert q_mani.get(timeout=TIMEOUT)[0] == '/test/one.git'


def test_start_socket_listener_replaces_a_stale_socket_file(tree: GrokTree) -> None:
    # A socket left behind by a process that died without cleaning up: since
    # nothing is listening on it any more, start_socket_listener() must unlink
    # it and bind its own, rather than refusing like it does for a plain file.
    sockfile = tree.root / 'grok-pull.sock'
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sockfile))
    stale.close()
    grokmirror.write_manifest(str(tree.manifest), {'/test/one.git': {'fingerprint': 'deadbeef', 'modified': 5}})
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {'socket': str(sockfile)}})))
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.start_socket_listener(config, q_mani)

    tell(sockfile, b'/test/one.git\n')
    assert q_mani.get(timeout=TIMEOUT)[0] == '/test/one.git'
