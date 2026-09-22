# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-shallow-tar builds one shallow single-branch repository per tarball."""

from __future__ import annotations

import errno
import os
import subprocess
import tarfile
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from grokmirror import shallowtar
from grokmirror.shallowtar import README_NAME, Branch, generate_tarball, select_branches, sweep_stale_workdirs

from support import Source, git

if TYPE_CHECKING:
    from collections.abc import Iterator

    # The type TarFile.addfile() actually takes; overriding it with anything
    # narrower (IO[bytes], say) is a Liskov violation the checkers will catch.
    from _typeshed import SupportsRead

pytestmark = pytest.mark.slow

NOW = 1757577600
DAY = 86400
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


def prose(clone: Path) -> str:
    """The readme with its line breaks collapsed away.

    The file is hard-wrapped for the person reading it, and where the breaks
    happen to fall is not something an assertion about its wording should be
    hostage to -- rewrapping a paragraph must not fail a test about what it
    says. Tests that care about structure read the file directly instead.
    """
    return ' '.join((clone / '.git' / README_NAME).read_text().split())


def test_the_description_carries_the_generation_data(origin_repo: Path, tmp_path: Path) -> None:
    """Where this tree came from, in the file whose job that is.

    All of this is also in the config keys and the sidecar, but those need
    tooling and the knowledge that they exist. This one needs cat(1).
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    description = (clone / '.git' / 'description').read_text()
    assert PUBLIC_URL in description
    assert 'master' in description
    assert branch_of(origin_repo).tip in description
    # The date a person reads, not the epoch seconds the config stamps.
    assert '2025-09-11' in description
    assert 'grok-shallow-tar' in description


def test_the_description_stays_short_and_points_at_the_readme(origin_repo: Path, tmp_path: Path) -> None:
    """A description is a description; the explaining happens next door.

    The length is pinned rather than left to taste because the pressure is
    all one way: every future "while we are here, we should also mention..."
    lands in this file unless something objects.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    lines = (clone / '.git' / 'description').read_text().splitlines()
    assert len(lines) <= 6
    assert lines[0] == 'Shallow single-branch clone of linux.git, branch master, one commit deep.'
    assert f'.git/{README_NAME}' in '\n'.join(lines)


def test_the_description_records_the_depth_that_was_asked_for(origin_repo: Path, tmp_path: Path) -> None:
    """Otherwise "why is this tree missing history" needs git to answer."""
    clone = extract(build(origin_repo, tmp_path / 'out', depth=5), tmp_path / 'x')
    assert '5 commits deep' in (clone / '.git' / 'description').read_text()


def test_a_depth_of_one_reads_as_a_sentence_not_as_a_number(origin_repo: Path, tmp_path: Path) -> None:
    """A stray "1 commits" in an artifact this many people unpack would grate."""
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert 'one commit deep' in (clone / '.git' / 'description').read_text()


def test_the_description_replaces_the_one_git_wrote(origin_repo: Path, tmp_path: Path) -> None:
    """git's placeholder tells that reader nothing, and shipping it is noise."""
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    assert 'Unnamed repository' not in (clone / '.git' / 'description').read_text()


def test_both_files_stay_inside_eighty_columns(origin_repo: Path, tmp_path: Path) -> None:
    """These are read in a terminal, and interpolation is what breaks wrapping.

    Hand-wrapped prose stays wrapped; a URL or an object name dropped into the
    middle of a sentence does not, and the paragraph only goes ragged for the
    repositories whose names are long -- which on kernel.org is most of them.

    So a long value does not exempt its line, it only excuses its own length:
    with the value taken out, what is left has to be a label rather than a
    sentence. Exempting the whole line would have made this test blind to the
    one arrangement it exists to forbid.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    tip = branch_of(origin_repo).tip
    for name in ('description', README_NAME):
        for line in (clone / '.git' / name).read_text().splitlines():
            rest = line.replace(PUBLIC_URL, '').replace(tip, '')
            if rest == line:
                assert len(line) <= 80, f'{name}: {line}'
            else:
                # A line carrying a long value may be as long as the value
                # makes it, but what surrounds the value must be a label.
                assert len(rest) <= 20, f'{name}: {line}'


def test_the_readme_sends_the_reader_next_door_for_the_details(origin_repo: Path, tmp_path: Path) -> None:
    """The two files point at each other, so either one is a way in.

    Nothing about this particular tarball is repeated here -- origin, branch,
    tip and depth are the description's job. That only works as long as the
    readme says so; without the pointer, a reader who opens this file first
    has no way of knowing the other one exists.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    readme = prose(clone)
    assert '"description"' in readme
    # And the facts really are only next door, not quietly duplicated here,
    # where a future edit could leave them disagreeing with each other.
    assert PUBLIC_URL not in readme
    assert branch_of(origin_repo).tip not in readme


def test_the_readme_says_to_discard_the_config_and_fsck_first(origin_repo: Path, tmp_path: Path) -> None:
    """This tree came off the network, and .git/config is a list of programs.

    We ship nothing dangerous, but the reader cannot tell our tarball from one
    tampered with on a mirror or in transit. Clearing .git/hooks is the obvious
    advice and it is not enough: core.hooksPath puts the hooks back. So the
    config has to be named in the file, and so does the reason -- advice a
    reader does not understand is advice they skip.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    readme = prose(clone)
    assert 'rm -rf .git/hooks .git/config .git/objects/info/alternates' in readme
    assert 'git init' in readme
    assert 'git fsck' in readme
    assert 'core.hooksPath' in readme
    assert 'instructions rather than as data' in readme


def test_the_readme_says_to_name_the_commit_by_object_id(origin_repo: Path, tmp_path: Path) -> None:
    """An fsck proves the objects are intact, not that the history is genuine.

    A fabricated commit is a perfectly valid object, and a branch that came in
    the tarball points wherever the tarball says. The one check a tampered
    tarball cannot answer wrongly is a full object ID from somewhere else, so
    the file has to say to use one.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    readme = prose(clone)
    assert 'full object ID' in readme


def test_the_safety_advice_comes_before_the_usage_advice(origin_repo: Path, tmp_path: Path) -> None:
    """Advice arriving after the command that needed it is decoration.

    A reader skims until they find something to type. If the fetch command is
    above the config warning, that is what they run, and the warning may as
    well not be in the file.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    readme = (clone / '.git' / README_NAME).read_text()
    assert readme.index('rm -rf .git/hooks') < readme.index('git fetch --depth=1')


def test_every_fetch_the_readme_shows_is_depth_limited(origin_repo: Path, tmp_path: Path) -> None:
    """The whole saving rests on this one flag, so no example may omit it.

    .git/shallow says where the history was cut, not how deep it is, and a
    fetch stops walking only at the commits on that list. In a merge-heavy
    tree the merged side branches fork below the cut and are never reached by
    it, so an undepth-limited fetch walks them to the bottom: one plain "git
    remote update" on torvalds/linux.git pulled 2.6 GiB, which is the failure
    this whole tool exists to prevent, arriving one step later than expected.

    A "git fetch" in this file therefore either carries --depth=1 or is being
    forbidden. --unshallow and --deepen are covered by their own test; this
    one exists so that a future edit cannot add a fourth, friendlier example
    that quietly drops the flag.
    """
    readme = prose(extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x'))
    at = readme.find('git fetch')
    assert at >= 0, 'the readme has to show a fetch at all'
    while at >= 0:
        after = readme[at : at + 40]
        before = readme[max(0, at - 40) : at].lower()
        assert '--depth=1' in after or 'not run' in before, readme[max(0, at - 40) : at + 40]
        at = readme.find('git fetch', at + 1)


def test_the_readme_forbids_git_remote_update_wherever_it_names_it(origin_repo: Path, tmp_path: Path) -> None:
    """The command everyone reaches for is the expensive one here.

    "git remote update" takes no --depth, so unlike an ordinary fetch there is
    no way to spell it safely in a shallow tree -- the only correct advice is
    not to run it. It stays in the file because a reader who does not see it
    named will assume it is fine; it must never appear without a refusal
    attached, which is what this pins.
    """
    readme = prose(extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x'))
    at = readme.find('git remote update')
    assert at >= 0, 'saying nothing about it leaves the reader to guess'
    while at >= 0:
        window = readme[max(0, at - 60) : at].lower()
        assert 'never run' in window or 'not run' in window or 'do not' in window, readme[max(0, at - 60) : at + 20]
        at = readme.find('git remote update', at + 1)


def test_the_readme_steers_away_from_unshallowing(origin_repo: Path, tmp_path: Path) -> None:
    """The obvious next command is the one thing this tool exists to prevent.

    "git fetch --unshallow" makes the server build the whole history as a
    single pack -- worse than the depth-1 clones these tarballs replace, and
    an artifact that suggested it would undo its own purpose at scale. So it
    appears only as an instruction not to run it, and this test pins that the
    two never drift apart into a bare recommendation again.

    Read on the collapsed text rather than line by line, because "Do not" and
    the command it forbids can perfectly well fall on either side of a line
    break -- and did, the first time this file was hand-wrapped.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    readme = prose(clone)
    assert 'clone a fresh copy' in readme
    for flag in ('--unshallow', '--deepen'):
        at = readme.find(flag)
        # Not mentioning it at all would be fine as well. What must never
        # happen is a mention that reads as advice, which is what this
        # asserts: every one of them has a "not run" close in front of it.
        while at >= 0:
            assert 'not run' in readme[max(0, at - 40) : at].lower(), readme[max(0, at - 40) : at + 40]
            at = readme.find(flag, at + 1)


def test_the_readme_travels_inside_the_tarball(origin_repo: Path, tmp_path: Path) -> None:
    """It is only useful if it is in the artifact, not just in the scratch clone."""
    tarpath = build(origin_repo, tmp_path / 'out')
    with tarfile.open(tarpath) as tar:
        assert f'linux/.git/{README_NAME}' in tar.getnames()


def test_the_readme_is_not_untracked_noise_in_the_working_tree(origin_repo: Path, tmp_path: Path) -> None:
    """It lives in .git/, so checking the branch out leaves git status clean.

    At the top of the tree it would be a file we invented showing up as
    untracked in somebody else's repository, which is our mess in their
    workspace.
    """
    clone = extract(build(origin_repo, tmp_path / 'out'), tmp_path / 'x')
    git('checkout', '-q', 'master', cwd=clone)
    assert git('status', '--porcelain', cwd=clone).strip() == ''


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


def stale_workdir(outdir: Path, age: int, name: str = '.shallowtar-abcd1234') -> Path:
    """A scratch directory of the shape a killed run leaves behind."""
    workdir = outdir / name
    (workdir / 'linux' / '.git').mkdir(parents=True)
    (workdir / 'linux' / '.git' / 'HEAD').write_text('ref: refs/heads/master\n')
    os.utime(workdir, (NOW - age, NOW - age))
    return workdir


def test_a_scratch_directory_a_killed_run_left_behind_is_swept_up(tmp_path: Path) -> None:
    """A signal skips the "finally", so the next run is what cleans up.

    The whole directory goes, not just its name: in a real kill this is most
    of a clone of linux.git, which is the only reason any of this matters.
    """
    outdir = tmp_path / 'out'
    outdir.mkdir()
    workdir = stale_workdir(outdir, age=2 * DAY)
    sweep_stale_workdirs(outdir, NOW)
    assert not workdir.exists()


def test_a_scratch_directory_something_may_still_be_writing_to_is_left_alone(tmp_path: Path) -> None:
    """Overlapping runs are a deployment mistake, not a reason to eat a clone."""
    outdir = tmp_path / 'out'
    outdir.mkdir()
    workdir = stale_workdir(outdir, age=60)
    sweep_stale_workdirs(outdir, NOW)
    assert workdir.exists()


def test_the_sweep_reaches_the_directory_the_scratch_clone_is_actually_made_in(tmp_path: Path) -> None:
    """Scratch dirs appear beside the tarball, which is nested under outdir.

    Sweeping only the top of the output directory would find nothing at all on
    a real tree, where every artifact lives under its repository's path.
    """
    outdir = tmp_path / 'out'
    nested = outdir / 'pub' / 'scm' / 'stable'
    nested.mkdir(parents=True)
    workdir = stale_workdir(nested, age=2 * DAY)
    sweep_stale_workdirs(outdir, NOW)
    assert not workdir.exists()


def test_the_sweep_leaves_the_published_tarballs_where_they_are(tmp_path: Path) -> None:
    outdir = tmp_path / 'out'
    outdir.mkdir()
    tarball = outdir / 'linux.master.shallow.20250911.abc1234.tar'
    tarball.write_text('not really a tarball\n')
    os.utime(tarball, (NOW - 400 * DAY, NOW - 400 * DAY))
    sweep_stale_workdirs(outdir, NOW)
    assert tarball.exists()


def test_the_sweep_does_not_walk_into_a_scratch_directory_it_is_keeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Descending would cost more than the rest of the sweep put together.

    The young directory is the case that can show this. For one being deleted,
    the rmtree happens before os.walk gets around to descending -- walk opens
    each subdirectory lazily, and a directory that has gone just yields
    nothing -- so the pruning looks unnecessary there and is not. A live clone
    that the sweep is deliberately leaving alone is still sitting on disk when
    walk reaches it, and that is a whole linux.git of directory entries.
    """
    outdir = tmp_path / 'out'
    outdir.mkdir()
    workdir = stale_workdir(outdir, age=60)
    # A file per object, the way a real clone's scratch directory holds one.
    for num in range(5):
        (workdir / 'linux' / '.git' / f'object{num}').write_text('x')

    visited = []
    real = os.walk

    # No **kwargs: the sweep passes only the directory, and spelling that out
    # keeps os.walk's overloads from having to be reasoned about here.
    def spy(top: Path) -> Iterator[tuple[str, list[str], list[str]]]:
        for dirpath, dirnames, filenames in real(top):
            visited.append(dirpath)
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(shallowtar.os, 'walk', spy)
    sweep_stale_workdirs(outdir, NOW)
    assert visited == [str(outdir)]
    assert workdir.exists()


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
