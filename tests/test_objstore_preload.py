# SPDX-License-Identifier: GPL-3.0-or-later
"""objstore_repo_preload(), grok-pull's HTTP shortcut for a fresh objstore repo.

Cloning a big fork group from scratch over git's normal wire protocol can be
slow, so an origin may publish a pre-built bundle of the objstore repo at
[remote] preload_bundle_url and let a mirror fetch that over plain HTTP
instead. Nothing about this had direct test coverage before this file: it is
the one part of grok-pull that talks to an external HTTP server, so a real
(local) one is used here rather than mocking `requests` out -- the retry
adapter, streaming download and error handling are exactly the parts worth
exercising for real.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree, git


@contextlib.contextmanager
def bundle_server(servedir: Path) -> Iterator[str]:
    """Serve `servedir` over HTTP on loopback and yield its base URL."""

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

    handler = functools.partial(QuietHandler, directory=str(servedir))
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)


def test_does_nothing_when_no_preload_url_is_configured(tree: GrokTree) -> None:
    obstrepo = tree.objstore / 'fg1.git'
    git('init', '-q', '--bare', str(obstrepo))
    config = grokmirror.load_config_file(str(tree.write_config(sections={'remote': {}})))
    ses = grokmirror.GrokSession()

    grokmirror.pull.objstore_repo_preload(ses, config, str(obstrepo))

    assert git('remote', cwd=obstrepo).strip() == ''
    assert not (tree.objstore / 'fg1.bundle').exists()


def test_downloads_and_preloads_a_real_bundle(tree: GrokTree) -> None:
    source = tree.source()
    with bundle_server(tree.root) as base_url:
        git('bundle', 'create', str(tree.root / 'fg1.bundle'), '--all', cwd=source.path)
        obstrepo = tree.objstore / 'fg1.git'
        git('init', '-q', '--bare', str(obstrepo))
        config = grokmirror.load_config_file(
            str(tree.write_config(sections={'remote': {'preload_bundle_url': base_url}}))
        )
        ses = grokmirror.GrokSession()

        grokmirror.pull.objstore_repo_preload(ses, config, str(obstrepo))

    refs = git('for-each-ref', '--format=%(refname)', cwd=obstrepo).split()
    assert f'refs/heads/{source.branch}' in refs
    assert git('rev-parse', f'refs/heads/{source.branch}', cwd=obstrepo).strip() == source.head()
    # The scratch remote and the downloaded bundle are both cleaned up
    # regardless of outcome, success included.
    assert git('remote', cwd=obstrepo).strip() == ''
    assert not (tree.objstore / 'fg1.bundle').exists()


def test_falls_back_to_a_normal_clone_when_the_download_fails(tree: GrokTree) -> None:
    # An empty server directory means every request 404s: raise_for_status()
    # turns that into an exception, which the broad except must catch and
    # clean up after, leaving the objstore repo untouched for a normal clone.
    obstrepo = tree.objstore / 'fg1.git'
    git('init', '-q', '--bare', str(obstrepo))
    with bundle_server(tree.root) as base_url:
        config = grokmirror.load_config_file(
            str(tree.write_config(sections={'remote': {'preload_bundle_url': base_url}}))
        )
        ses = grokmirror.GrokSession()

        grokmirror.pull.objstore_repo_preload(ses, config, str(obstrepo))

    assert git('remote', cwd=obstrepo).strip() == ''
    assert not (tree.objstore / 'fg1.bundle').exists()


def test_falls_back_when_the_downloaded_file_is_not_a_valid_bundle(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    # The download itself can succeed against a stale or truncated bundle: the
    # failure only shows up once git tries to fetch from it, in the "remote
    # update" step, not in the "remote add" step (which never validates the
    # url) -- that later, separate failure path is exercised here.
    (tree.root / 'fg1.bundle').write_text('not a real bundle file\n')
    obstrepo = tree.objstore / 'fg1.git'
    git('init', '-q', '--bare', str(obstrepo))
    with bundle_server(tree.root) as base_url:
        config = grokmirror.load_config_file(
            str(tree.write_config(sections={'remote': {'preload_bundle_url': base_url}}))
        )
        ses = grokmirror.GrokSession()

        with caplog.at_level('INFO'):
            grokmirror.pull.objstore_repo_preload(ses, config, str(obstrepo))

    assert 'failed to preload' in caplog.text
    assert git('for-each-ref', cwd=obstrepo).strip() == ''
    assert git('remote', cwd=obstrepo).strip() == ''
    assert not (tree.objstore / 'fg1.bundle').exists()
