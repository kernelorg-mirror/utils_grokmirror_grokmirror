# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the test scaffolding itself.

Everything else in the suite trusts these fixtures, so if one of them quietly
stops isolating the environment or stops building real repositories, dozens of
tests start passing for the wrong reason. These assertions are cheap insurance
against that.
"""

from __future__ import annotations

import os
import subprocess
from configparser import ConfigParser
from pathlib import Path

import pytest

from support import DECOY_URL, GrokTree, git


def test_runs_from_the_decoy_repo(decoy: Path) -> None:
    # The whole point of the decoy: a git command with no repository argument
    # answers about it, not about the grokmirror checkout.
    assert Path.cwd() == decoy
    assert git('rev-parse', '--abbrev-ref', 'HEAD').strip() == 'decoybranch'
    assert DECOY_URL in git('remote', '-v')


def test_git_is_isolated_from_the_users_config(isolated_env: Path) -> None:
    assert os.environ['HOME'] == str(isolated_env)
    assert git('config', 'user.email').strip() == 'tests@grokmirror.invalid'
    assert git('config', 'init.defaultBranch').strip() == 'master'


def test_commits_are_reproducible(tree: GrokTree, origin: GrokTree) -> None:
    # The same history built twice in two different trees must give the same
    # hashes, or fingerprint assertions elsewhere would be untestable.
    assert tree.source('shared', commits=2).head() == origin.source('shared', commits=2).head()


def test_add_repo_builds_a_real_bare_repo(tree: GrokTree) -> None:
    fullpath = tree.add_repo('test/repo.git')
    assert git('rev-parse', '--is-bare-repository', cwd=fullpath).strip() == 'true'
    assert git('rev-parse', 'refs/heads/master', cwd=fullpath).strip()
    assert git('symbolic-ref', 'HEAD', cwd=fullpath).strip() == 'refs/heads/master'
    assert not (fullpath / 'git-daemon-export-ok').exists()


def test_repos_from_one_source_share_roots(tree: GrokTree) -> None:
    # This sharing is what makes grok-fsck migrate repositories into a common
    # objstore repo, so several tests depend on it.
    one = tree.add_repo('test/one.git')
    two = tree.add_repo('test/two.git')
    root_one = git('rev-list', '--max-parents=0', 'refs/heads/master', cwd=one).split()
    root_two = git('rev-list', '--max-parents=0', 'refs/heads/master', cwd=two).split()
    assert root_one == root_two


def test_repos_from_different_sources_do_not_share_roots(tree: GrokTree) -> None:
    one = tree.add_repo('test/one.git', source='alpha')
    two = tree.add_repo('test/two.git', source='beta')
    root_one = git('rev-list', '--max-parents=0', 'refs/heads/master', cwd=one).split()
    root_two = git('rev-list', '--max-parents=0', 'refs/heads/master', cwd=two).split()
    assert root_one != root_two


def test_empty_repo_has_no_refs(tree: GrokTree) -> None:
    fullpath = tree.add_empty_repo('test/empty.git')
    assert git('for-each-ref', cwd=fullpath).strip() == ''


def test_config_defaults_and_overrides(tree: GrokTree) -> None:
    cfgfile = tree.write_config({'remote': {'site': 'https://example.com'}, 'core': {'loglevel': 'info'}})
    config = ConfigParser()
    config.read(cfgfile)
    assert config['core']['toplevel'] == str(tree.toplevel)
    assert config['core']['loglevel'] == 'info'
    assert config['remote']['site'] == 'https://example.com'
    assert config['fsck']['statusfile'] == str(tree.statusfile)


def test_config_can_drop_a_default(tree: GrokTree) -> None:
    # Needed by the tests that check error handling on incomplete configs.
    cfgfile = tree.write_config({'core': {'toplevel': ''}})
    config = ConfigParser()
    config.read(cfgfile)
    assert 'toplevel' not in config['core']


def test_manifest_round_trip(tree: GrokTree) -> None:
    tree.write_manifest({'/test/repo.git': {'fingerprint': 'abc', 'modified': 1}})
    assert tree.read_manifest()['/test/repo.git']['fingerprint'] == 'abc'


def test_manifest_round_trip_uncompressed(tree: GrokTree) -> None:
    plain = tree.root / 'manifest.js'
    tree.write_manifest({'/test/repo.git': {'modified': 2}}, manifile=plain)
    assert tree.read_manifest(plain)['/test/repo.git']['modified'] == 2


def test_run_checks_the_exit_code(tree: GrokTree) -> None:
    with pytest.raises(AssertionError, match='exited with'):
        tree.run('grok-manifest', '--nonsense-option')


def test_run_accepts_an_expected_failure(tree: GrokTree) -> None:
    res = tree.run('grok-manifest', '--nonsense-option', expect=2)
    assert 'unrecognized arguments' in res.stderr


def test_run_fails_on_a_traceback(tree: GrokTree) -> None:
    # Simulate a crashing command: run() must reject a traceback even when the
    # exit code is the one we expected, since a cron-driven mirror that dies
    # with a traceback is always a bug.
    with pytest.raises(AssertionError, match='crashed'):
        tree.run('python3', '-c', 'raise RuntimeError("boom")', expect=1)


def test_run_can_allow_a_traceback(tree: GrokTree) -> None:
    res = tree.run('python3', '-c', 'raise RuntimeError("boom")', expect=1, allow_traceback=True)
    assert 'RuntimeError' in res.stderr


def test_subprocesses_inherit_the_isolated_environment() -> None:
    # run() relies on this: grok-* subprocesses must see the same fake HOME.
    res = subprocess.run(
        ['python3', '-c', 'import os; print(os.environ["HOME"])'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == os.environ['HOME']
