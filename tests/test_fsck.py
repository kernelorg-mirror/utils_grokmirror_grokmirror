# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-fsck repacks, fscks and repairs repositories, and runs objstore migration.

The objstore part is where the interesting failures are. Repositories that share
a root commit are treated as forks of each other and get migrated into a single
objstore repository, which they then use through git alternates. Anything that
opts out of that -- a private repository, one carrying the
grokmirror.do-not-objstore marker, one that was pulled back out by hand -- takes
a path that has historically been much less travelled.
"""

from __future__ import annotations

import json

import pytest

import grokmirror
from grokmirror.fsck import refresh_obst_roots

from support import GrokTree, git

pytestmark = pytest.mark.slow


def test_do_not_objstore_repo_is_left_alone(tree: GrokTree) -> None:
    # A repository with the marker and no alternates fell through into branches
    # that all assume altdir is set, reaching os.path.isdir(None) -> TypeError.
    # The whole fsck run died, so no repository got checked that night.
    tree.add_repo('test/one.git')
    tree.add_repo('test/fork.git')
    excluded = tree.add_repo('test/excluded.git')
    (excluded / 'grokmirror.do-not-objstore').touch()
    tree.run_manifest()
    tree.write_config()

    tree.run_fsck('-f')

    assert tree.alternates('test/excluded.git') is None
    # The other two are unaffected and still share an objstore repository.
    assert len(tree.objstore_repos()) == 1
    assert tree.alternates('test/one.git') == tree.alternates('test/fork.git')


def test_do_not_objstore_repo_with_a_single_sibling(tree: GrokTree) -> None:
    # The marked repository is the *only* other repo sharing these roots, so it
    # is also the only sibling candidate. Nothing should get migrated at all.
    tree.add_repo('test/one.git')
    excluded = tree.add_repo('test/excluded.git')
    (excluded / 'grokmirror.do-not-objstore').touch()
    tree.run_manifest()
    tree.write_config()

    tree.run_fsck('-f')

    assert tree.alternates('test/excluded.git') is None


def test_do_not_objstore_repo_with_dangling_alternates(tree: GrokTree) -> None:
    # The marker only short-circuits when there are no alternates. If the
    # alternates file points at something that is gone, fsck has to cope with
    # that too, rather than crashing on a path that does not exist.
    tree.add_repo('test/one.git')
    tree.add_repo('test/fork.git')
    excluded = tree.add_repo('test/excluded.git')
    (excluded / 'grokmirror.do-not-objstore').touch()
    altfile = excluded / 'objects' / 'info' / 'alternates'
    altfile.write_text(str(tree.objstore / 'nosuchrepo.git' / 'objects') + '\n')
    tree.run_manifest()
    tree.write_config()

    res = tree.run_fsck('-f')

    # The repository is scheduled for a reclone, which is the honest answer: its
    # objects are gone and only the origin can supply them again.
    assert 'reclone: /test/excluded.git' in res.stdout + res.stderr
    assert (excluded / 'grokmirror.reclone').exists()
    # The other two are unaffected.
    assert len(tree.objstore_repos()) == 1


def test_private_repo_contributes_no_objects(tree: GrokTree) -> None:
    # A private repository may still *use* the shared objstore -- that only lets
    # it avoid storing objects it can get from its siblings -- but none of its own
    # objects may end up there, so it is never added as a remote.
    tree.add_repo('test/one.git')
    tree.add_repo('test/fork.git')
    tree.add_repo('test/secret.git')
    tree.run_manifest()
    tree.write_config({'core': {'private': '/test/secret.git'}})

    tree.run_fsck('-f')

    assert tree.alternates('test/one.git') == tree.alternates('test/fork.git')
    (obstrepo,) = tree.objstore_repos()
    remotes = git('remote', cwd=obstrepo).split()
    assert len(remotes) == 2
    for remote in remotes:
        assert 'secret' not in git('remote', 'get-url', remote, cwd=obstrepo)


def test_no_fingerprint_is_recorded_for_a_repo_with_no_refs(tree: GrokTree) -> None:
    # get_repo_fingerprint() returns None for a repository with no refs, and that
    # None used to be written out as the literal string "None". Every reader then
    # accepted it as a valid fingerprint, because a non-empty string is truthy,
    # so the repository looked up to date forever.
    empty = tree.add_empty_repo('test/empty.git')

    assert grokmirror.set_repo_fingerprint(str(tree.toplevel), '/test/empty.git') is None
    assert not (empty / 'grokmirror.fingerprint').exists()


def test_fingerprint_is_recorded_once_there_are_refs(tree: GrokTree) -> None:
    repo = tree.add_repo('test/one.git')
    fpr = grokmirror.set_repo_fingerprint(str(tree.toplevel), '/test/one.git')

    assert fpr
    assert (repo / 'grokmirror.fingerprint').read_text() == fpr


def test_status_file_records_every_repo(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git', source='beta')
    tree.run_manifest()
    tree.write_config()
    tree.run_fsck('-f')

    # The status file is keyed by full path, not by the manifest's gitdir.
    status = json.loads(tree.statusfile.read_text())
    for gitdir in ('test/one.git', 'test/two.git'):
        entry = status[str(tree.path(gitdir))]
        assert entry['lastcheck']
        assert entry['nextcheck']
        assert entry['fingerprint'] == tree.read_manifest()[f'/{gitdir}']['fingerprint']


def test_second_run_checks_nothing_without_force(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.write_config()
    tree.run_fsck('-f')

    res = tree.run_fsck('-v')

    # Everything was just checked, so the next check is in the future.
    assert 'No repos need attention' in res.stdout + res.stderr


def test_emptied_objstore_repo_is_not_offered_as_a_sibling(tree: GrokTree) -> None:
    # refresh_obst_roots() drops an objstore repo from the roots map when it has
    # no roots left, instead of caching an empty entry. Every consumer skips
    # rootless repos anyway, so a cached empty entry meant an objstore repo whose
    # contents had just been migrated elsewhere could still be picked as a
    # sibling target.
    obst_roots: dict[str, set[str]] = {}
    obstrepo = str(tree.add_empty_repo('unused.git'))
    obst_roots[obstrepo] = {'0' * 40}

    assert not refresh_obst_roots(obst_roots, obstrepo)
    assert obstrepo not in obst_roots


def test_objstore_repo_roots_are_cached_when_present(tree: GrokTree) -> None:
    repo = str(tree.add_repo('test/one.git'))
    obst_roots: dict[str, set[str]] = {}
    roots = refresh_obst_roots(obst_roots, repo)

    assert roots
    assert obst_roots[repo] == roots


def test_unreachable_mailhost_does_not_lose_the_report(tree: GrokTree) -> None:
    # Anything logged at CRITICAL level gets mailed as a report, and the mail was
    # sent without a net: on a host with no local MTA, grok-fsck died with a
    # traceback after doing all the work, and the report went nowhere.
    repo = tree.add_repo('test/one.git')
    altfile = repo / 'objects' / 'info' / 'alternates'
    altfile.write_text(str(tree.objstore / 'nosuchrepo.git' / 'objects') + '\n')
    tree.run_manifest()
    tree.write_config()

    res = tree.run_fsck('-f')

    output = res.stdout + res.stderr
    assert 'Could not send the report' in output
    assert 'Report follows' in output
