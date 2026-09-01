# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-bundle generates clone.bundle files for CDN-offloaded cloning."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest

from grokmirror.bundle import BundleState, promote_bundles

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


def _entries(tree: GrokTree, gitdir: str = 'test/one') -> list[dict[str, Any]]:
    """grok-bundle's own bookkeeping: one record per bundle it has made."""
    state = json.loads((tree.root / 'bundles' / gitdir / '.bundlestate').read_text())
    return list(state['bundles'])


def _bundles(tree: GrokTree, gitdir: str = 'test/one') -> list[str]:
    """The real bundle files, not the clone.bundle symlink pointing at one."""
    return sorted(p.name for p in (tree.root / 'bundles' / gitdir).glob('*.bundle') if not p.is_symlink())


def _listed(tree: GrokTree, gitdir: str = 'test/one') -> list[str]:
    """The bundle names the published list actually points clients at."""
    listfile = tree.root / 'bundles' / gitdir / 'bundle-list'
    if not listfile.exists():
        return []
    out = git('config', '-f', str(listfile), '--get-regexp', r'^bundle\..*\.uri$')
    return sorted(line.split()[1] for line in out.splitlines())


def test_incremental_holds_the_first_bundle_back(tree: GrokTree) -> None:
    # The bundle exists but nothing points at it yet: the mirrors have not had
    # it long enough, and with bundle.mode=all a list naming a file that is not
    # everywhere yet breaks the clone outright.
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('-v', '--incremental')

    assert len(_bundles(tree)) == 1
    assert _listed(tree) == []
    assert not (tree.root / 'bundles' / 'test' / 'one' / 'bundle-list').exists()
    assert not (tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle').exists()


def test_incremental_publishes_after_the_delay(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('-v', '--incremental', '--publish-delay', '0')

    assert _listed(tree) == _bundles(tree)
    listfile = tree.root / 'bundles' / 'test' / 'one' / 'bundle-list'
    assert git('config', '-f', str(listfile), 'bundle.mode').strip() == 'all'
    assert git('config', '-f', str(listfile), 'bundle.heuristic').strip() == 'creationToken'


def test_incremental_keeps_a_clone_bundle_symlink(tree: GrokTree) -> None:
    # "repo" and anything else hardcoding the old name keeps working, and the
    # link only ever moves onto a bundle that is already published.
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('-v', '--incremental', '--publish-delay', '0')

    link = tree.root / 'bundles' / 'test' / 'one' / 'clone.bundle'
    assert link.is_symlink()
    assert str(link.readlink()) == _bundles(tree)[0]


def test_incremental_adds_a_second_bundle_for_new_commits(tree: GrokTree) -> None:
    src = tree.source()
    tree.add_repo('test/one.git', source=src)
    tree.run_manifest()
    tree.run_bundle('--incremental', '--publish-delay', '0')

    src.commit('second')
    src.push(tree.toplevel / 'test' / 'one.git')
    tree.run_manifest()
    tree.run_bundle('-v', '--incremental', '--publish-delay', '0')

    assert len(_bundles(tree)) == 2
    assert _listed(tree) == _bundles(tree)
    kinds = [entry['full'] for entry in _entries(tree)]
    assert kinds == [True, False]


def test_incremental_skips_a_repo_with_no_new_commits(tree: GrokTree) -> None:
    # A moved or deleted ref changes the fingerprint without adding a commit,
    # and asking git for an empty bundle would just fail every run from then on.
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('--incremental', '--publish-delay', '0')
    git('tag', 'v1', cwd=tree.toplevel / 'test' / 'one.git')
    tree.run_manifest()
    res = tree.run_bundle('-v', '--incremental', '--publish-delay', '0')

    assert 'no new commits' in res.stdout + res.stderr
    assert len(_bundles(tree)) == 1


def test_incremental_starts_over_once_the_list_is_full(tree: GrokTree) -> None:
    src = tree.source()
    tree.add_repo('test/one.git', source=src)
    tree.run_manifest()
    tree.run_bundle('--incremental', '--publish-delay', '0', '--max-bundles', '2')

    for msg in ('second', 'third'):
        src.commit(msg)
        src.push(tree.toplevel / 'test' / 'one.git')
        tree.run_manifest()
        tree.run_bundle('--incremental', '--publish-delay', '0', '--max-bundles', '2')

    # The third run hit the limit and cut a fresh full bundle, which retires
    # everything older than itself the moment it joins the list.
    entries = _entries(tree)
    assert entries[-1]['full'] is True
    assert _listed(tree) == [entries[-1]['name']]
    # Retired, but still on disk: a client may still be working through a list
    # it fetched a moment ago, and a CDN can hand that list out for longer.
    assert len(_bundles(tree)) == 3


def test_incremental_prunes_retired_bundles(tree: GrokTree) -> None:
    src = tree.source()
    tree.add_repo('test/one.git', source=src)
    tree.run_manifest()
    common = ['--incremental', '--publish-delay', '0', '--max-bundles', '1', '--prune-delay', '0']
    tree.run_bundle(*common)

    src.commit('second')
    src.push(tree.toplevel / 'test' / 'one.git')
    tree.run_manifest()
    tree.run_bundle(*common)
    # Retired by the second full bundle, and past its prune delay by the third
    # run -- pruning is never same-run, so it takes one more pass.
    tree.run_bundle('-v', *common)

    assert _bundles(tree) == _listed(tree)
    assert len(_bundles(tree)) == 1


def test_incremental_leaves_a_newer_state_version_alone(tree: GrokTree) -> None:
    # Rebuilding from scratch here would unpublish bundles that clients are in
    # the middle of using, so an unreadable state means hands off entirely.
    tree.add_repo('test/one.git')
    tree.run_manifest()
    bundledir = tree.root / 'bundles' / 'test' / 'one'
    bundledir.mkdir(parents=True)
    (bundledir / '.bundlestate').write_text('{"version": 99}')
    res = tree.run_bundle('-v', '--incremental', '--publish-delay', '0')

    assert 'unknown state version' in res.stdout + res.stderr
    assert _bundles(tree) == []


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        pytest.param('{"version": 1, "bundles": [{"na', 'cannot read', id='truncated'),
        pytest.param('{"version": 1}', 'malformed', id='missing-keys'),
        pytest.param('{"version": 1, "fingerprint": "x", "tips": [], "bundles": [{}]}', 'malformed', id='bad-entry'),
    ],
)
def test_incremental_leaves_a_damaged_state_alone(tree: GrokTree, content: str, expected: str) -> None:
    """A state file we cannot make sense of must cost nothing that is published.

    Treating it as "start over" is the worst of the options: the run writes a
    list with nothing on it, which means unlinking the list clients are
    fetching with none of the publication delay that normally protects them,
    and every bundle already on disk is stranded because no record of it is
    left to prune. Refusing the directory outright is the same answer the
    version check already gives.
    """
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_bundle('--incremental', '--publish-delay', '0')
    published = _listed(tree)
    assert published, 'nothing was published, so this proves nothing'

    (tree.root / 'bundles' / 'test' / 'one' / '.bundlestate').write_text(content)
    res = tree.run_bundle('-v', '--incremental', '--publish-delay', '0')

    assert expected in res.stdout + res.stderr
    # The list and the bundles it names are exactly as they were.
    assert _listed(tree) == published
    assert _bundles(tree) == published


def test_incremental_does_not_publish_out_of_order() -> None:
    """An increment must never reach the list before the bundle it builds on.

    next_token() keeps tokens monotonic across a clock that steps backwards,
    but 'created' is raw wall clock, so judging each bundle's age on its own
    can publish an increment while its predecessor is still held back. With
    bundle.mode=all that is a set no client can unbundle.
    """
    state: dict[str, Any] = {
        'version': 1,
        'fingerprint': 'x',
        'tips': [],
        'bundles': [
            {'name': 'a', 'token': 1000, 'created': 1000, 'listed': True, 'unlisted': None, 'full': True},
            {'name': 'b', 'token': 2000, 'created': 2000, 'listed': False, 'unlisted': None, 'full': True},
            # Same run order, but the clock had stepped back when it was made.
            {'name': 'c', 'token': 2001, 'created': 500, 'listed': False, 'unlisted': None, 'full': False},
        ],
    }
    promote_bundles(cast('BundleState', state), now=2200, publishdelay=600)

    assert [e['name'] for e in state['bundles'] if e['listed']] == ['a']


def test_incremental_bundle_list_clones(tree: GrokTree, tmp_path: Path) -> None:
    # The end-to-end check: git itself has to accept the list we write, and the
    # bundles named in it have to be enough to build the repository from.
    src = tree.source()
    tree.add_repo('test/one.git', source=src)
    tree.run_manifest()
    tree.run_bundle('--incremental', '--publish-delay', '0')

    src.commit('second')
    src.push(tree.toplevel / 'test' / 'one.git')
    tree.run_manifest()
    tree.run_bundle('--incremental', '--publish-delay', '0')

    listuri = (tree.root / 'bundles' / 'test' / 'one' / 'bundle-list').as_uri()
    dest = tmp_path / 'cloned'
    git('clone', f'--bundle-uri={listuri}', str(tree.toplevel / 'test' / 'one.git'), str(dest))
    assert git('rev-parse', 'HEAD', cwd=dest) == git('rev-parse', 'HEAD', cwd=tree.toplevel / 'test' / 'one.git')
    # Downloaded, not fetched: the bundles are what filled the object database.
    assert git('for-each-ref', '--format=%(refname)', 'refs/bundles', cwd=dest).strip()
