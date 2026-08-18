# SPDX-License-Identifier: GPL-3.0-or-later
"""manifest_worker(), the thread pull_mirror() restarts on every refresh cycle
to go fetch the remote manifest and queue up whatever changed.

Never exercised directly: test_config_errors.py drives the same OSError/
GrokManifestError path, but only through a full `grok-pull` process, where a
crash in this thread would already be invisible to the caller. What actually
matters here is that this function itself never lets that exception escape --
a thread that dies silently would just stop refreshing forever, with nothing
in the exit code to say so.
"""

from __future__ import annotations

import queue

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree


def test_successful_run_queues_updates_and_logs_the_pacing_line(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    tree.add_repo('test/one.git')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest({'/test/one.git': {'fingerprint': fp}}, remote_manifest_path)
    tree.write_manifest({}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    with caplog.at_level('INFO'):
        grokmirror.pull.manifest_worker(ses, config, q_mani)

    assert q_mani.get_nowait()[0] == '/test/one.git'
    assert ' manifest: sleeping' in caplog.text
    assert 'CRITICAL' not in caplog.text


def test_a_missing_remote_manifest_is_caught_and_logged_critically_not_raised(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    config = tree.load_remote_config(tree.root / 'does-not-exist.json')
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    with caplog.at_level('INFO'):
        # Must not raise: a dead thread here would silently stop every future
        # manifest refresh with nothing in the daemon's exit code to show it.
        grokmirror.pull.manifest_worker(ses, config, q_mani)

    assert q_mani.qsize() == 0
    assert any(r.levelname == 'CRITICAL' and 'Could not get the remote manifest' in r.message for r in caplog.records)
