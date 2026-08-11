# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-bundle generates clone.bundle files for CDN-offloaded cloning."""

from __future__ import annotations

import pytest

from support import GrokTree

pytestmark = pytest.mark.slow


def test_bundles_a_repo(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('-v')

    bundle = tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle'
    assert bundle.exists()
    # The fingerprint recorded next to the bundle is what makes the next run
    # skip it, so it has to match the repo's.
    fprfile = bundle.parent / '.fingerprint'
    assert fprfile.read_text().strip() == tree.read_manifest()['/test/one.git']['fingerprint']


def test_second_run_skips_unchanged_repos(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle()
    res = tree.run_bundle('-v')
    assert 'skipped' in res.stdout + res.stderr


@pytest.mark.parametrize(
    'args',
    [
        pytest.param([], id='defaults'),
        pytest.param(['-g', ''], id='empty-gitargs'),
        pytest.param(['-r', ''], id='empty-revlistargs'),
        pytest.param(['-g', '', '-r', ''], id='both-empty'),
    ],
)
def test_empty_extra_arguments_do_not_crash(tree: GrokTree, args: list[str]) -> None:
    # An empty value skipped the .split(), so a str got concatenated with a list
    # and raised TypeError before git was ever called. ''.split() is already [],
    # so the special case was never needed, and passing -g '' is the obvious way
    # to say "no extra git arguments".
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('-v', *args)


def test_empty_gitargs_still_bundles(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('-v', '-g', '')

    assert (tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle').exists()


def test_empty_revlistargs_bundles_nothing(tree: GrokTree) -> None:
    # Not a grokmirror decision: with no rev-list arguments there are no refs to
    # put in the bundle, and git refuses to create an empty one. Worth pinning
    # down so the "no crash" test above is not mistaken for "it worked".
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('-v', '-r', '')

    assert not (tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle').exists()


def test_skips_a_repo_with_no_refs(tree: GrokTree) -> None:
    # grok-manifest will not record a ref-less repo, so this needs a manifest
    # written by hand -- which is also what a mirror ends up with when every ref
    # in a repo gets deleted after the manifest was generated.
    tree.add_empty_repo('test/empty.git')
    tree.write_manifest({'/test/empty.git': {'modified': 1, 'fingerprint': None}})
    res = tree.run_bundle('-v')

    assert not (tree.root / 'bundles' / 'test' / 'empty' / 'clone.bundle').exists()
    assert 'no refs to bundle' in res.stdout + res.stderr


def test_repo_name_containing_dot_git_keeps_its_name(tree: GrokTree) -> None:
    # The output directory was derived with repo.replace('.git', ''), which
    # strips every occurrence, not just the trailing one, so a repository named
    # foo.github.io.git bundled into "foohub.io" instead of "foo.github.io".
    tree.add_repo('test/foo.github.io.git')
    tree.run_manifest()
    tree.run_bundle('-v')

    assert (tree.root / 'bundles' / 'test' / 'foo.github.io' / 'clone.bundle').exists()


def test_include_globbing(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.add_repo('other/two.git', source='beta')
    tree.run_manifest()
    tree.run_bundle('-v', '-i', '/test/*')

    assert (tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle').exists()
    assert not (tree.root / 'bundles' / 'other' / 'two' / 'clone.bundle').exists()


def test_maxsize_skips_larger_repos(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    # Nothing can be smaller than 0 MiB, so everything gets skipped.
    res = tree.run_bundle('-v', '-s', '0')

    assert not (tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle').exists()
    assert 'skipped' in res.stdout + res.stderr
