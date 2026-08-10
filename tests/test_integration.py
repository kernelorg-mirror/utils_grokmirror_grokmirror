# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end flows, exercised over real repositories.

These are the two things grokmirror is actually for -- an origin publishing a
manifest, and a mirror cloning from it -- plus grok-fsck's object storage
migration. They are broad on purpose: they are the tests that would notice a
regression anywhere along the main path, and they are what the narrower
regression tests are built on top of.
"""

from __future__ import annotations

import shutil

import pytest

from support import DECOY_URL, GrokTree, git

# These build and repack real repositories, so they are the slow part of the
# suite: run just the quick checks with `pytest -m "not slow"`.
pytestmark = pytest.mark.slow


def test_mirror_clones_from_origin(origin: GrokTree, tree: GrokTree) -> None:
    origin.add_repo('test/one.git')
    origin.add_repo('test/two.git', source='beta')
    origin.run_manifest()

    tree.write_mirror_config(origin)
    res = tree.run_pull('-v')

    for gitdir in ('test/one.git', 'test/two.git'):
        assert tree.path(gitdir).is_dir(), res.stdout
        assert git('rev-parse', 'refs/heads/master', cwd=tree.path(gitdir)).strip()
        # The mirror's own manifest describes what it actually has on disk.
        assert gitdir in ' '.join(tree.read_manifest().keys())
    assert (tree.root / 'projects.list').exists()
    # Same history on both ends.
    for gitdir in ('test/one.git', 'test/two.git'):
        theirs = git('rev-parse', 'refs/heads/master', cwd=origin.path(gitdir)).strip()
        ours = git('rev-parse', 'refs/heads/master', cwd=tree.path(gitdir)).strip()
        assert ours == theirs


def test_mirror_second_run_is_a_no_op(origin: GrokTree, tree: GrokTree) -> None:
    origin.add_repo('test/one.git')
    origin.run_manifest()
    tree.write_mirror_config(origin)
    tree.run_pull()

    before = tree.read_manifest()
    tree.run_pull('-v')
    assert tree.read_manifest() == before


def test_mirror_picks_up_new_commits(origin: GrokTree, tree: GrokTree) -> None:
    origin.add_repo('test/one.git')
    origin.run_manifest()
    tree.write_mirror_config(origin)
    tree.run_pull()

    # New commit on the origin, manifest regenerated, mirror pulls again.
    source = origin.source('source')
    source.commit()
    source.push(origin.path('test/one.git'))
    origin.run_manifest()
    tree.run_pull('-n')

    assert git('rev-parse', 'refs/heads/master', cwd=tree.path('test/one.git')).strip() == source.head()


def test_mirror_purges_removed_repos(origin: GrokTree, tree: GrokTree) -> None:
    origin.add_repo('test/one.git')
    origin.add_repo('test/two.git', source='beta')
    origin.run_manifest()
    tree.write_mirror_config(origin)
    tree.run_pull()

    # Take the repository off the origin the way an admin would, then let the
    # origin's own purge drop it from the manifest.
    shutil.rmtree(origin.path('test/two.git'))
    origin.run_manifest('-p')
    assert sorted(origin.read_manifest().keys()) == ['/test/one.git']

    tree.run_pull('-n', '-p', '--force-purge')

    assert tree.path('test/one.git').is_dir()
    assert not tree.path('test/two.git').exists()


def test_fsck_migrates_forks_into_objstore(tree: GrokTree) -> None:
    # Two repositories sharing a root commit are forks as far as grokmirror is
    # concerned, and get merged into one objstore repository they both use as an
    # alternate. This is the feature the whole 2.0 rewrite was about.
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git')
    tree.run_manifest()
    tree.write_config()
    tree.run_fsck('-f', '-v')

    obstrepos = tree.objstore_repos()
    assert len(obstrepos) == 1, f'expected one objstore repo, got {obstrepos}'
    obstrepo = obstrepos[0]
    for gitdir in ('test/one.git', 'test/two.git'):
        assert tree.alternates(gitdir) == str(obstrepo / 'objects')
    # The objstore repo keeps the forks' objects under refs/virtual/.
    virtrefs = git('for-each-ref', '--format=%(refname)', 'refs/virtual/', cwd=obstrepo).split()
    assert len(virtrefs) >= 2, virtrefs


def test_fsck_leaves_unrelated_repos_alone(tree: GrokTree) -> None:
    tree.add_repo('test/one.git', source='alpha')
    tree.add_repo('test/two.git', source='beta')
    tree.run_manifest()
    tree.write_config()
    tree.run_fsck('-f')

    # Unrelated histories have no siblings to share with, so there is nothing
    # to gain from an objstore repo and none is created.
    assert tree.objstore_repos() == []
    assert tree.alternates('test/one.git') is None
    assert tree.alternates('test/two.git') is None


def test_fsck_writes_a_status_file(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.write_config()
    tree.run_fsck('-f')

    assert tree.statusfile.exists()


def test_nothing_leaks_from_the_current_directory(origin: GrokTree, tree: GrokTree) -> None:
    # A blunt catch-all for the "git was asked about no repository" class of
    # bug: after a full origin-to-mirror cycle, nothing anywhere may mention
    # the decoy repository the commands were run from.
    origin.add_repo('test/one.git')
    origin.run_manifest()
    tree.write_mirror_config(origin)
    tree.run_pull('-v')
    tree.run_fsck('-f')

    assert DECOY_URL not in str(tree.read_manifest())
    assert DECOY_URL not in tree.log_text()
    assert 'decoy' not in str(tree.read_manifest())
