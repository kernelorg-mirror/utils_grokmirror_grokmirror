# SPDX-License-Identifier: GPL-3.0-or-later
"""contrib/pubsubv1.py is a Google pubsub v1 push listener for grok-pull.

It is not part of the installed package, so it has to be loaded from its path.
It was broken outright with falcon 3.0 and newer, which removed Response.body:
every single code path raised AttributeRemovedError, so the listener answered
nothing but 500s no matter what you sent it.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

falcon = pytest.importorskip('falcon')
from falcon import testing

CONTRIB = Path(__file__).resolve().parent.parent / 'contrib' / 'pubsubv1.py'


@pytest.fixture(scope='module')
def pubsubv1() -> Any:
    # The loader is built explicitly rather than via spec_from_file_location(),
    # whose spec.loader is typed as the base Loader, which promises nothing.
    loader = importlib.machinery.SourceFileLoader('pubsubv1', str(CONTRIB))
    spec = importlib.util.spec_from_loader('pubsubv1', loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules['pubsubv1'] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def client(pubsubv1: Any) -> testing.TestClient:
    return testing.TestClient(pubsubv1.app)


def payload(proj: str = 'test', repo: str = '/test/one.git') -> dict[str, Any]:
    return {'message': {'attributes': {'proj': proj, 'repo': repo}}}


@pytest.fixture
def listening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[socket.socket]:
    """A config directory with a test.conf pointing at a live unix socket."""
    sockfile = tmp_path / 'pull.sock'
    (tmp_path / 'test.conf').write_text(f'[pull]\nsocket = {sockfile}\n')
    monkeypatch.setenv('GROKMIRROR_CONFIG_DIR', str(tmp_path))
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sockfile))
    listener.listen(1)
    yield listener
    listener.close()


def test_a_valid_payload_reaches_the_socket(client: testing.TestClient, listening: socket.socket) -> None:
    res = client.simulate_post('/pubsub_v1', json=payload())

    assert res.status_code == 204
    conn, _addr = listening.accept()
    with conn:
        # Newline-terminated, because the daemon reads the socket with
        # readline() and must not have to rely on our close for the last line
        assert conn.recv(1024).decode() == '/test/one.git\n'


def test_get_is_refused_politely(client: testing.TestClient) -> None:
    res = client.simulate_get('/pubsub_v1')

    # 200 with an explanation, not a traceback: this endpoint is public.
    assert res.status_code == 200
    assert "don't serve GETs" in res.text


def test_no_payload(client: testing.TestClient) -> None:
    res = client.simulate_post('/pubsub_v1')

    assert res.status_code == 500
    assert res.text == 'Payload required\n'


def test_unparseable_payload(client: testing.TestClient) -> None:
    res = client.simulate_post('/pubsub_v1', body='{not json')

    assert res.status_code == 500
    assert res.text == 'Failed to parse payload as json\n'


@pytest.mark.parametrize(
    'body',
    [
        pytest.param({}, id='empty-object'),
        pytest.param({'message': {}}, id='no-attributes'),
        pytest.param({'message': {'attributes': {'proj': 'test'}}}, id='no-repo'),
        pytest.param({'message': 'a string'}, id='message-is-not-an-object'),
        pytest.param([1, 2, 3], id='not-an-object-at-all'),
    ],
)
def test_payload_that_is_not_pubsub_v1(client: testing.TestClient, body: Any) -> None:
    res = client.simulate_post('/pubsub_v1', body=json.dumps(body))

    assert res.status_code == 500
    assert res.text == 'Not a pubsub v1 payload\n'


def test_overlong_values(client: testing.TestClient, pubsubv1: Any) -> None:
    res = client.simulate_post('/pubsub_v1', json=payload(proj='p' * (pubsubv1.MAX_PROJ_LEN + 1)))

    assert res.status_code == 500
    assert res.text == 'Repo or project value too long\n'


@pytest.mark.parametrize('proj', ['../etc/shadow', 'two words'])
def test_project_names_are_restricted(client: testing.TestClient, proj: str) -> None:
    # The project name becomes part of a path, so a slash in it is a traversal.
    res = client.simulate_post('/pubsub_v1', json=payload(proj=proj))

    assert res.status_code == 500
    assert res.text == 'Invalid characters in project name\n'


@pytest.mark.parametrize(
    'repo',
    [
        pytest.param('/test/one.git\n/test/two.git', id='embedded-newline'),
        pytest.param('/test/one.git two.git', id='space'),
    ],
)
def test_repo_names_are_restricted(client: testing.TestClient, listening: socket.socket, repo: str) -> None:
    # The daemon reads its socket a line at a time, so a newline here used to
    # queue one repo per line from a single message
    res = client.simulate_post('/pubsub_v1', json=payload(repo=repo))

    assert res.status_code == 500
    assert res.text == 'Invalid characters in repo name\n'
    listening.settimeout(0.2)
    with pytest.raises(socket.timeout):
        listening.accept()


def test_unknown_project(client: testing.TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GROKMIRROR_CONFIG_DIR', str(tmp_path))
    res = client.simulate_post('/pubsub_v1', json=payload(proj='nosuchproj'))

    assert res.status_code == 500
    assert res.text == 'Invalid project name\n'


def test_project_with_no_socket_configured(
    client: testing.TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / 'test.conf').write_text('[core]\ntoplevel = /var/lib/git\n')
    monkeypatch.setenv('GROKMIRROR_CONFIG_DIR', str(tmp_path))
    res = client.simulate_post('/pubsub_v1', json=payload())

    assert res.status_code == 500
    assert 'no socket defined' in res.text


def test_socket_that_is_not_there(client: testing.TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / 'test.conf').write_text(f'[pull]\nsocket = {tmp_path}/nope.sock\n')
    monkeypatch.setenv('GROKMIRROR_CONFIG_DIR', str(tmp_path))
    res = client.simulate_post('/pubsub_v1', json=payload())

    assert res.status_code == 500
    assert 'socket does not exist' in res.text


def test_socket_that_nobody_is_listening_on(
    client: testing.TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The daemon died but left its socket behind, which is a plausible state and
    # must not take the listener down with it.
    sockfile = tmp_path / 'pull.sock'
    orphan = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    orphan.bind(str(sockfile))
    orphan.close()
    (tmp_path / 'test.conf').write_text(f'[pull]\nsocket = {sockfile}\n')
    monkeypatch.setenv('GROKMIRROR_CONFIG_DIR', str(tmp_path))

    res = client.simulate_post('/pubsub_v1', json=payload())

    assert res.status_code == 500
    assert res.text == 'Unable to communicate with the socket\n'
