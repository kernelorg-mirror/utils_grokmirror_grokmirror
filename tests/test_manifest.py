# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-manifest builds the manifest that describes an origin's repositories.

It runs in two modes: a full walk of the toplevel, and a per-repository update
called from a git hook. The per-repository mode is the one with the sharp edges,
because it does not purge and it trusts what the manifest already says.

Every test here runs with the current directory inside an unrelated decoy
repository, which is deliberate: several of these bugs were git commands invoked
with no repository path, which git answers from the current directory instead of
failing.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import grokmirror.manifest

from support import BASE_TIMESTAMP, DECOY_URL, GrokTree, git


def test_walks_the_toplevel(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git', source='beta')
    tree.run_manifest()

    manifest = tree.read_manifest()
    assert sorted(manifest) == ['/test/one.git', '/test/two.git']
    for entry in manifest.values():
        assert entry['fingerprint']
        assert entry['head'] == 'ref: refs/heads/master'
        assert entry['reference'] is None


def test_modified_is_the_latest_commit_timestamp(tree: GrokTree) -> None:
    # The timestamp comes from `git for-each-ref --format=%(committerdate:iso-strict)`,
    # which recent git versions render with a trailing 'Z' for UTC. Python's
    # fromisoformat() only accepts that from 3.11 on, so on everything older
    # grok-manifest raised ValueError on every single repository -- and the
    # fallback it had was written for Python 3.6 and caught AttributeError.
    source = tree.source('source', commits=3)
    tree.add_repo('test/one.git', source=source)
    tree.run_manifest()

    assert tree.read_manifest()['/test/one.git']['modified'] == BASE_TIMESTAMP + 3 * 60


def test_records_description_and_owner(tree: GrokTree) -> None:
    tree.add_repo('test/one.git', description='A test repository', owner='Tester')
    tree.run_manifest()

    entry = tree.read_manifest()['/test/one.git']
    assert entry['description'] == 'A test repository'
    assert entry['owner'] == 'Tester'


def test_ignores_a_repo_with_no_refs(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_empty_repo('test/empty.git')
    res = tree.run_manifest('-v')

    assert '/test/empty.git' not in tree.read_manifest()
    assert 'no heads' in res.stdout + res.stderr


def test_check_export_ok(tree: GrokTree) -> None:
    tree.add_repo('test/public.git', export_ok=True)
    tree.add_repo('test/private.git', source='beta')
    tree.run_manifest('-c')

    assert sorted(tree.read_manifest()) == ['/test/public.git']


def test_purges_a_repo_that_is_gone_from_disk(tree: GrokTree) -> None:
    # purge_manifest() called manifest.remove(), but manifest is a dict, so any
    # run that found a repository missing from disk died with AttributeError.
    # That has been broken since 2020, i.e. in every 2.x release.
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git', source='beta')
    tree.run_manifest()
    assert sorted(tree.read_manifest()) == ['/test/one.git', '/test/two.git']

    shutil.rmtree(tree.path('test/two.git'))
    res = tree.run_manifest('-p', '-v')

    assert sorted(tree.read_manifest()) == ['/test/one.git']
    assert 'purged /test/two.git' in res.stdout + res.stderr


def test_removing_a_repo_explicitly(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git', source='beta')
    tree.run_manifest()
    # -x takes full paths, the same as the per-repository hook mode.
    tree.run_manifest('-v', '-x', str(tree.path('test/two.git')))

    assert sorted(tree.read_manifest()) == ['/test/one.git']


def test_removing_a_symlink_explicitly(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_symlink('test/link.git', 'test/one.git')
    tree.run_manifest()
    assert tree.read_manifest()['/test/one.git']['symlinks'] == ['/test/link.git']

    tree.run_manifest('-v', '-x', str(tree.path('test/link.git')))

    # The whole key goes away once the last symlink is removed, rather than
    # being left as an empty list.
    assert 'symlinks' not in tree.read_manifest()['/test/one.git']


def test_a_repo_replaced_by_a_symlink_loses_its_own_entry(tree: GrokTree) -> None:
    # set_symlinks() compared manifest[gitdir] (an info dict) against a path
    # string, so the removal branch was dead: the replaced repository stayed in
    # the manifest as a repository of its own *and* showed up in the target's
    # symlinks. Clients then cloned the same thing twice.
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git', source='beta')
    tree.run_manifest()
    assert sorted(tree.read_manifest()) == ['/test/one.git', '/test/two.git']

    shutil.rmtree(tree.path('test/two.git'))
    tree.add_symlink('test/two.git', 'test/one.git')
    # The per-repository mode, i.e. what a post-update hook runs. It does not
    # purge, so this is the path where the dead branch mattered.
    tree.run_manifest('-v', str(tree.path('test/two.git')))

    manifest = tree.read_manifest()
    assert sorted(manifest) == ['/test/one.git']
    assert manifest['/test/one.git']['symlinks'] == ['/test/two.git']


def test_symlink_target_embedding_the_toplevel_path_is_still_outside(tmp_path: Path) -> None:
    # The outside-toplevel check used to be a string *containment* test, so a
    # target whose path merely contained the toplevel string somewhere in the
    # middle passed it, and set_symlinks() happily recorded a symlink entry
    # for a repository that lives outside the tree being mirrored. (The
    # simple case, a target sharing no path text with the toplevel, is
    # covered by test_symlink_pointing_outside_toplevel_is_ignored below.)
    toplevel = tmp_path / 'top'
    toplevel.mkdir()
    # An outside directory whose path embeds the toplevel path mid-string.
    evil_repo = Path(str(tmp_path / 'evil') + str(toplevel)) / 'foo.git'
    evil_repo.mkdir(parents=True)
    link = toplevel / 'link.git'
    link.symlink_to(evil_repo)

    key = '/' + os.path.relpath(evil_repo, toplevel)
    manifest: dict = {key: {}}
    grokmirror.manifest.set_symlinks(manifest, str(toplevel), [str(link)])

    assert 'symlinks' not in manifest[key]


def test_symlink_is_listed_on_a_full_walk(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_symlink('test/link.git', 'test/one.git')
    tree.run_manifest()

    manifest = tree.read_manifest()
    assert sorted(manifest) == ['/test/one.git']
    assert manifest['/test/one.git']['symlinks'] == ['/test/link.git']


def test_broken_symlink_is_ignored(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    # A full walk never finds a broken symlink, since it cannot look like a git
    # repository, so this needs the per-repository mode -- which is exactly what
    # a hook does after the symlink target was removed.
    tree.add_symlink('test/link.git', 'test/nowhere.git')
    res = tree.run_manifest('-v', str(tree.path('test/link.git')))

    assert sorted(tree.read_manifest()) == ['/test/one.git']
    assert 'is broken' in res.stdout + res.stderr


def test_symlink_pointing_outside_toplevel_is_ignored(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    linkpath = tree.path('test/outside.git')
    linkpath.symlink_to(tree.decoy)
    res = tree.run_manifest('-v', str(linkpath))

    assert sorted(tree.read_manifest()) == ['/test/one.git']
    assert 'points outside toplevel' in res.stdout + res.stderr


@pytest.mark.slow
def test_reference_is_not_harvested_from_the_current_directory(tree: GrokTree) -> None:
    # A repository that left the objstore keeps its forkgroup in the manifest but
    # has no alternates any more, so get_altrepo() returns None. That None went
    # straight into list_repo_remotes(), i.e. `git remote -v` with no repository
    # path -- which git answers from the current directory. The manifest then
    # recorded a "reference" pointing at whatever repo the cron job happened to
    # be standing in.
    tree.add_repo('test/one.git')
    tree.add_repo('test/fork.git')
    tree.run_manifest()
    tree.write_config()
    tree.run_fsck('-f')

    # Both are in the objstore now, and the manifest knows their forkgroup.
    assert tree.read_manifest()['/test/fork.git']['forkgroup']
    assert tree.alternates('test/fork.git')

    # Now take one back out, the way an admin would: repack it standalone and
    # drop the alternates, leaving the manifest entry stale.
    git('repack', '-a', '-d', '-q', cwd=tree.path('test/fork.git'))
    (tree.path('test/fork.git') / 'objects' / 'info' / 'alternates').unlink()
    tree.run_manifest('-v', str(tree.path('test/fork.git')))

    entry = tree.read_manifest()['/test/fork.git']
    assert entry['reference'] is None
    # And nothing from the decoy repository the command was run in.
    assert DECOY_URL not in str(entry)


def test_manifest_with_no_directory_component(tree: GrokTree) -> None:
    # os.path.dirname('manifest.js.gz') is '', and os.makedirs('') raises, so
    # the lock could not be taken and grok-manifest died before doing any work.
    # Running from the manifest's own directory is a plausible cron setup.
    tree.add_repo('test/one.git')
    tree.run('grok-manifest', '-m', 'manifest.js.gz', '-t', str(tree.toplevel), cwd=tree.root)

    assert sorted(tree.read_manifest(tree.root / 'manifest.js.gz')) == ['/test/one.git']
