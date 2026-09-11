# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-shallow-tar builds one shallow single-branch repository per tarball."""

from __future__ import annotations

import errno
import subprocess
import tarfile
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from grokmirror import shallowtar
from grokmirror.shallowtar import Branch, generate_tarball, select_branches

from support import Source, git

if TYPE_CHECKING:
    # The type TarFile.addfile() actually takes; overriding it with anything
    # narrower (IO[bytes], say) is a Liskov violation the checkers will catch.
    from _typeshed import SupportsRead

pytestmark = pytest.mark.slow

NOW = 1757577600
PUBLIC_URL = 'https://git.example.com/pub/scm/linux.git'


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """A bare repo with ten commits on master, as something to publish."""
    bare = tmp_path / 'origin' / 'linux.git'
    bare.parent.mkdir(parents=True, exist_ok=True)
    git('init', '-q', '--bare', str(bare))
    source = Source(tmp_path / 'work', branch='master')
    for _ in range(10):
        source.commit()
    source.push(bare)
    return bare


def branch_of(repo: Path, name: str = 'master') -> Branch:
    selected = select_branches(repo, name, NOW)
    assert len(selected) == 1
    return selected[0]


def build(repo: Path, outdir: Path, name: str = 'master', depth: int = 1) -> Path:
    tarpath = outdir / 'linux.master.shallow.tar'
    assert generate_tarball(repo, branch_of(repo, name), PUBLIC_URL, tarpath, 'linux', NOW, depth=depth)
    return tarpath


def extract(tarpath: Path, into: Path) -> Path:
    """Unpack with tar(1), the way a CI runner actually would."""
    into.mkdir(parents=True, exist_ok=True)
    subprocess.run(['tar', '-xf', str(tarpath)], cwd=into, check=True, capture_output=True)
    return into / 'linux'


def test_the_tarball_holds_a_repository_and_no_working_tree(origin_repo: Path, tmp_path: Path) -> None:
    """CI checks out the sha it wants, so a tree here is a doubled download."""
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert (clone / '.git').is_dir()
    assert [p.name for p in clone.iterdir()] == ['.git']


def test_the_clone_inside_is_genuinely_shallow(origin_repo: Path, tmp_path: Path) -> None:
    """The one test that catches a silently dropped --depth.

    "git clone --depth=1 /path" warns and ignores the depth, and the result is
    a *working* clone of the whole history -- it serves the right commit and
    passes every other test in this file. Only asking git directly tells the
    two apart.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert len((clone / '.git' / 'shallow').read_text().splitlines()) == 1
    assert len(git('log', '--oneline', 'origin/master', cwd=clone).splitlines()) == 1


def test_a_deeper_tarball_carries_the_history_it_was_asked_for(origin_repo: Path, tmp_path: Path) -> None:
    clone = extract(build(origin_repo, tmp_path / 'out', depth=5), tmp_path / 'x')
    assert len(git('log', '--oneline', 'origin/master', cwd=clone).splitlines()) == 5


def test_the_origin_is_the_public_url_not_the_path_we_cloned_from(origin_repo: Path, tmp_path: Path) -> None:
    """The file:// URL is a path on the generating host and no use to CI."""
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert git('config', '--get', 'remote.origin.url', cwd=clone).strip() == PUBLIC_URL


def test_the_refspec_asks_the_origin_about_one_ref(origin_repo: Path, tmp_path: Path) -> None:
    """The narrow refspec is the whole ongoing load win, so pin it."""
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    fetch = git('config', '--get', 'remote.origin.fetch', cwd=clone).strip()
    assert fetch == '+refs/heads/master:refs/remotes/origin/master'


def test_head_points_at_the_branch_so_a_bare_checkout_works(origin_repo: Path, tmp_path: Path) -> None:
    """Otherwise "git checkout" in CI lands on the client's default branch."""
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert git('symbolic-ref', 'HEAD', cwd=clone).strip() == 'refs/heads/master'
    git('checkout', '-q', 'master', cwd=clone)
    assert (clone / 'file.txt').exists()


def test_the_creation_time_is_stamped_where_a_human_can_find_it(origin_repo: Path, tmp_path: Path) -> None:
    """A tarball hoarded for months is a failure we can only make diagnosable."""
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert git('config', '--get', 'grokmirror.shallowCreated', cwd=clone).strip() == str(NOW)
    assert git('config', '--get', 'grokmirror.shallowBranch', cwd=clone).strip() == 'master'


def test_no_sample_hooks_are_shipped(origin_repo: Path, tmp_path: Path) -> None:
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert list((clone / '.git' / 'hooks').glob('*.sample')) == []


def test_a_tag_on_the_tip_does_not_ship_inside_the_tarball(origin_repo: Path, tmp_path: Path) -> None:
    """The tag that gets in is the one that is already there when we clone.

    Tag auto-following runs during the clone, before prepare_clone() gets to
    set remote.origin.tagOpt, so a tag sitting on the branch tip -- which on a
    stable tree is every release -- rode along into the published tarball. The
    other tag test pushes its tag afterwards and so never saw this.
    """
    git('tag', 'v9.9', 'master', cwd=origin_repo)
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert git('tag', cwd=clone).split() == []


def test_tags_do_not_follow_on_the_first_fetch(origin_repo: Path, tmp_path: Path) -> None:
    """Asserting tagOpt is set is not enough; the behaviour is the thing.

    Tags auto-follow even on a --single-branch clone. On a stable tree that is
    thousands of them, and a tag pointing outside the shallow boundary drags
    its history across -- precisely the server load this tool exists to remove.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert git('config', '--get', 'remote.origin.tagOpt', cwd=clone).strip() == '--no-tags'

    source = Source(tmp_path / 'work', branch='master')
    source.commit()
    source.tag('v9.9')
    source.push(origin_repo)
    source.push(origin_repo, refspec='refs/tags/v9.9')

    git('remote', 'set-url', 'origin', origin_repo.resolve().as_uri(), cwd=clone)
    git('remote', 'update', cwd=clone)
    assert git('tag', cwd=clone).split() == []
    assert len((clone / '.git' / 'shallow').read_text().splitlines()) == 1


def test_a_ci_run_can_untar_update_and_check_out_a_new_sha(origin_repo: Path, tmp_path: Path) -> None:
    """The workflow this whole tool exists for, start to finish."""
    tarpath = build(origin_repo, tmp_path / 'out')

    source = Source(tmp_path / 'work', branch='master')
    wanted = source.commit(content='the commit CI is here for\n')
    source.push(origin_repo)

    clone = extract(tarpath, tmp_path / 'ci')
    git('remote', 'set-url', 'origin', origin_repo.resolve().as_uri(), cwd=clone)
    git('remote', 'update', cwd=clone)
    git('checkout', '-q', wanted, cwd=clone)
    assert (clone / 'file.txt').read_text() == 'the commit CI is here for\n'


def test_the_tarball_carries_no_objstore_alternate(tmp_path: Path) -> None:
    """The repos this runs against are alternates-backed, and CI's are not.

    The child here is built the way grokmirror's objstore leaves one: it has
    the refs, but not one object of its own -- every object is reached through
    objects/info/alternates. upload-pack reads through the alternate, so the
    tarball must come out self-contained, with no alternates file pointing at
    a path that does not exist on the CI node.
    """
    objstore = tmp_path / 'objstore.git'
    child = tmp_path / 'linux.git'
    for repo in (objstore, child):
        git('init', '-q', '--bare', str(repo))
    (child / 'objects' / 'info' / 'alternates').write_text(f'{objstore / "objects"}\n')

    source = Source(tmp_path / 'work', branch='master')
    for _ in range(3):
        source.commit()
    source.push(objstore)
    # Only the ref, so the child stays object-free and the alternate is
    # load-bearing rather than merely present.
    git('update-ref', 'refs/heads/master', source.head(), cwd=child)
    assert 'in-pack: 0' in git('count-objects', '-v', cwd=child)

    clone = extract(build(child, tmp_path / 'out'), tmp_path / 'x')
    assert not (clone / '.git' / 'objects' / 'info' / 'alternates').exists()
    assert git('cat-file', '-e', f'{source.head()}^{{commit}}', cwd=clone) == ''


def test_the_tarball_lands_whole_or_not_at_all(origin_repo: Path, tmp_path: Path) -> None:
    """No reader may ever meet a half-written tarball under the final name."""
    outdir = tmp_path / 'out'
    tarpath = build(origin_repo, outdir)
    assert tarfile.is_tarfile(tarpath)
    assert [p.name for p in outdir.iterdir() if p.name != tarpath.name] == []


class OutOfSpaceTar(tarfile.TarFile):
    """A TarFile that fills the disk partway through writing the tarball."""

    added = 0

    def addfile(self, tarinfo: tarfile.TarInfo, fileobj: SupportsRead[bytes] | None = None) -> None:
        self.added += 1
        if self.added > 3:
            raise OSError(errno.ENOSPC, 'No space left on device')
        super().addfile(tarinfo, fileobj)


def test_a_write_that_dies_halfway_leaves_the_old_tarball_alone(
    origin_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reason for the temporary name, stated as a test.

    Writing straight to the published name would hand every CI node that
    arrives mid-run a truncated tarball, and a crash would leave one there for
    good. Failing after a few entries is the only way to see the difference,
    since a successful write looks identical either way.
    """
    outdir = tmp_path / 'out'
    tarpath = build(origin_repo, outdir)
    before = tarpath.read_bytes()

    def failing_open(name: Path, mode: str, *, format: int) -> tarfile.TarFile:
        assert mode == 'w'
        return OutOfSpaceTar.open(name, 'w', format=format)

    monkeypatch.setattr(shallowtar.tarfile, 'open', failing_open)
    assert not generate_tarball(origin_repo, branch_of(origin_repo), PUBLIC_URL, tarpath, 'linux', NOW)
    monkeypatch.undo()

    assert tarpath.read_bytes() == before
    assert [p.name for p in outdir.iterdir()] == [tarpath.name]
    assert 'could not write the tarball' in caplog.text


def test_directory_entries_are_added_in_sorted_order(origin_repo: Path, tmp_path: Path) -> None:
    """Two runs over the same tip should differ only in the mtimes git wrote.

    Comparing whole name lists against sorted() would be wrong: entries go in
    depth first, and "a/z" sorts after "a.b" as a string while coming before
    it in the tarball. What is actually promised is that each directory's
    children are sorted, so that is what gets checked.
    """
    with tarfile.open(build(origin_repo, tmp_path / 'out')) as tar:
        names = tar.getnames()
    children: defaultdict[str, list[str]] = defaultdict(list)
    for name in names:
        parent, _, base = name.rpartition('/')
        children[parent].append(base)
    assert all(group == sorted(group) for group in children.values())


def test_ownership_is_zeroed_so_the_builder_is_not_in_the_artifact(origin_repo: Path, tmp_path: Path) -> None:
    with tarfile.open(build(origin_repo, tmp_path / 'out')) as tar:
        members = tar.getmembers()
    assert {(m.uid, m.gid, m.uname, m.gname) for m in members} == {(0, 0, 'root', 'root')}


def test_a_repository_git_cannot_clone_publishes_nothing(tmp_path: Path) -> None:
    missing = tmp_path / 'gone.git'
    tarpath = tmp_path / 'out' / 'linux.master.shallow.tar'
    assert not generate_tarball(missing, Branch('master', 'master', 'a' * 40), PUBLIC_URL, tarpath, 'linux', NOW)
    assert not tarpath.exists()


def test_a_failed_run_leaves_no_scratch_directory_behind(tmp_path: Path) -> None:
    """The scratch clone lives in the output directory, so litter is visible."""
    missing = tmp_path / 'gone.git'
    outdir = tmp_path / 'out'
    generate_tarball(missing, Branch('master', 'master', 'a' * 40), PUBLIC_URL, outdir / 'x.tar', 'linux', NOW)
    assert list(outdir.iterdir()) == []


def test_a_clone_that_is_not_shallow_is_refused(
    origin_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Simulate --depth being ignored, and check we refuse rather than ship.

    This is the guard that turns the nastiest failure mode in this tool -- a
    perfectly working clone of the entire history -- from silent into loud.
    """
    real = shallowtar.clone_shallow

    def unshallow(fullpath: Path, branch: Branch, destdir: Path, depth: int) -> bool:
        assert real(fullpath, branch, destdir, depth)
        subprocess.run(['git', 'fetch', '--unshallow', '-q'], cwd=destdir, check=True, capture_output=True)
        return True

    monkeypatch.setattr(shallowtar, 'clone_shallow', unshallow)
    tarpath = tmp_path / 'out' / 'linux.master.shallow.tar'
    assert not generate_tarball(origin_repo, branch_of(origin_repo), PUBLIC_URL, tarpath, 'linux', NOW)
    assert not tarpath.exists()
    assert 'not shallow' in caplog.text


def test_a_clone_whose_head_is_not_the_branch_is_refused(
    origin_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = shallowtar.clone_shallow

    def detach(fullpath: Path, branch: Branch, destdir: Path, depth: int) -> bool:
        assert real(fullpath, branch, destdir, depth)
        git('checkout', '-q', '--detach', cwd=destdir)
        return True

    monkeypatch.setattr(shallowtar, 'clone_shallow', detach)
    tarpath = tmp_path / 'out' / 'linux.master.shallow.tar'
    assert not generate_tarball(origin_repo, branch_of(origin_repo), PUBLIC_URL, tarpath, 'linux', NOW)
    assert not tarpath.exists()
    assert 'HEAD is not pointing at the branch' in caplog.text
