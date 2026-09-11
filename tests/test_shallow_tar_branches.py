# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-shallow-tar picks which branches of a repository to publish."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from grokmirror.shallowtar import Branch, check_collisions, select_branches, slug_branch

# A fixed "now" so a test can say "this branch is 100 days stale" and mean it.
NOW = 1757577600
DAY = 86400


def make_repo(path: Path, branches: dict[str, int]) -> Path:
    """A bare repo with one branch per entry, tip aged that many days back.

    Every branch gets its own single commit rather than a shared history,
    which keeps each tip's committer date independent -- the thing under test
    here is the age filter, and a shared parent would let one branch's date
    leak into another's.
    """
    work = path.parent / f'{path.name}-work'
    subprocess.run(['git', 'init', '-q', '-b', 'trunk', str(work)], check=True, capture_output=True)
    subprocess.run(['git', 'init', '-q', '--bare', str(path)], check=True, capture_output=True)
    for num, (name, age) in enumerate(branches.items()):
        stamp = f'{NOW - age * DAY} +0000'
        env = dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        (work / 'file.txt').write_text(f'{name} at {age} days\n')
        # A fresh orphan name per branch: the staging branch from the last
        # round is still there, and --orphan will not reuse it.
        for args in (['checkout', '-q', '--orphan', f'staging{num}'], ['add', 'file.txt']):
            subprocess.run(['git', *args], cwd=work, check=True, capture_output=True)
        subprocess.run(
            ['git', 'commit', '-q', '-m', f'tip of {name}'], cwd=work, env=env, check=True, capture_output=True
        )
        subprocess.run(
            ['git', 'push', '-q', str(path), f'HEAD:refs/heads/{name}'], cwd=work, check=True, capture_output=True
        )
    return path


def names(branches: list[Branch]) -> list[str]:
    return [b.name for b in branches]


def test_a_glob_picks_the_branches_it_matches(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'linux-6.18.y': 1, 'linux-6.12.y': 1, 'master': 1, 'testing': 1})
    assert names(select_branches(repo, 'linux-*.y', NOW)) == ['linux-6.12.y', 'linux-6.18.y']


def test_a_repository_with_no_matching_branch_publishes_nothing(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'master': 1})
    assert select_branches(repo, 'linux-*.y', NOW) == []


def test_the_recorded_tip_is_the_branch_tip(tmp_path: Path) -> None:
    """The published filename is built from this, so it had better be right."""
    repo = make_repo(tmp_path / 'linux.git', {'master': 1})
    tip = subprocess.run(
        ['git', '--git-dir', str(repo), 'rev-parse', 'master'], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert [b.tip for b in select_branches(repo, 'master', NOW)] == [tip]


def test_a_branch_nobody_maintains_any_more_is_retired(tmp_path: Path) -> None:
    """An end-of-life stable branch stops receiving commits, so age retires it."""
    repo = make_repo(tmp_path / 'linux.git', {'linux-6.18.y': 2, 'linux-4.4.y': 400})
    assert names(select_branches(repo, 'linux-*.y', NOW, maxrefage=90)) == ['linux-6.18.y']


def test_a_max_ref_age_of_zero_keeps_every_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'linux-6.18.y': 2, 'linux-4.4.y': 400})
    assert names(select_branches(repo, 'linux-*.y', NOW, maxrefage=0)) == ['linux-4.4.y', 'linux-6.18.y']


def test_the_cap_keeps_the_branches_that_are_still_moving(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'linux-6.18.y': 1, 'linux-6.12.y': 5, 'linux-6.6.y': 200})
    assert names(select_branches(repo, 'linux-*.y', NOW, maxbranches=2)) == ['linux-6.12.y', 'linux-6.18.y']


def test_a_cap_of_zero_publishes_everything_that_matches(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'linux-6.18.y': 1, 'linux-6.12.y': 5, 'linux-6.6.y': 200})
    assert len(select_branches(repo, 'linux-*.y', NOW, maxbranches=0)) == 3


def test_branches_come_back_sorted_by_name_not_by_age(tmp_path: Path) -> None:
    """The by-date sort the cap needs must not leak into the returned order.

    Asserting this without a cap in play would prove nothing: for-each-ref
    already lists refs in name order, so the result comes out sorted whether
    or not anything sorts it. The cap is the one path that reorders, so it is
    the one that can show the sort doing work -- here the newest branch is
    the one that sorts last by name.
    """
    repo = make_repo(tmp_path / 'linux.git', {'alpha': 9, 'middle': 5, 'zebra': 1})
    assert names(select_branches(repo, '*', NOW, maxbranches=2)) == ['middle', 'zebra']


def test_a_repository_git_cannot_read_publishes_nothing(tmp_path: Path) -> None:
    """A broken repo skips, rather than taking the whole run down with it."""
    missing = tmp_path / 'nope.git'
    assert select_branches(missing, '*', NOW) == []


def test_a_slash_in_a_branch_name_becomes_one_path_component(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'for-next/core': 1})
    selected = select_branches(repo, 'for-next/*', NOW)
    assert [(b.name, b.slug) for b in selected] == [('for-next/core', 'for-next-core')]


@pytest.mark.parametrize(
    'name',
    [
        'linux-6.18.y',  # dots are ordinary
        '-dashlead',  # legal, and safe because the slug is never first in the filename
        'CAPS',
        'uni-café',
    ],
)
def test_a_branch_name_git_allows_survives_slugging_unchanged(name: str) -> None:
    """git-check-ref-format already rejects everything hostile to a filename.

    This pins that we are not scrubbing more than the one character that
    needs it: someone reading slug_branch() later should not add defensive
    replacements believing they were ever load-bearing.
    """
    subprocess.run(['git', 'check-ref-format', f'refs/heads/{name}'], check=True)
    assert slug_branch(name) == name


def test_two_branches_that_would_share_a_filename_publish_neither(tmp_path: Path) -> None:
    """Last-writer-wins here would look exactly like success, which is worse.

    Both branches are dropped rather than one, because publishing a tarball
    whose contents depend on which branch git listed last is the
    silently-wrong-content failure this tool exists to avoid.
    """
    repo = make_repo(tmp_path / 'linux.git', {'for-next/core': 1, 'for-next-core': 1, 'innocent': 1})
    assert names(select_branches(repo, '*', NOW)) == ['innocent']


def test_the_collision_is_reported_loudly(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'for-next/core': 1, 'for-next-core': 1})
    select_branches(repo, '*', NOW)
    # Both culprits and the name they fought over, so the operator can act on
    # the message without going and reading the code.
    assert 'for-next-core, for-next/core' in caplog.text
    assert 'publish as "for-next-core"' in caplog.text
    # The collision record itself, not records[0]: whether git's own debug
    # chatter is captured depends on the log level another test left behind,
    # and the level being asserted here is the collision's. Matched on the
    # start of the message rather than on "collision" appearing anywhere in
    # it, because the debug line names the repository -- whose path, in this
    # test, is built from the test's own name.
    levels = {rec.levelname for rec in caplog.records if rec.getMessage().startswith('  collision:')}
    assert levels == {'CRITICAL'}


def test_a_branch_the_cap_dropped_cannot_cause_a_collision(tmp_path: Path) -> None:
    """The cap runs first, so a branch not being published cannot collide.

    Checking collisions before the cap would let a stale branch nobody asked
    for veto the publication of one that is actually being maintained.
    """
    repo = make_repo(tmp_path / 'linux.git', {'for-next/core': 1, 'for-next-core': 300})
    assert names(select_branches(repo, '*', NOW, maxbranches=1)) == ['for-next/core']


def test_a_branch_max_ref_age_retired_cannot_cause_a_collision(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / 'linux.git', {'for-next/core': 1, 'for-next-core': 300})
    assert names(select_branches(repo, '*', NOW, maxrefage=90)) == ['for-next/core']


def test_check_collisions_keeps_everything_when_there_is_no_clash() -> None:
    branches = [Branch('a', 'a', 'aaa'), Branch('b', 'b', 'bbb')]
    assert check_collisions(branches) == branches
