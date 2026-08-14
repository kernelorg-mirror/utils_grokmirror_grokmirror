# SPDX-License-Identifier: GPL-3.0-or-later
"""build_optimal_forkgroups(), which decides which repos share an objstore.

grok-pull calls this while fetching the remote manifest, to work out whether
the origin's own fork detection (or a legacy grokmirror-1.x 'reference') found
siblings we should merge into our own objstore layout -- and whether our own
grok-fsck already found a better grouping than the one the manifest describes.
"""

from __future__ import annotations

import grokmirror
import grokmirror.pull

from support import GrokTree, git


def test_a_remote_forkgroup_key_groups_its_members(tree: GrokTree) -> None:
    r_manifest: grokmirror.Manifest = {
        '/test/one.git': {'forkgroup': 'fg1'},
        '/test/two.git': {'forkgroup': 'fg1'},
    }

    forkgroups = grokmirror.pull.build_optimal_forkgroups({}, r_manifest, str(tree.toplevel), str(tree.objstore))

    assert forkgroups == {
        'fg1': {str(tree.path('test/one.git')), str(tree.path('test/two.git'))},
    }


def test_our_own_forkgroup_wins_over_the_remote_manifests(tree: GrokTree) -> None:
    # Our own grok-fsck may have found a better grouping than the origin's --
    # e.g. we merged this repo into a fork group the origin doesn't know about
    # -- so our forkgroup key overrides theirs, not the other way around.
    l_manifest: grokmirror.Manifest = {'/test/one.git': {'forkgroup': 'ours'}}
    r_manifest: grokmirror.Manifest = {
        '/test/one.git': {'forkgroup': 'theirs'},
        '/test/two.git': {'forkgroup': 'ours'},
    }

    forkgroups = grokmirror.pull.build_optimal_forkgroups(
        l_manifest, r_manifest, str(tree.toplevel), str(tree.objstore)
    )

    assert forkgroups == {
        'ours': {str(tree.path('test/one.git')), str(tree.path('test/two.git'))},
    }
    # The override is also written back into r_manifest, since callers use it
    # afterwards to decide which alternate to point this repo at.
    assert r_manifest['/test/one.git'].get('forkgroup') == 'ours'


def test_a_legacy_reference_without_a_forkgroup_synthesizes_one(tree: GrokTree) -> None:
    # grokmirror-1.x manifests only ever recorded 'reference', not 'forkgroup':
    # a repo with a reference but no forkgroup of its own must still end up
    # grouped with the repo it references.
    r_manifest: grokmirror.Manifest = {
        '/test/one.git': {'reference': '/test/two.git'},
        '/test/two.git': {},
    }

    forkgroups = grokmirror.pull.build_optimal_forkgroups({}, r_manifest, str(tree.toplevel), str(tree.objstore))

    assert len(forkgroups) == 1
    (siblings,) = forkgroups.values()
    assert siblings == {str(tree.path('test/one.git')), str(tree.path('test/two.git'))}


def test_merges_with_an_existing_objstore_forkgroup_that_shares_a_sibling(tree: GrokTree) -> None:
    # Our own objstore already groups 'one.git' on its own (e.g. from a prior
    # grok-fsck run). The remote manifest now says 'one.git' and 'two.git' are
    # forks of each other -- the two groups share 'one.git', so they must be
    # merged into our existing forkgroup rather than left as two separate ones.
    obstrepo = tree.objstore / 'existing-fg.git'
    git('init', '-q', '--bare', str(obstrepo))
    git('remote', 'add', 'virtref1', str(tree.path('test/one.git')), cwd=obstrepo)

    r_manifest: grokmirror.Manifest = {
        '/test/one.git': {'forkgroup': 'newfg'},
        '/test/two.git': {'forkgroup': 'newfg'},
    }

    forkgroups = grokmirror.pull.build_optimal_forkgroups({}, r_manifest, str(tree.toplevel), str(tree.objstore))

    assert forkgroups == {
        'existing-fg': {str(tree.path('test/one.git')), str(tree.path('test/two.git'))},
    }
