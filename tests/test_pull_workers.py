# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-pull's worker functions, driven directly rather than through the daemon.

The daemon's own pacing (waiting out worker futures and lock retries) makes a
full run an expensive way to test a single decision, and some of these
decisions are only reachable when the state of a repository changes between
queueing an action and running it -- which is exactly what happens on a busy
mirror.
"""

from __future__ import annotations

import os
import queue
import time
from pathlib import Path

import pytest

import grokmirror
import grokmirror.pull

from support import BASE_TIMESTAMP, DECOY_URL, GrokTree, git


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
    config = tree.load_config()
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
    config = tree.load_config()
    tree.run_fsck('-f')

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
    config = tree.load_config()

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    q_spa.put(('/test/one.git', [action]))
    drain(config, q_spa)

    assert grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')


def test_an_unknown_action_is_ignored(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    config = tree.load_config()

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
    config = tree.load_config()

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
    config = tree.load_mirror_config(origin)
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
    fullpath, _remotename, config = tree.wire_mirror_repo(origin, '/test/one.git')
    repoinfo = origin.read_manifest()['/test/one.git']
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


def test_pull_worker_replaces_a_stale_directory_with_a_symlink(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    # A repo that used to be its own toplevel entry can become a symlink to
    # another one instead (its history got folded into a sibling upstream).
    # The stale directory has to go, or the mirror would keep two copies of
    # the same repository around under different names forever.
    tree.add_repo('test/one.git')
    tree.add_repo('test/link.git')
    config = tree.load_config({'pull': {}, 'remote': {'site': 'file:///dev/null'}})
    repoinfo: grokmirror.RepoInfo = {'symlinks': ['/test/link.git']}

    q_spa: queue.Queue[grokmirror.pull.SpaItem | None] = queue.Queue()
    with caplog.at_level('WARNING'):
        result = grokmirror.pull.pull_worker(
            grokmirror.GrokSession(), config, ('/test/one.git', repoinfo, 'fix_params', 'fix_params'), q_spa
        )

    assert result is True
    linkpath = tree.path('test/link.git')
    assert linkpath.is_symlink()
    assert os.path.realpath(linkpath) == os.fspath(tree.path('test/one.git'))
    assert 'because it is now a symlink' in caplog.text


@pytest.mark.parametrize('site', ['https://git.example.org/pub/scm', 'https://git.example.org/pub/scm/'])
def test_remote_url_is_joined_as_a_url(origin: GrokTree, tree: GrokTree, site: str) -> None:
    # [remote] site is a URL, and the remote URL used to be built with
    # os.path.join(), which is only ever right for one by coincidence: the
    # separator happens to match. Path() is not even that -- it collapses the
    # '//' in 'https://' (or 'file://') down to one slash, so a conversion to
    # pathlib would hand git a remote nothing can fetch from. Both spellings of
    # site, with and without a trailing slash, must give the same URL.
    config = tree.load_mirror_config(origin)

    fullpath = str(tree.path('test/one.git'))
    assert grokmirror.setup_bare_repo(fullpath)
    assert grokmirror.pull.fix_remotes(str(tree.toplevel), '/test/one.git', site, config)

    url = git('remote', 'get-url', '_grokmirror', cwd=tree.path('test/one.git')).strip()
    assert url == 'https://git.example.org/pub/scm/test/one.git'


# -- run_pull_action() ---------------------------------------------------------
#
# pull_worker()'s happy path above only ever exercises an initial clone. These
# drive run_pull_action() directly for the branches that only show up once a
# repository already exists: skipping a fetch on a fingerprint match, retrying
# (and eventually giving up) on a failing fetch, reorigining a repo whose
# remote went missing, the lazy vs. eager objstore fetch decision, the
# precious-repo repack skip, the agefile write, and objstore_migrate's
# unconditional spa actions.


def test_run_pull_action_skips_fetch_when_fingerprint_matches(
    origin: GrokTree, tree: GrokTree, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.add_repo('test/one.git')
    gitdir = '/test/one.git'
    fullpath, _remotename, config = tree.wire_mirror_repo(origin, gitdir)
    ses = grokmirror.GrokSession()

    # A real initial pull first, so the fingerprint we compare against below is
    # an actual content hash rather than two coincidentally-equal Nones. A
    # bogus starting fingerprint forces that first pull to happen at all: a
    # freshly bare-inited repo's real fingerprint is also None, and None ==
    # None would otherwise look like a match too.
    repoinfo: grokmirror.RepoInfo = {'fingerprint': 'bogus-forces-the-initial-pull'}
    assert grokmirror.pull.run_pull_action(ses, config, gitdir, fullpath, 'pull', repoinfo, [])
    matched_fp = repoinfo['fingerprint']

    calls: list[object] = []

    def record_call(*args: object, **_kwargs: object) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(grokmirror.pull, 'pull_repo', record_call)

    repoinfo2: grokmirror.RepoInfo = {'fingerprint': matched_fp}
    with caplog.at_level('DEBUG'):
        success = grokmirror.pull.run_pull_action(ses, config, gitdir, fullpath, 'pull', repoinfo2, [])

    assert success is True
    assert calls == []
    assert 'FP match, not pulling' in caplog.text


def test_run_pull_action_retries_before_giving_up(
    origin: GrokTree, tree: GrokTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin.add_repo('test/one.git')
    gitdir = '/test/one.git'
    fullpath, _remotename, config = tree.wire_mirror_repo(origin, gitdir, {'pull': {'retries': '2'}})

    calls = 0

    def always_fails(*_a: object, **_k: object) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(grokmirror.pull, 'pull_repo', always_fails)

    # A bogus fingerprint forces a fetch attempt: a freshly bare-inited repo's
    # own fingerprint is None, and an empty repoinfo's is also None, which
    # would otherwise look like a match and skip the fetch entirely.
    repoinfo: grokmirror.RepoInfo = {'fingerprint': 'bogus-forces-a-fetch-attempt'}
    success = grokmirror.pull.run_pull_action(grokmirror.GrokSession(), config, gitdir, fullpath, 'pull', repoinfo, [])

    assert success is False
    # One try plus [pull] retries additional ones, then give up.
    assert calls == 2
    # A failed fetch never reports a new fingerprint upstream.
    assert repoinfo['fingerprint'] == 'bogus-forces-a-fetch-attempt'


def test_run_pull_action_reorigins_before_fetching_if_the_remote_is_missing(origin: GrokTree, tree: GrokTree) -> None:
    origin.add_repo('test/one.git')
    origin.run_manifest()
    config = tree.load_mirror_config(origin)
    gitdir = '/test/one.git'
    fullpath = str(tree.path(gitdir))
    assert grokmirror.setup_bare_repo(fullpath)
    # Deliberately skip fix_remotes(): a freshly bare-inited repo has no remote
    # configured yet, same as one whose remote config got lost somehow.
    assert grokmirror.list_repo_remotes(fullpath) == []

    # A bogus fingerprint forces a fetch attempt, same reasoning as the retry
    # test above: two Nones would otherwise look like a match.
    repoinfo: grokmirror.RepoInfo = {'fingerprint': 'bogus-forces-a-fetch-attempt'}
    success = grokmirror.pull.run_pull_action(grokmirror.GrokSession(), config, gitdir, fullpath, 'pull', repoinfo, [])

    assert success is True
    assert '_grokmirror' in grokmirror.list_repo_remotes(fullpath)
    theirs = git('rev-parse', 'refs/heads/master', cwd=origin.path(gitdir)).strip()
    ours = git('rev-parse', 'refs/heads/master', cwd=tree.path(gitdir)).strip()
    assert ours == theirs


def test_run_pull_action_eagerly_fetches_objstore_when_the_alternate_is_still_empty(
    tree: GrokTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An objstore repo with nothing in it yet -- e.g. the very first fork of a
    # brand new fork group -- is fetched right away, rather than being left for
    # the spa, since other forks may be waiting on those objects.
    obstrepo = tree.objstore / 'fg1.git'
    git('init', '-q', '--bare', str(obstrepo))
    config = tree.load_config(sections={'pull': {}, 'remote': {'site': 'file:///dev/null'}})
    gitdir = '/test/fork.git'
    fullpath = str(tree.path(gitdir))
    assert grokmirror.setup_bare_repo(fullpath)
    grokmirror.set_altrepo(fullpath, str(obstrepo))
    git('remote', 'add', '_grokmirror', 'file:///dev/null', cwd=fullpath)
    # fetch_objstore_repo() only pulls from a sibling it already knows about --
    # normally wired up by grok-fsck when it groups a fork into the objstore.
    virtref = grokmirror.objstore_virtref(fullpath)
    git('remote', 'add', virtref, fullpath, cwd=obstrepo)

    source = tree.source('source')
    newref = source.commit(message='The fork group is new here too')

    def fake_pull_repo(*_a: object, **_k: object) -> bool:
        source.push(Path(fullpath))
        return True

    monkeypatch.setattr(grokmirror.pull, 'pull_repo', fake_pull_repo)

    spa_actions: list[str] = []
    repoinfo: grokmirror.RepoInfo = {'fingerprint': 'bogus-forces-a-fetch-attempt'}
    success = grokmirror.pull.run_pull_action(
        grokmirror.GrokSession(), config, gitdir, fullpath, 'pull', repoinfo, spa_actions
    )

    assert success is True
    assert 'objstore' not in spa_actions
    assert 'repack' in spa_actions
    assert newref in git('cat-file', '--batch-check', '--batch-all-objects', cwd=obstrepo)


@pytest.mark.slow
def test_run_pull_action_lazily_queues_objstore_when_the_alternate_already_has_objects(
    tree: GrokTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An objstore repo with objects already in it is fetched lazily, in the
    # spa, rather than right now: the eager-fetch path (empty objstore repo) is
    # covered by test_run_pull_action_eagerly_fetches_objstore_when_the_alternate_is_still_empty above.
    tree.add_repo('test/one.git')
    tree.add_repo('test/fork.git')
    tree.run_manifest()
    config = tree.load_config(sections={'pull': {}, 'remote': {'site': 'file:///dev/null'}})
    tree.run_fsck('-f')
    gitdir = '/test/fork.git'
    fullpath = str(tree.path(gitdir))
    obstrepo = tree.objstore_repos()[0]
    # Present a remote so run_pull_action() does not try to reorigin against
    # the fake site above.
    git('remote', 'add', '_grokmirror', 'file:///dev/null', cwd=fullpath)

    # Stand in for a real network fetch: push the new commit directly into the
    # "mirror" repo -- exactly what a successful pull_repo() would have left
    # behind -- at the moment run_pull_action() attempts the fetch, since the
    # fingerprint comparison that decides the objstore branch below is taken
    # from the repo's state before and after that one call.
    source = tree.source('source')
    newref = source.commit(message='New in the fork only')

    def fake_pull_repo(*_a: object, **_k: object) -> bool:
        source.push(Path(fullpath))
        return True

    monkeypatch.setattr(grokmirror.pull, 'pull_repo', fake_pull_repo)

    spa_actions: list[str] = []
    repoinfo: grokmirror.RepoInfo = {'fingerprint': 'stale-fingerprint-forcing-a-pull'}
    success = grokmirror.pull.run_pull_action(
        grokmirror.GrokSession(), config, gitdir, fullpath, 'pull', repoinfo, spa_actions
    )

    assert success is True
    assert spa_actions == ['objstore']
    assert newref not in git('cat-file', '--batch-check', '--batch-all-objects', cwd=obstrepo)


@pytest.mark.parametrize(('precious', 'expect_repack'), [(False, True), (True, False)])
def test_run_pull_action_skips_repack_for_a_precious_repo(
    origin: GrokTree, tree: GrokTree, monkeypatch: pytest.MonkeyPatch, precious: bool, expect_repack: bool
) -> None:
    origin.add_repo('test/one.git')
    gitdir = '/test/one.git'
    fullpath, _remotename, config = tree.wire_mirror_repo(origin, gitdir)
    if precious:
        git('config', 'extensions.preciousObjects', 'true', cwd=fullpath)
    # A freshly-fetched, single-commit test repo is far too small to trigger a
    # real repack on its own object counts, so force get_repack_level()'s
    # verdict to isolate the precious-repo check from the repack heuristics.
    monkeypatch.setattr(grokmirror, 'get_repack_level', lambda obj_info: 1)  # noqa: ARG005

    # A bogus fingerprint forces a fetch attempt, same reasoning as the retry
    # test above: two Nones would otherwise look like a match.
    repoinfo: grokmirror.RepoInfo = {'fingerprint': 'bogus-forces-a-fetch-attempt'}
    spa_actions: list[str] = []
    success = grokmirror.pull.run_pull_action(
        grokmirror.GrokSession(), config, gitdir, fullpath, 'pull', repoinfo, spa_actions
    )

    assert success is True
    assert ('repack' in spa_actions) is expect_repack


def test_run_pull_action_writes_the_agefile_when_modified_is_set(origin: GrokTree, tree: GrokTree) -> None:
    origin.add_repo('test/one.git')
    gitdir = '/test/one.git'
    fullpath, _remotename, config = tree.wire_mirror_repo(origin, gitdir)

    # A bogus fingerprint forces a fetch attempt, same reasoning as the retry
    # test above: two Nones would otherwise look like a match, and the agefile
    # is only written inside the "fingerprints differ" branch.
    repoinfo: grokmirror.RepoInfo = {'fingerprint': 'bogus-forces-a-fetch-attempt', 'modified': BASE_TIMESTAMP}
    success = grokmirror.pull.run_pull_action(grokmirror.GrokSession(), config, gitdir, fullpath, 'pull', repoinfo, [])

    assert success is True
    agefile = tree.path(gitdir) / 'info' / 'web' / 'last-modified'
    expected = time.strftime('%F %T', time.localtime(BASE_TIMESTAMP))
    assert agefile.read_text() == f'{expected}\n'


def test_run_pull_action_objstore_migrate_always_extends_spa_actions(tree: GrokTree) -> None:
    # Even when the fingerprint already matches, and there is nothing to pull,
    # an explicit objstore_migrate action still queues its spa treatments.
    tree.add_repo('test/one.git')
    config = tree.load_config(sections={'pull': {}, 'remote': {'site': 'file:///dev/null'}})
    gitdir = '/test/one.git'
    fullpath = str(tree.path(gitdir))
    my_fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), gitdir, force=True)
    repoinfo: grokmirror.RepoInfo = {'fingerprint': my_fp}

    spa_actions: list[str] = []
    success = grokmirror.pull.run_pull_action(
        grokmirror.GrokSession(), config, gitdir, fullpath, 'objstore_migrate', repoinfo, spa_actions
    )

    assert success is True
    assert spa_actions == ['objstore', 'repack']
