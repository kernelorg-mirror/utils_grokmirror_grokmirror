# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-shallow-tar publishes dated tarballs behind a fixed "latest" name."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from grokmirror.shallowtar import Branch, plan_artifact, prune_tarballs, publish_branch, select_branches

from support import Source, git

pytestmark = pytest.mark.slow

NOW = 1757577600  # 2025-09-11 UTC
DAY = 86400
REPO = '/pub/scm/linux/kernel/git/stable/linux.git'
PUBLIC_URL = 'https://git.example.com/pub/scm/linux/kernel/git/stable/linux.git'


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    bare = tmp_path / 'linux.git'
    git('init', '-q', '--bare', str(bare))
    source = Source(tmp_path / 'work', branch='master')
    source.commit()
    source.push(bare)
    return bare


def publish(repo: Path, outdir: Path, now: int = NOW, branch: str = 'master') -> bool:
    selected = select_branches(repo, branch, now)
    assert len(selected) == 1
    return publish_branch(repo, outdir, REPO, selected[0], PUBLIC_URL, now)


def published_dir(outdir: Path) -> Path:
    return outdir / 'pub/scm/linux/kernel/git/stable'


def tarballs(outdir: Path) -> list[str]:
    """Every real tarball on disk, oldest first, symlinks excluded."""
    found = [p for p in published_dir(outdir).glob('*.tar') if not p.is_symlink()]
    return [p.name for p in sorted(found, key=lambda p: p.stat().st_mtime)]


def advance(repo: Path, work: Path, branch: str = 'master') -> str:
    """Put one new commit on the origin and return its sha.

    The content is seeded from the current tip rather than from a counter,
    because Source restarts its counter every time one is constructed over an
    existing directory -- which would rewrite the same bytes and leave git
    with nothing to commit.
    """
    source = Source(work, branch=branch)
    sha = source.commit(content=f'after {source.head()[:12]}\n')
    source.push(repo)
    return sha


def test_the_filename_carries_the_date_and_the_tip(origin_repo: Path, tmp_path: Path) -> None:
    """The name is the whole state file, so it had better say what is inside."""
    outdir = tmp_path / 'out'
    assert publish(origin_repo, outdir)
    tip = git('--git-dir', str(origin_repo), 'rev-parse', 'master').strip()
    assert tarballs(outdir) == [f'linux.master.shallow.20250911.{tip[:7]}.tar']


def test_the_branch_sits_between_the_repository_and_the_date(tmp_path: Path) -> None:
    """One flat directory of siblings, not a directory per branch."""
    artifact = plan_artifact(tmp_path / 'out', REPO, Branch('linux-6.18.y', 'linux-6.18.y', 'dafd9ab' + 'e' * 33), NOW)
    assert artifact.directory == tmp_path / 'out/pub/scm/linux/kernel/git/stable'
    assert artifact.dated == 'linux.linux-6.18.y.shallow.20250911.dafd9ab.tar'
    assert artifact.latest == 'linux.linux-6.18.y.shallow.latest.tar'


def test_strip_prefix_lifts_the_shared_head_off_the_layout(tmp_path: Path) -> None:
    """The point of the switch: "stable/linux..." instead of five dead levels.

    The output directory already says what these files are, so repeating
    "pub/scm/linux/kernel/git" inside it only makes the URL longer.
    """
    artifact = plan_artifact(
        tmp_path / 'out',
        REPO,
        Branch('linux-6.18.y', 'linux-6.18.y', 'dafd9ab' + 'e' * 33),
        NOW,
        strip_prefix='/pub/scm/linux/kernel/git',
    )
    assert artifact.directory == tmp_path / 'out/stable'
    # Only the directory moves. The filename still names the repository, the
    # branch and the commit, because that is what makes it self-describing.
    assert artifact.dated == 'linux.linux-6.18.y.shallow.20250911.dafd9ab.tar'


@pytest.mark.parametrize('prefix', ['pub/scm/linux/kernel/git', '/pub/scm/linux/kernel/git/'])
def test_the_prefix_is_read_the_same_with_or_without_its_slashes(tmp_path: Path, prefix: str) -> None:
    """Manifest keys are absolute and operators type both spellings."""
    artifact = plan_artifact(tmp_path / 'out', REPO, Branch('master', 'master', 'd' * 40), NOW, strip_prefix=prefix)
    assert artifact.directory == tmp_path / 'out/stable'


def test_a_repository_outside_the_prefix_keeps_its_full_path(tmp_path: Path) -> None:
    """Publishing it somewhere odd beats not publishing it at all.

    --branches named this repository explicitly, so silently dropping it
    because a *layout* switch did not apply would lose an artifact somebody
    asked for. Nothing here can tell a mistyped prefix from a repository that
    genuinely lives elsewhere.
    """
    artifact = plan_artifact(
        tmp_path / 'out',
        '/pub/scm/git/git.git',
        Branch('master', 'master', 'd' * 40),
        NOW,
        strip_prefix='/pub/scm/linux/kernel/git',
    )
    assert artifact.directory == tmp_path / 'out/pub/scm/git'


def test_a_prefix_matching_the_whole_parent_leaves_the_tarball_at_the_top(tmp_path: Path) -> None:
    """The fully-stripped case has no parent left to join, and must not crash."""
    artifact = plan_artifact(
        tmp_path / 'out',
        '/pub/scm/linux.git',
        Branch('master', 'master', 'd' * 40),
        NOW,
        strip_prefix='/pub/scm',
    )
    assert artifact.directory == tmp_path / 'out'
    assert artifact.dated.startswith('linux.master.shallow.')


def test_no_prefix_is_the_layout_we_had_before(tmp_path: Path) -> None:
    """The default has to be inert, or every existing deployment moves."""
    branch = Branch('master', 'master', 'd' * 40)
    assert plan_artifact(tmp_path / 'out', REPO, branch, NOW) == plan_artifact(
        tmp_path / 'out', REPO, branch, NOW, strip_prefix=''
    )


def test_a_slashed_branch_makes_no_directory_under_outdir(tmp_path: Path) -> None:
    bare = tmp_path / 'linux.git'
    git('init', '-q', '--bare', str(bare))
    source = Source(tmp_path / 'work', branch='for-next/core')
    source.commit()
    source.push(bare)

    outdir = tmp_path / 'out'
    assert publish(bare, outdir, branch='for-next/*')
    assert [p.name for p in published_dir(outdir).iterdir() if p.is_dir()] == []
    assert any(name.startswith('linux.for-next-core.shallow.') for name in tarballs(outdir))


def test_latest_points_at_the_dated_tarball_and_its_sidecar(origin_repo: Path, tmp_path: Path) -> None:
    outdir = tmp_path / 'out'
    assert publish(origin_repo, outdir)
    dated = tarballs(outdir)[0]

    link = published_dir(outdir) / 'linux.master.shallow.latest.tar'
    assert link.is_symlink()
    # Relative, so the tree survives being rsynced somewhere else entirely.
    assert str(link.readlink()) == dated
    assert str((published_dir(outdir) / 'linux.master.shallow.latest.tar.json').readlink()) == f'{dated}.json'


def test_the_sidecar_describes_the_tarball_beside_it(origin_repo: Path, tmp_path: Path) -> None:
    """A node reads this, then only ever asks for an immutable filename."""
    outdir = tmp_path / 'out'
    assert publish(origin_repo, outdir)
    dated = published_dir(outdir) / tarballs(outdir)[0]
    sidecar = json.loads(dated.with_name(f'{dated.name}.json').read_text())

    tip = git('--git-dir', str(origin_repo), 'rev-parse', 'master').strip()
    assert sidecar['tarball'] == dated.name
    assert sidecar['repo'] == REPO
    assert sidecar['branch'] == 'master'
    assert sidecar['tip'] == tip
    assert sidecar['created'] == NOW
    assert sidecar['depth'] == 1
    assert sidecar['size'] == dated.stat().st_size
    assert sidecar['sha256'] == _sha256(dated)
    # The filename abbreviates the tip the sidecar spells out, so a node that
    # read either one is talking about the same commit.
    assert dated.name.endswith(f'.{tip[:7]}.tar')


def _sha256(path: Path) -> str:
    out = subprocess.run(['sha256sum', str(path)], check=True, capture_output=True, text=True).stdout
    return out.split()[0]


def test_an_unchanged_branch_republishes_nothing(origin_repo: Path, tmp_path: Path) -> None:
    """Cron runs daily; the content is what earns a new date, not the clock."""
    outdir = tmp_path / 'out'
    assert publish(origin_repo, outdir)
    first = tarballs(outdir)
    before = (published_dir(outdir) / first[0]).stat().st_mtime_ns

    assert publish(origin_repo, outdir, now=NOW + 3 * DAY)
    assert tarballs(outdir) == first
    assert (published_dir(outdir) / first[0]).stat().st_mtime_ns == before
    # And the date still reports the age of the content, not of the run.
    assert '20250911' in first[0]


def test_a_new_commit_moves_latest_and_keeps_the_old_tarball(origin_repo: Path, tmp_path: Path) -> None:
    """The one-cycle grace period, which nothing else would notice losing.

    A node that read "latest" from a frontend that has synced, and then asks
    for that dated file from one that has not, must still get its tarball.
    """
    outdir = tmp_path / 'out'
    assert publish(origin_repo, outdir)
    first = tarballs(outdir)[0]

    advance(origin_repo, tmp_path / 'work')
    assert publish(origin_repo, outdir, now=NOW + DAY)

    second = [name for name in tarballs(outdir) if name != first]
    assert len(second) == 1
    assert str((published_dir(outdir) / 'linux.master.shallow.latest.tar').readlink()) == second[0]
    # Still on disk, and still a tarball somebody can finish downloading.
    old = published_dir(outdir) / first
    assert old.is_file() and old.stat().st_size > 0
    assert (published_dir(outdir) / f'{first}.json').is_file()


def test_a_third_run_drops_the_tarball_from_two_cycles_back(origin_repo: Path, tmp_path: Path) -> None:
    """Otherwise the grace period quietly becomes unbounded retention."""
    outdir = tmp_path / 'out'
    assert publish(origin_repo, outdir)
    oldest = tarballs(outdir)[0]

    for day in (1, 2):
        advance(origin_repo, tmp_path / 'work')
        assert publish(origin_repo, outdir, now=NOW + day * DAY)

    assert len(tarballs(outdir)) == 2
    assert oldest not in tarballs(outdir)
    assert not (published_dir(outdir) / f'{oldest}.json').exists()


def test_pruning_goes_by_age_and_not_by_name(tmp_path: Path) -> None:
    """Two tarballs cut the same day sort by tip, and a tip is not a clock.

    Built by hand rather than by publishing three times, because with real
    tips the two orderings agree by luck most of the time -- the test has to
    choose names that disagree with the ages to mean anything at all.
    """
    outdir = tmp_path / 'out'
    artifact = plan_artifact(outdir, REPO, Branch('master', 'master', 'a' * 40), NOW)
    artifact.directory.mkdir(parents=True)

    # Newest first has the alphabetically *smallest* tip, so sorting by name
    # would keep exactly the wrong two.
    ages = {'aaaaaaa': NOW + 300, 'mmmmmmm': NOW + 200, 'zzzzzzz': NOW + 100}
    for tip, mtime in ages.items():
        stale = artifact.directory / f'{artifact.stem}.20250911.{tip}.tar'
        stale.write_bytes(b'tarball')
        stale.with_name(f'{stale.name}.json').write_text('{}')
        os.utime(stale, (mtime, mtime))

    prune_tarballs(artifact)
    assert sorted(tarballs(outdir)) == [
        f'{artifact.stem}.20250911.aaaaaaa.tar',
        f'{artifact.stem}.20250911.mmmmmmm.tar',
    ]


def test_the_date_in_the_name_is_utc(tmp_path: Path) -> None:
    """A generating host in Auckland must not name things a day ahead.

    The two frontends serving this are not in the same timezone as each other
    or as the builder, and a DST shift must never rename an artifact.
    """
    late = NOW + 14 * 3600  # 22:00 UTC on the 11th, 10:00 in Auckland on the 12th
    with_tz = os.environ.get('TZ')
    try:
        os.environ['TZ'] = 'Pacific/Auckland'
        time.tzset()
        artifact = plan_artifact(tmp_path / 'out', REPO, Branch('master', 'master', 'a' * 40), late)
    finally:
        if with_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = with_tz
        time.tzset()
    assert artifact.dated == f'{artifact.stem}.20250911.aaaaaaa.tar'


def test_the_sidecar_reaches_its_name_by_rename(origin_repo: Path, tmp_path: Path) -> None:
    """A node parsing a half-written sidecar gets a syntax error, not a retry.

    The sidecar is small enough that the window is narrow, but "narrow" is
    not a property that survives a CDN and a few thousand CI nodes.
    """
    renames: list[tuple[str, str]] = []
    real_replace = Path.replace

    def spy(self: Path, target: str | Path) -> Path:
        renames.append((self.name, Path(target).name))
        return real_replace(self, target)

    outdir = tmp_path / 'out'
    with mock.patch.object(Path, 'replace', spy):
        assert publish(origin_repo, outdir)

    dated = tarballs(outdir)[0]
    assert (f'.{dated}.json.tmp', f'{dated}.json') in renames


def test_a_branch_that_fails_to_generate_publishes_no_links(tmp_path: Path) -> None:
    outdir = tmp_path / 'out'
    missing = tmp_path / 'gone.git'
    assert not publish_branch(missing, outdir, REPO, Branch('master', 'master', 'a' * 40), PUBLIC_URL, NOW)
    assert list(published_dir(outdir).iterdir()) == []


def test_two_branches_of_one_repository_are_siblings(tmp_path: Path) -> None:
    bare = tmp_path / 'linux.git'
    git('init', '-q', '--bare', str(bare))
    for name in ('linux-6.12.y', 'linux-6.18.y'):
        source = Source(tmp_path / f'work-{name}', branch=name)
        source.commit()
        source.push(bare)

    outdir = tmp_path / 'out'
    for branch in select_branches(bare, 'linux-*.y', NOW):
        assert publish_branch(bare, outdir, REPO, branch, PUBLIC_URL, NOW)

    assert len(tarballs(outdir)) == 2
    assert {p.name for p in published_dir(outdir).glob('*.latest.tar')} == {
        'linux.linux-6.12.y.shallow.latest.tar',
        'linux.linux-6.18.y.shallow.latest.tar',
    }


def test_one_branch_does_not_prune_another(tmp_path: Path) -> None:
    """The stem is per-branch, so a busy branch cannot evict a quiet one."""
    bare = tmp_path / 'linux.git'
    git('init', '-q', '--bare', str(bare))
    for name in ('quiet', 'busy'):
        source = Source(tmp_path / f'work-{name}', branch=name)
        source.commit()
        source.push(bare)

    outdir = tmp_path / 'out'
    for branch in select_branches(bare, '*', NOW):
        assert publish_branch(bare, outdir, REPO, branch, PUBLIC_URL, NOW)

    for day in (1, 2, 3):
        advance(bare, tmp_path / 'work-busy', branch='busy')
        busy = select_branches(bare, 'busy', NOW)[0]
        assert publish_branch(bare, outdir, REPO, busy, PUBLIC_URL, NOW + day * DAY)

    quiet = [name for name in tarballs(outdir) if '.quiet.' in name]
    assert len(quiet) == 1
    assert len([name for name in tarballs(outdir) if '.busy.' in name]) == 2


def test_the_sidecar_survives_a_reader_arriving_mid_write(origin_repo: Path, tmp_path: Path) -> None:
    """Written under a dot-name and renamed, like everything else here."""
    outdir = tmp_path / 'out'
    assert publish(origin_repo, outdir)
    assert [p.name for p in published_dir(outdir).iterdir() if p.name.startswith('.')] == []
