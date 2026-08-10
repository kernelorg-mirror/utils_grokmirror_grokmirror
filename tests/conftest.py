# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures and environment isolation for the test suite.

The isolation here is doing real work, so it is worth reading before adding
tests:

* git runs with its own HOME and its own global config, so the developer's
  ~/.gitconfig (hooks, templates, default branch name, commit.gpgsign) cannot
  change the outcome. The tests are also run on 3.9 through 3.14 by
  ci-matrix.sh, so nothing may depend on the ambient environment.
* Every test starts with the current directory inside a decoy repository. Several
  real bugs in this tree were of the form "git was asked about no repository in
  particular, so it answered about the current directory" -- running from a
  repository that no test should ever touch turns that class of bug from
  invisible into an obvious failed assertion.
* Author and committer identity and dates are fixed, so fingerprints and
  timestamps are reproducible.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from support import BASE_TIMESTAMP, GrokTree, make_decoy_repo


def pytest_report_header() -> str:
    """Note the git version in the header; behaviour does vary between them."""
    version = subprocess.run(['git', '--version'], capture_output=True, text=True, check=False).stdout.strip()
    return f'grokmirror tests using: {version or "git NOT FOUND"}'


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate git and the tests from the developer's environment."""
    home = tmp_path / 'home'
    home.mkdir()
    gitconfig = home / '.gitconfig'
    # A real config file rather than /dev/null: git needs an identity, and
    # pinning init.defaultBranch keeps both the hint noise and the "master or
    # main?" ambiguity out of the tests.
    gitconfig.write_text(
        '[user]\n'
        '\tname = Grokmirror Tests\n'
        '\temail = tests@grokmirror.invalid\n'
        '[init]\n'
        '\tdefaultBranch = master\n'
        '[commit]\n'
        '\tgpgsign = false\n'
        '[gc]\n'
        '\tauto = 0\n'
        '[protocol "file"]\n'
        '\tallow = always\n'
    )
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(gitconfig))
    monkeypatch.setenv('GIT_CONFIG_SYSTEM', '/dev/null')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_AUTHOR_NAME', 'Grokmirror Tests')
    monkeypatch.setenv('GIT_AUTHOR_EMAIL', 'tests@grokmirror.invalid')
    monkeypatch.setenv('GIT_COMMITTER_NAME', 'Grokmirror Tests')
    monkeypatch.setenv('GIT_COMMITTER_EMAIL', 'tests@grokmirror.invalid')
    monkeypatch.setenv('GIT_AUTHOR_DATE', f'{BASE_TIMESTAMP} +0000')
    monkeypatch.setenv('GIT_COMMITTER_DATE', f'{BASE_TIMESTAMP} +0000')
    monkeypatch.setenv('TZ', 'UTC')
    monkeypatch.setenv('LC_ALL', 'C')
    # Anything inherited from the shell that would redirect git elsewhere.
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        monkeypatch.delenv(name, raising=False)
    yield home


@pytest.fixture
def decoy(tmp_path: Path) -> Path:
    """A repository that no test should ever be reading from."""
    return make_decoy_repo(tmp_path / 'decoy')


@pytest.fixture(autouse=True)
def _run_from_decoy(decoy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never run a test from the grokmirror checkout itself.

    In-process calls that ask git about "no repository" then land in the decoy,
    where the answers are recognizable, instead of in this source tree, where
    they look plausible.
    """
    monkeypatch.chdir(decoy)


@pytest.fixture
def tree(tmp_path: Path, decoy: Path) -> GrokTree:
    """An empty grokmirror toplevel to build repositories in."""
    return GrokTree(tmp_path / 'mirror', decoy)


@pytest.fixture
def origin(tmp_path: Path, decoy: Path) -> GrokTree:
    """A second tree, to act as the origin that a mirror pulls from."""
    return GrokTree(tmp_path / 'origin', decoy)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line('markers', 'slow: tests that repack or fsck real repositories')


@pytest.fixture(scope='session', autouse=True)
def _require_git() -> None:
    if shutil.which('git') is None:  # pragma: no cover - environment sanity
        pytest.skip('git is not installed', allow_module_level=True)
