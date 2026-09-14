# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-shallow-tar's command line: which repositories it is told to publish."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from grokmirror.shallowtar import Pattern, match_patterns, parse_pattern

from support import GrokTree

BASE = 'https://git.example.com'


def published(tree: GrokTree) -> list[str]:
    """Every real tarball under the output directory, as relative paths."""
    outdir = tree.root / 'shallow'
    if not outdir.exists():
        return []
    return sorted(str(p.relative_to(outdir)) for p in outdir.rglob('*.tar') if not p.is_symlink())


# -- argument parsing ---------------------------------------------------------


def test_a_pattern_splits_into_a_repository_and_a_branch_glob() -> None:
    assert parse_pattern('*/stable/linux.git:linux-*.y') == Pattern('*/stable/linux.git', 'linux-*.y')


def test_the_first_colon_is_the_separator() -> None:
    """A colon is legal in a path and illegal in a refname, so it splits left."""
    assert parse_pattern('/pub/odd:name/linux.git:master') == Pattern('/pub/odd:name/linux.git', 'master')


@pytest.mark.parametrize('value', ['*/linux.git', '', ':master', '*/linux.git:'])
def test_a_pattern_that_is_not_two_halves_is_refused(value: str) -> None:
    """Silently treating it as one half or the other would publish the wrong set."""
    with pytest.raises(Exception, match='REPOGLOB:BRANCHGLOB'):
        parse_pattern(value)


def test_a_repository_glob_matches_with_or_without_the_leading_slash() -> None:
    """Manifest keys are absolute, but both spellings are documented as working."""
    patterns = [Pattern('pub/scm/*.git', 'master')]
    assert match_patterns('/pub/scm/linux.git', patterns) == ['master']


def test_every_pattern_naming_a_repository_contributes_its_branches() -> None:
    """One repository can be named twice, e.g. for its stable and its next branches."""
    patterns = [Pattern('/test/*.git', 'master'), Pattern('/test/one.git', 'topic-*'), Pattern('/other/*', 'nope')]
    assert match_patterns('/test/one.git', patterns) == ['master', 'topic-*']


def test_a_repository_no_pattern_names_contributes_nothing() -> None:
    assert match_patterns('/test/one.git', [Pattern('/other/*.git', 'master')]) == []


# -- the command itself -------------------------------------------------------


@pytest.mark.slow
def test_a_repository_no_pattern_names_publishes_nothing(tree: GrokTree) -> None:
    """Publication is opt-in: these artifacts are far too big to default to "all"."""
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git')
    tree.run_manifest()
    tree.run_shallow_tar('-v', '--clone-url-base', BASE, '--branches', '/test/one.git:master')

    names = published(tree)
    assert len(names) == 1
    assert names[0].startswith('test/one.master.shallow.')


@pytest.mark.slow
def test_a_repeated_switch_accumulates_instead_of_overwriting(tree: GrokTree) -> None:
    """append, not store: the second --branches must not silently drop the first."""
    tree.add_repo('test/one.git')
    tree.add_repo('test/two.git')
    tree.run_manifest()
    tree.run_shallow_tar(
        '-v',
        '--clone-url-base',
        BASE,
        '--branches',
        '/test/one.git:master',
        '--branches',
        '/test/two.git:master',
    )

    assert [name.split('/')[0] for name in published(tree)] == ['test', 'test']
    assert {name.split('.')[0].split('/')[1] for name in published(tree)} == {'one', 'two'}


@pytest.mark.slow
def test_a_pattern_with_no_colon_stops_the_run(tree: GrokTree) -> None:
    """argparse refuses it up front rather than the run misreading it."""
    tree.add_repo('test/one.git')
    tree.run_manifest()
    res = tree.run_shallow_tar('--clone-url-base', BASE, '--branches', '/test/one.git', expect=2)
    assert 'REPOGLOB:BRANCHGLOB' in res.stderr
    assert published(tree) == []


@pytest.mark.slow
def test_a_run_sweeps_up_after_a_run_that_was_killed(tree: GrokTree) -> None:
    """End to end, because the sweep is only useful if the run actually calls it."""
    tree.add_repo('test/one.git')
    tree.run_manifest()
    workdir = tree.root / 'shallow' / 'test' / '.shallowtar-deadbeef'
    workdir.mkdir(parents=True)
    (workdir / 'one').mkdir()
    stale = time.time() - 2 * 86400
    os.utime(workdir, (stale, stale))

    tree.run_shallow_tar('-v', '--clone-url-base', BASE, '--branches', '/test/one.git:master')
    assert not workdir.exists()
    # The sweep runs before the publishing, so this run's own output is proof
    # it did not take the live scratch directory with it.
    assert len(published(tree)) == 1


@pytest.mark.slow
def test_the_origin_in_the_tarball_is_the_public_site(tree: GrokTree, tmp_path: Path) -> None:
    """The manifest key appends to --clone-url-base, giving the public clone URL."""
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_shallow_tar('-v', '--clone-url-base', BASE + '/', '--branches', '/test/*.git:master')

    into = tmp_path / 'unpacked'
    into.mkdir()
    tarball = tree.root / 'shallow' / published(tree)[0]
    subprocess.run(['tar', '-xf', str(tarball)], cwd=into, check=True, capture_output=True)
    url = subprocess.run(
        ['git', 'config', '--get', 'remote.origin.url'],
        cwd=str(into / 'one'),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert url == f'{BASE}/test/one.git'


@pytest.mark.slow
def test_the_sidecar_names_the_repository_it_came_from(tree: GrokTree) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.run_shallow_tar('-v', '--clone-url-base', BASE, '--branches', '/test/one.git:master')

    sidecar = tree.root / 'shallow' / 'test' / 'one.master.shallow.latest.tar.json'
    assert json.loads(sidecar.read_text())['repo'] == '/test/one.git'
