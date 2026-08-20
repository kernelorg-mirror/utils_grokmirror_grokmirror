# SPDX-License-Identifier: GPL-3.0-or-later
"""run_git_repack() chooses its git-repack flags by repository class.

The class matrix is the heart of grok-fsck's safety story: an objstore
repository holds objects for others and must never lose any, a repository used
as an alternate by others must keep even its unreachable objects, and only a
repository with no relationships at all may drop things aggressively. These
tests pin the exact flags each class gets, so any change to the matrix has to
be made deliberately, in front of a failing test.

Every test runs the real grok-fsck over real repositories and reads the flags
back from the "repacking with" log line, then checks the repositories again
with git itself: the flags being right is necessary, the repositories staying
whole is the point.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import grokmirror

from support import GrokTree, git

pytestmark = pytest.mark.slow

# Modern git tries to write bitmaps on any repack-into-one of a bare repo, and
# warns when alternates make that impossible. The stock grokmirror.conf ships
# this warning in ignore_errors, so the tests run with it ignored too --
# without it, every repack of a repo with alternates counts as failed.
BITMAP_WARNING = 'warning: disabling bitmap writing'


def repacked(tree: GrokTree, *args: str) -> dict[str, str]:
    """Run grok-fsck and map each repacked repository's path to its flags.

    The flags come from the 'repacking with "..."' log line, which names them
    exactly as they are passed to git repack (minus the -q that is always
    appended after logging). The preceding "<fullpath>:" line says which
    repository they were for.
    """
    res = tree.run_fsck('-v', *args)
    flags: dict[str, str] = {}
    current = None
    for rawline in (res.stdout + res.stderr).splitlines():
        line = rawline.strip()
        if line.endswith('.git:'):
            current = line[:-1]
        elif 'repacking with "' in line and current:
            flags[current] = line.split('"')[1]
    return flags


def assert_clean(*repos: object) -> None:
    """Fail if git fsck finds anything wrong in any of the given repositories."""
    for repo in repos:
        out = git('fsck', '--no-dangling', cwd=str(repo))
        assert out == '', f'{repo} is not clean after repack:\n{out}'


def test_standalone_quick_repack(tree: GrokTree) -> None:
    # A quick repack is geometric: it rolls up only the packs at the small
    # end of a geometric progression, so it never rewrites (or drops) the
    # bulk of a large repository. With several packs the bitmap has to live
    # in a multi-pack-index, hence --write-midx.
    repo = tree.add_repo('test/solo.git')
    head = git('rev-parse', 'HEAD', cwd=repo).strip()
    tree.run_manifest()
    tree.write_config()

    flags = repacked(tree, '--repack-all-quick')

    assert flags[str(repo)] == '--geometric=2 --write-midx -b -d'
    assert (repo / 'objects' / 'pack' / 'multi-pack-index').exists()
    assert_clean(repo)
    assert git('cat-file', '-e', head, cwd=repo) == ''


def test_standalone_full_repack(tree: GrokTree) -> None:
    repo = tree.add_repo('test/solo.git')
    tree.run_manifest()
    tree.write_config()

    flags = repacked(tree, '--repack-all-full')

    # No -f: delta islands are enforced even on reused deltas, so recomputing
    # every delta from scratch is pure CPU cost. It can be restored through
    # extra_repack_flags_full (proven below).
    assert flags[str(repo)] == '-a -b --unpack-unreachable=yesterday --pack-kept-objects -d'
    assert_clean(repo)


def test_extra_repack_flags_are_passed_through(tree: GrokTree) -> None:
    # extra_repack_flags rides along on every repack; the _full variant is
    # added only when the repack is full.
    repo = tree.add_repo('test/solo.git')
    tree.run_manifest()
    tree.write_config(
        {
            'fsck': {
                'extra_repack_flags': '--threads=1',
                # -f restores the old recompute-all-deltas full repacks
                'extra_repack_flags_full': '-f --window=250 --depth=50',
            }
        }
    )

    flags = repacked(tree, '--repack-all-quick')
    assert '--threads=1' in flags[str(repo)]
    assert '--window=250' not in flags[str(repo)]

    # A quick repack leaves no loose objects behind, and a repo with nothing
    # loose and a single pack is not worth repacking again -- so give it fresh
    # loose objects before asking for the full repack.
    src = tree.source()
    src.commit()
    src.push(repo)

    flags = repacked(tree, '--repack-all-full')
    assert '--threads=1' in flags[str(repo)]
    assert '-f --window=250 --depth=50' in flags[str(repo)]
    assert_clean(repo)


def test_fork_group_repack(tree: GrokTree) -> None:
    # Two forks share an objstore repository. Neither the objstore repo nor
    # the children ever delete anything on a quick repack -- geometric
    # repacking only rolls small packs together. No -b for the objstore repo:
    # repack.writeBitmaps is set in its config instead.
    one = tree.add_repo('test/one.git')
    fork = tree.add_repo('test/fork.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'ignore_errors': BITMAP_WARNING}})

    # The first run migrates the pair into a new objstore repository, which
    # always gets an immediate full repack.
    flags = repacked(tree, '-f')
    (obstrepo,) = tree.objstore_repos()
    assert flags[str(obstrepo)] == '-a --pack-kept-objects -d'

    # An objstore repo is only requeued when its loose objects are worth the
    # bother: more than 10% of a total that exceeds 1MB. An incompressible
    # megabyte pushed to a child and fetched into the objstore repo gets over
    # that bar; small trees never would.
    src = tree.source()
    src.commit(content=random.Random(20260820).randbytes(1500000).hex())
    src.push(one)
    # The objstore fetch is fingerprint-gated, and it is grok-pull that
    # refreshes the fingerprint after fetching into a child -- a bare push
    # does not, so stand in for grok-pull here.
    grokmirror.set_repo_fingerprint(str(tree.toplevel), '/test/one.git')
    heads = {repo: git('rev-parse', 'HEAD', cwd=repo).strip() for repo in (one, fork)}

    flags = repacked(tree, '--repack-all-quick')

    # Children get no --write-midx: the objstore fetch hardlinks pack files
    # out of them, and a multi-pack-index must never travel between repos.
    assert flags[str(one)] == '--geometric=2 -l -d'
    assert flags[str(obstrepo)] == '--geometric=2 --write-midx -d'
    assert (obstrepo / 'objects' / 'pack' / 'multi-pack-index').exists()
    assert not (one / 'objects' / 'pack' / 'multi-pack-index').exists()
    assert_clean(one, fork, obstrepo)
    for repo, head in heads.items():
        assert git('cat-file', '-e', head, cwd=repo) == ''
    # The objstore repository stays precious outside of repacks: the window
    # where a concurrent object deletion could hurt it must stay shut.
    assert grokmirror.is_precious(str(obstrepo))


def test_alt_parent_and_grandchild_repack(tree: GrokTree) -> None:
    # A pre-objstore arrangement grok-fsck must maintain but not convert:
    # grandma lends objects to mommy, and mommy lends objects to child. The
    # three histories are unrelated, so sibling detection has no reason to
    # migrate anybody into an objstore repository.
    #
    # grandma is used by others: unreachable objects may be reachable from a
    # borrower, so nothing may ever be dropped -- which a geometric repack
    # never does anyway.
    #
    # mommy both has and provides alternates. That is the grandchild-corruption
    # arrangement, so she keeps the maximally conservative all-into-one flags:
    # only her own objects (-l), unreachables kept loose (-A), and a warning
    # in the log.
    #
    # child only borrows, so she geometrically repacks her own objects.
    grandma = tree.add_repo('test/grandma.git', source='gsource')
    mommy = tree.add_repo('test/mommy.git', source='msource')
    child = tree.add_repo('test/child.git', source='csource')
    (mommy / 'objects' / 'info' / 'alternates').write_text(f'{grandma / "objects"}\n')
    (child / 'objects' / 'info' / 'alternates').write_text(f'{mommy / "objects"}\n')
    heads = {repo: git('rev-parse', 'HEAD', cwd=repo).strip() for repo in (grandma, mommy, child)}
    tree.run_manifest()
    tree.write_config({'fsck': {'ignore_errors': BITMAP_WARNING}})

    flags = repacked(tree, '--repack-all-quick')

    assert flags[str(grandma)] == '--geometric=2 --write-midx -b -d'
    assert flags[str(mommy)] == '-A -l -d'
    assert flags[str(child)] == '--geometric=2 -l -d'
    assert 'grandchild corruption' in tree.log_text()
    assert_clean(grandma, mommy, child)
    for repo, head in heads.items():
        assert git('cat-file', '-e', head, cwd=repo) == ''


def test_repeated_geometric_repacks_keep_the_repo_whole(tree: GrokTree) -> None:
    # The steady state grok-fsck now lives in: pushes trickle in, quick
    # repacks roll them up geometrically, and once in a while a full repack
    # consolidates everything. Nothing may ever go missing along the way.
    repo = tree.add_repo('test/solo.git')
    tree.run_manifest()
    tree.write_config()
    src = tree.source()

    heads = []
    for _ in range(4):
        src.commit()
        src.push(repo)
        heads.append(git('rev-parse', 'HEAD', cwd=repo).strip())
        tree.run_fsck('--repack-all-quick')
        assert_clean(repo)

    packdir = repo / 'objects' / 'pack'
    assert (packdir / 'multi-pack-index').exists()

    # The consolidating full repack must cope with the multi-pack-index the
    # geometric runs left behind: everything ends up in a single pack and no
    # stale midx is left pointing at deleted packs.
    src.commit()
    src.push(repo)
    tree.run_fsck('--repack-all-full')
    heads.append(git('rev-parse', 'HEAD', cwd=repo).strip())

    assert len(list(packdir.glob('*.pack'))) == 1
    assert_clean(repo)
    for head in heads:
        assert git('cat-file', '-e', head, cwd=repo) == ''


def test_midx_is_never_hardlinked_into_the_objstore(tree: GrokTree) -> None:
    # The objstore fetch hardlinks pack files straight out of the child repo.
    # A multi-pack-index describes the packs of the repository it was written
    # in, so one must never make the trip -- and it must not be deleted from
    # the child either, since the child still needs it.
    child = tree.add_repo('test/one.git')
    git('repack', '-a', '-d', cwd=child)
    git('multi-pack-index', 'write', cwd=child)
    assert (child / 'objects' / 'pack' / 'multi-pack-index').exists()
    head = git('rev-parse', 'HEAD', cwd=child).strip()

    obstrepo = grokmirror.setup_objstore_repo(str(tree.objstore))
    virtref = grokmirror.objstore_virtref(str(child))
    assert grokmirror._fetch_objstore_repo_using_plumbing(str(child), obstrepo, virtref)

    obstpacks = Path(obstrepo, 'objects', 'pack')
    assert not list(obstpacks.glob('multi-pack-index*'))
    assert (child / 'objects' / 'pack' / 'multi-pack-index').exists()
    # The objects themselves did make the trip.
    assert git('cat-file', '-e', head, cwd=obstrepo) == ''


def test_objstore_compression_is_left_at_the_zlib_default(tree: GrokTree, tmp_path: Path) -> None:
    # Grokmirror used to force pack.compression=9 on objstore repositories.
    # The last percent of pack size is not worth what level 9 costs in CPU on
    # every repack of the largest repos on a server, so new objstore repos now
    # leave compression alone, and a repack removes the old forced setting.
    # (Created in its own directory so it does not join the tree's objstore.)
    fresh = grokmirror.setup_objstore_repo(str(tmp_path / 'freshobst'))
    assert git('config', '--get', 'pack.compression', cwd=fresh, check=False).strip() == ''

    tree.add_repo('test/one.git')
    tree.add_repo('test/fork.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'ignore_errors': BITMAP_WARNING}})
    tree.run_fsck('-f')
    (obstrepo,) = tree.objstore_repos()

    # An objstore repo carrying the old forced setting is healed when it is
    # next repacked...
    git('config', 'pack.compression', '9', cwd=obstrepo)
    (obstrepo / 'grokmirror.repack').touch()
    tree.run_fsck()
    assert git('config', '--get', 'pack.compression', cwd=obstrepo, check=False).strip() == ''

    # ...but a compression level someone chose on purpose is not ours to undo.
    git('config', 'pack.compression', '1', cwd=obstrepo)
    (obstrepo / 'grokmirror.repack').touch()
    tree.run_fsck()
    assert git('config', '--get', 'pack.compression', cwd=obstrepo).strip() == '1'


def test_precious_repo_is_repacked_without_d(tree: GrokTree) -> None:
    # With precious=always, extensions.preciousObjects stays on even during
    # the repack, and git would refuse -d outright. The flags must not
    # include it, at either repack level.
    repo = tree.add_repo('test/solo.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'precious': 'always'}})

    flags = repacked(tree, '--repack-all-full')

    assert '-d' not in flags[str(repo)].split()
    assert grokmirror.is_precious(str(repo))
    assert_clean(repo)
