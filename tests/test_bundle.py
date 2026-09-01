# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-bundle generates clone.bundle files for CDN-offloaded cloning."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from support import GrokTree, git

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


def _bundle_refs(bundle: Path) -> set[str]:
    """Ref names recorded in a bundle's header."""
    out = git('bundle', 'list-heads', str(bundle))
    return {line.split(maxsplit=1)[1] for line in out.splitlines() if line.strip()}


def _commit_at(worktree: Path, stamp: int, message: str, filename: str = 'file.txt') -> None:
    """Commit with both dates pinned, so ref ages are what the test says.

    Every commit the fixtures make is dated BASE_TIMESTAMP (2020), so without
    this there is no way to tell a maintained branch from a retired one.
    """
    (worktree / filename).write_text(f'{message}\n')
    git('add', filename, cwd=worktree)
    env = dict(os.environ, GIT_AUTHOR_DATE=f'{stamp} +0000', GIT_COMMITTER_DATE=f'{stamp} +0000')
    subprocess.run(
        ['git', 'commit', '-q', '-m', message],
        cwd=str(worktree),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def aged_repo(tree: GrokTree) -> Path:
    """A repo with one maintained branch and one retired branch, plus tags.

    'retired' is an orphan branch: none of its history is reachable from
    'master', so anything it contributes to a bundle shows up as its own
    objects rather than being shared. That is what makes the tag-reachability
    assertions below mean something.
    """
    src = tree.source()
    repo = tree.add_repo('test/one.git')
    now = int(time.time())

    git('checkout', '-q', '--orphan', 'retired', cwd=src.path)
    _commit_at(src.path, now - 3600 * 24 * 3000, 'Retired branch commit', filename='retired.txt')
    git('tag', '-a', 'v-retired', '-m', 'on the retired branch', cwd=src.path)

    git('checkout', '-q', '-f', src.branch, cwd=src.path)
    _commit_at(src.path, now - 3600, 'Fresh commit on master')
    git('tag', '-a', 'v-live', '-m', 'on master', cwd=src.path)

    git('push', '-q', str(repo), '--tags', 'refs/heads/retired:refs/heads/retired', cwd=src.path)
    git('push', '-q', str(repo), f'refs/heads/{src.branch}:refs/heads/{src.branch}', cwd=src.path)
    tree.run_manifest()
    return repo


@pytest.mark.usefixtures('aged_repo')
def test_max_ref_age_keeps_maintained_branches_only(tree: GrokTree) -> None:
    tree.run_bundle('-v', '--max-ref-age', '365')

    refs = _bundle_refs(tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle')
    assert 'refs/heads/master' in refs
    assert 'refs/heads/retired' not in refs


@pytest.mark.usefixtures('aged_repo')
def test_max_ref_age_drops_tags_on_retired_branches(tree: GrokTree) -> None:
    # The reason tags are filtered by reachability and not by their own age.
    # v-retired is a brand new tag object pointing at ancient orphan history,
    # so an age filter on the tag itself would keep it -- and keeping it would
    # pull the entire retired branch back into the bundle.
    tree.run_bundle('-v', '--max-ref-age', '365')

    refs = _bundle_refs(tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle')
    assert 'refs/tags/v-live' in refs
    assert 'refs/tags/v-retired' not in refs


@pytest.mark.usefixtures('aged_repo')
def test_max_ref_age_keeps_everything_when_generous(tree: GrokTree) -> None:
    # Same tree, cutoff wide enough to cover the retired branch: now both
    # branches and both tags belong in the bundle. Pins that the filter is
    # doing reachability, not just dropping whatever looks old.
    tree.run_bundle('-v', '--max-ref-age', '36500')

    refs = _bundle_refs(tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle')
    assert {'refs/heads/master', 'refs/heads/retired', 'refs/tags/v-live', 'refs/tags/v-retired'} <= refs


def test_max_ref_age_skips_a_repo_with_nothing_recent(tree: GrokTree) -> None:
    # Every fixture commit is dated 2020, so a one-day cutoff retires the lot.
    tree.add_repo('test/one.git')
    tree.run_manifest()
    res = tree.run_bundle('-v', '--max-ref-age', '1')

    assert not (tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle').exists()
    assert 'no branch newer than' in res.stdout + res.stderr


@pytest.mark.usefixtures('aged_repo')
def test_max_ref_age_keeps_head(tree: GrokTree) -> None:
    # Without HEAD, "git clone clone.bundle" warns that the remote HEAD refers
    # to a nonexistent ref and leaves an empty working tree -- unless the
    # client's init.defaultBranch happens to match. The default -r of
    # '--branches HEAD' always put it in, so the age filter has to as well.
    tree.run_bundle('-v', '--max-ref-age', '365')

    assert 'HEAD' in _bundle_refs(tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle')


def test_max_ref_age_leaves_out_a_retired_head(tree: GrokTree, aged_repo: Path) -> None:
    # HEAD pointing at a branch the filter dropped must stay out: naming it
    # would drag that branch's whole history back in through the back door.
    # 'master' survives, so this is the filter choosing, not the repo running
    # out of branches to offer.
    git('symbolic-ref', 'HEAD', 'refs/heads/retired', cwd=aged_repo)
    tree.run_bundle('-v', '--max-ref-age', '365')

    refs = _bundle_refs(tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle')
    assert 'refs/heads/master' in refs
    assert 'HEAD' not in refs
    assert 'refs/heads/retired' not in refs


def test_max_ref_age_bundle_still_clones(tree: GrokTree, tmp_path: Path) -> None:
    """The whole point of keeping HEAD: the bundle is still cloneable.

    The branch is deliberately not called master or main. With no HEAD in the
    bundle git falls back to guessing the default branch, and on a repository
    whose branch it cannot guess the clone succeeds with a warning and leaves
    an empty working tree -- which is why "it worked when I tried it" is not
    evidence of anything here.
    """
    src = tree.source(name='devel', branch='devel')
    repo = tree.add_repo('test/one.git', source=src)
    _commit_at(src.path, int(time.time()) - 3600, 'Fresh commit on devel')
    src.push(repo)
    tree.run_manifest()
    tree.run_bundle('--max-ref-age', '365')

    dest = tmp_path / 'cloned'
    git('clone', '-q', str(tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle'), str(dest))
    assert (dest / 'file.txt').exists()


@pytest.mark.usefixtures('aged_repo')
def test_without_max_ref_age_nothing_changes(tree: GrokTree) -> None:
    # The default has to stay bit-for-bit the old behaviour: --branches HEAD,
    # every branch, no tags.
    tree.run_bundle('-v')

    refs = _bundle_refs(tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle')
    assert {'refs/heads/master', 'refs/heads/retired'} <= refs
    assert not {r for r in refs if r.startswith('refs/tags/')}
