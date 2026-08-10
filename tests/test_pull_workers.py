# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-pull's worker functions, driven directly rather than through the daemon.

The daemon's own pacing (five-second sleeps between polls) makes a full run an
expensive way to test a single decision, and some of these decisions are only
reachable when the state of a repository changes between queueing an action and
running it -- which is exactly what happens on a busy mirror.
"""

from __future__ import annotations

import multiprocessing as mp

import pytest

import grokmirror
import grokmirror.pull

from support import DECOY_URL, GrokTree, git


def drain(config: grokmirror.GrokConfigParser, q_spa: mp.Queue) -> None:
    """Run spa_worker until the queue is empty; it exits by calling sys.exit()."""
    with pytest.raises(SystemExit) as excinfo:
        grokmirror.pull.spa_worker(config, q_spa, pauseonload=False)
    assert excinfo.value.code == 0


def test_objstore_action_on_a_repo_with_no_alternates(
    tree: GrokTree, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The objstore action re-reads get_altrepo() when it runs, so a repository
    # that lost (or never had) alternates in between yields None -- and that None
    # went straight into fetch_objstore_repo(), which runs git with no repository
    # path, i.e. against the current working directory. Same class of bug as the
    # reference grok-manifest used to harvest from the cwd.
    tree.add_repo('test/one.git')
    cfgfile = tree.write_config()
    config = grokmirror.load_config_file(str(cfgfile))
    monkeypatch.chdir(tree.decoy)

    q_spa: mp.Queue = mp.Queue()
    q_spa.put(('/test/one.git', ['objstore']))
    with caplog.at_level('DEBUG'):
        drain(config, q_spa)

    assert 'no alternates, skipping objstore fetch' in caplog.text
    # Nothing was fetched into the repository we happened to be standing in.
    assert git('remote', cwd=tree.decoy).split() == ['origin']
    assert DECOY_URL not in caplog.text
    assert git('for-each-ref', '--format=%(refname)', cwd=tree.decoy).split() == ['refs/heads/decoybranch']


@pytest.mark.slow
def test_objstore_action_fetches_when_alternates_are_set(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_repo('test/fork.git')
    tree.run_manifest()
    cfgfile = tree.write_config()
    tree.run_fsck('-f')
    config = grokmirror.load_config_file(str(cfgfile))

    # A new commit that only exists in the toplevel repo, not in the objstore.
    source = tree.source('source')
    newref = source.commit(message='Only in the fork')
    source.push(tree.path('test/fork.git'))
    obstrepo = tree.objstore_repos()[0]
    assert newref not in git('cat-file', '--batch-check', '--batch-all-objects', cwd=obstrepo)

    q_spa: mp.Queue = mp.Queue()
    q_spa.put(('/test/fork.git', ['objstore']))
    drain(config, q_spa)

    assert newref in git('cat-file', '--batch-check', '--batch-all-objects', cwd=obstrepo)


@pytest.mark.parametrize('action', ['repack', 'fingerprint'])
def test_simple_actions_run(tree: GrokTree, action: str) -> None:
    tree.add_repo('test/one.git')
    cfgfile = tree.write_config()
    config = grokmirror.load_config_file(str(cfgfile))

    q_spa: mp.Queue = mp.Queue()
    q_spa.put(('/test/one.git', [action]))
    drain(config, q_spa)

    assert grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')


def test_an_unknown_action_is_ignored(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    cfgfile = tree.write_config()
    config = grokmirror.load_config_file(str(cfgfile))

    q_spa: mp.Queue = mp.Queue()
    q_spa.put(('/test/one.git', ['no-such-action']))
    drain(config, q_spa)
