# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-pull's worker functions, driven directly rather than through the daemon.

The daemon's own pacing (waiting out worker futures and lock retries) makes a
full run an expensive way to test a single decision, and some of these
decisions are only reachable when the state of a repository changes between
queueing an action and running it -- which is exactly what happens on a busy
mirror.
"""

from __future__ import annotations

import queue

import pytest

import grokmirror
import grokmirror.pull

from support import DECOY_URL, GrokTree, git


def drain(config: grokmirror.GrokConfigParser, q_spa: queue.Queue[grokmirror.pull.SpaItem | None]) -> None:
    """Run spa_worker until the queue is empty; a None sentinel makes it exit."""
    q_spa.put(None)
    grokmirror.pull.spa_worker(config, q_spa, pauseonload=False)
    # Every item, including the sentinel, must have been accounted for, or
    # pull_mirror()'s closing q_spa.join() would hang forever.
    assert q_spa.unfinished_tasks == 0


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

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
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

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    q_spa.put(('/test/fork.git', ['objstore']))
    drain(config, q_spa)

    assert newref in git('cat-file', '--batch-check', '--batch-all-objects', cwd=obstrepo)


@pytest.mark.parametrize('action', ['repack', 'fingerprint'])
def test_simple_actions_run(tree: GrokTree, action: str) -> None:
    tree.add_repo('test/one.git')
    cfgfile = tree.write_config()
    config = grokmirror.load_config_file(str(cfgfile))

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    q_spa.put(('/test/one.git', [action]))
    drain(config, q_spa)

    assert grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')


def test_an_unknown_action_is_ignored(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    cfgfile = tree.write_config()
    config = grokmirror.load_config_file(str(cfgfile))

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    q_spa.put(('/test/one.git', ['no-such-action']))
    drain(config, q_spa)


def test_spa_worker_survives_a_failing_treatment(
    tree: GrokTree, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The spa runs as a single thread for the whole life of the daemon. An
    # unexpected exception during a treatment must not take it down: with the
    # old process pool a dead spa worker was silently respawned, but a dead
    # thread would leave every later spa action sitting in the queue forever,
    # and a --runonce drain would hang on q_spa.join(). The failed item still
    # has to be accounted with task_done(), and later items still have to run.
    tree.add_repo('test/one.git')
    cfgfile = tree.write_config()
    config = grokmirror.load_config_file(str(cfgfile))

    real_spa_repo = grokmirror.pull._spa_repo

    def flaky(
        config: grokmirror.GrokConfigParser, toplevel: str, gitdir: str, actions: list[str], waiting: int = 0
    ) -> None:
        if gitdir == '/test/boom.git':
            raise RuntimeError('injected treatment failure')
        real_spa_repo(config, toplevel, gitdir, actions, waiting)

    monkeypatch.setattr(grokmirror.pull, '_spa_repo', flaky)

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    q_spa.put(('/test/boom.git', ['fingerprint']))
    q_spa.put(('/test/one.git', ['fingerprint']))
    drain(config, q_spa)

    assert 'injected treatment failure' in caplog.text
    # The item behind the failed one was still treated.
    assert grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')


def test_pull_worker_defers_a_locked_repo(
    origin: GrokTree, tree: GrokTree, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A repository that is already locked is not a failure: the worker reports
    # None so the supervisor puts the item back in line, and it naps first so
    # the retry round-trip doesn't spin hot. The lock is taken here in the same
    # process, which the registry refuses exactly like a lock held by another
    # grokmirror process on the same repository.
    origin.add_repo('test/one.git')
    origin.run_manifest()
    cfgfile = tree.write_mirror_config(origin)
    config = grokmirror.load_config_file(str(cfgfile))
    repoinfo = origin.read_manifest()['/test/one.git']

    naps: list[float] = []
    monkeypatch.setattr('time.sleep', naps.append)

    fullpath = str(tree.path('test/one.git'))
    tree.path('test/one.git').parent.mkdir(parents=True, exist_ok=True)
    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    with grokmirror.locked_repo(fullpath), caplog.at_level('INFO'):
        result = grokmirror.pull.pull_worker(
            grokmirror.GrokSession(), config, ('/test/one.git', repoinfo, 'pull', 'pull'), q_spa
        )

    assert result is None
    assert naps == [5]
    assert 'defer: /test/one.git' in caplog.text
    # Nothing was pulled and nothing went to the spa.
    assert not tree.path('test/one.git').exists()
    assert q_spa.qsize() == 0


def test_pull_worker_pulls_and_queues_spa_actions(origin: GrokTree, tree: GrokTree) -> None:
    # The happy path: a fresh bare repository (the supervisor's init step,
    # minus the daemon around it) gets its objects fetched from the origin,
    # and the initial-clone treatments land in the spa queue.
    origin.add_repo('test/one.git')
    origin.run_manifest()
    cfgfile = tree.write_mirror_config(origin)
    config = grokmirror.load_config_file(str(cfgfile))
    repoinfo = origin.read_manifest()['/test/one.git']

    fullpath = str(tree.path('test/one.git'))
    assert grokmirror.setup_bare_repo(fullpath)
    assert grokmirror.pull.fix_remotes(str(tree.toplevel), '/test/one.git', config['remote']['site'], config)
    grokmirror.pull.set_repo_params(fullpath, repoinfo)

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    result = grokmirror.pull.pull_worker(
        grokmirror.GrokSession(), config, ('/test/one.git', repoinfo, 'pull', 'init'), q_spa
    )

    assert result is True
    theirs = git('rev-parse', 'refs/heads/master', cwd=origin.path('test/one.git')).strip()
    ours = git('rev-parse', 'refs/heads/master', cwd=tree.path('test/one.git')).strip()
    assert ours == theirs
    # An initial clone queues the pack-all-refs treatment for the spa.
    queued = q_spa.get_nowait()
    assert queued is not None
    (gitdir, spa_actions) = queued
    assert gitdir == '/test/one.git'
    assert 'packrefs-all' in spa_actions
    # The lock was released for the next action on this repository.
    assert fullpath not in grokmirror.REPO_LOCKH
