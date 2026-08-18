# SPDX-License-Identifier: GPL-3.0-or-later
"""fill_todo_from_manifest(), the decision tree behind every grok-pull action.

test_integration.py only ever exercises the two simplest branches end to end:
a brand-new clone (no forkgroup) and a full pull triggered by a new remote
commit. Everything else in the decision tree -- reclone markers, repos that
exist on disk but are unknown to our own manifest, description/owner/head or
symlink drift, a forced pull driven by *our own* stale bookkeeping rather than
a remote change, and the three ways a forkgroup can be populated (an existing
objstore repo, an unmigrated sibling on disk, or a private repo with public
siblings) -- had no coverage at any level. These tests call the function
directly against a real GrokTree and inspect the resulting q_mani queue.
"""

from __future__ import annotations

import queue

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree


def actions(q_mani: queue.Queue[grokmirror.pull.ManiItem]) -> list[tuple[str, str]]:
    return [(gitdir, action) for gitdir, _repoinfo, action in list(q_mani.queue)]


def test_reclone_marker_file_queues_a_reclone_and_nothing_else(tree: GrokTree) -> None:
    fullpath = tree.add_repo('test/one.git')
    (fullpath / 'grokmirror.reclone').write_text('grok-fsck asked for this\n')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest({'/test/one.git': {'fingerprint': fp}}, remote_manifest_path)
    tree.write_manifest({'/test/one.git': {'fingerprint': fp}}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    assert actions(q_mani) == [('/test/one.git', 'reclone')]


def test_repo_on_disk_but_unknown_to_local_manifest_queues_fix_remotes(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest({'/test/one.git': {'fingerprint': fp}}, remote_manifest_path)
    tree.write_manifest({}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    assert actions(q_mani) == [('/test/one.git', 'fix_remotes')]


def test_description_mismatch_queues_fix_params_only(tree: GrokTree) -> None:
    tree.add_repo('test/one.git', description='old description')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')
    common = {'fingerprint': fp, 'owner': None, 'head': 'refs/heads/master'}

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest({'/test/one.git': {**common, 'description': 'new description'}}, remote_manifest_path)
    tree.write_manifest({'/test/one.git': {**common, 'description': 'old description'}}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    # Fingerprints match on both sides, so nothing but the params mismatch
    # should produce an action -- a forced pull here would hide the fact that
    # this branch has its own, independent trigger.
    assert actions(q_mani) == [('/test/one.git', 'fix_params')]


def test_symlink_mismatch_queues_fix_params_only(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')
    entry = {'fingerprint': fp, 'description': None, 'owner': None, 'head': 'refs/heads/master'}

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest({'/test/one.git': {**entry, 'symlinks': ['/test/link.git']}}, remote_manifest_path)
    tree.write_manifest({'/test/one.git': dict(entry)}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    assert actions(q_mani) == [('/test/one.git', 'fix_params')]


def test_stale_local_manifest_fingerprint_forces_a_pull(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    tree.add_repo('test/one.git')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/one.git')
    entry = {'fingerprint': fp, 'description': None, 'owner': None, 'head': 'refs/heads/master'}

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest({'/test/one.git': dict(entry)}, remote_manifest_path)
    # Our own manifest thinks the repo is at a different fingerprint than it
    # actually is on disk -- distinct from the "remote has a new commit" case
    # (test_mirror_picks_up_new_commits), where the *local* record is correct
    # and only the remote-recorded fingerprint has moved on.
    tree.write_manifest({'/test/one.git': {**entry, 'fingerprint': 'stale-value'}}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    with caplog.at_level('DEBUG'):
        grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    assert actions(q_mani) == [('/test/one.git', 'pull')]
    assert 'Fingerprint discrepancy, forcing a fetch' in caplog.text


def test_existing_objstore_repo_for_forkgroup_takes_the_easy_init_path(tree: GrokTree) -> None:
    (tree.objstore / 'fg1.git').mkdir()
    # An unmigrated sibling still sitting on disk: if the obstrepo shortcut
    # were not taken, the found_existing search below would pick this up and
    # queue an objstore_migrate for it. Seeing only the plain init for
    # 'new.git' is what proves the shortcut actually fired.
    tree.add_repo('test/existing.git')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/existing.git')
    existing_entry = {'fingerprint': fp, 'description': None, 'owner': None, 'head': 'refs/heads/master'}

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest(
        {
            '/test/existing.git': {**existing_entry, 'forkgroup': 'fg1'},
            '/test/new.git': {'fingerprint': 'abc', 'forkgroup': 'fg1'},
        },
        remote_manifest_path,
    )
    tree.write_manifest({'/test/existing.git': {**existing_entry, 'forkgroup': 'fg1'}}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    assert actions(q_mani) == [('/test/new.git', 'init')]


def test_unmigrated_sibling_on_disk_is_queued_for_objstore_migration_before_the_new_clone(
    tree: GrokTree,
) -> None:
    tree.add_repo('test/existing.git')
    fp = grokmirror.get_repo_fingerprint(str(tree.toplevel), '/test/existing.git')
    existing_entry = {'fingerprint': fp, 'description': None, 'owner': None, 'head': 'refs/heads/master'}

    remote_manifest_path = tree.root / 'remote-manifest.json'
    tree.write_manifest(
        {
            '/test/existing.git': {**existing_entry, 'forkgroup': 'fg1'},
            '/test/new.git': {'fingerprint': 'abc', 'forkgroup': 'fg1'},
        },
        remote_manifest_path,
    )
    # Our own manifest already knows about 'existing.git' -- forkgroup info in
    # l_manifest wins over the remote's, so it has to be recorded here too, or
    # build_optimal_forkgroups() will not consider it part of the same group.
    tree.write_manifest({'/test/existing.git': {**existing_entry, 'forkgroup': 'fg1'}}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path)
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    assert actions(q_mani) == [('/test/existing.git', 'objstore_migrate'), ('/test/new.git', 'init')]


def test_private_repo_clones_its_public_siblings_first(tree: GrokTree) -> None:
    remote_manifest_path = tree.root / 'remote-manifest.json'
    # Insertion order matters: our own repo has to be the first one this
    # function considers, or its public siblings would already be 'seen' (and
    # simply cloned on their own turn) by the time we get to it, masking the
    # ordering this branch is actually responsible for.
    tree.write_manifest(
        {
            '/test/priv.git': {'fingerprint': 'p0', 'forkgroup': 'fg1'},
            '/test/pub1.git': {'fingerprint': 'p1', 'forkgroup': 'fg1'},
            '/test/pub2.git': {'fingerprint': 'p2', 'forkgroup': 'fg1'},
        },
        remote_manifest_path,
    )
    tree.write_manifest({}, tree.manifest)

    config = tree.load_remote_config(remote_manifest_path, extra={'core': {'private': '/test/priv.git'}})
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    grokmirror.pull.fill_todo_from_manifest(ses, config, q_mani)

    result = actions(q_mani)
    assert len(result) == 3
    assert set(result[:2]) == {('/test/pub1.git', 'init'), ('/test/pub2.git', 'init')}
    assert result[2] == ('/test/priv.git', 'init')
