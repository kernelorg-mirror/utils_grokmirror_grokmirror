# SPDX-License-Identifier: GPL-3.0-or-later
"""Characterization tests for grok-dumb-pull.

Unlike the manifest-driven commands, dumb_pull.py had no coverage beyond
test_smoke.py's import check before this file -- these pin its current
behavior before anyone touches it again.
"""

from __future__ import annotations

import pytest

import grokmirror
from grokmirror import dumb_pull

from support import DECOY_URL, GrokTree, git, write_script

# -- dumb_pull_repo() ---------------------------------------------------------


def test_dumb_pull_repo_with_no_remotes_returns_false(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    fullpath = tree.add_empty_repo('test/one.git')

    with caplog.at_level('INFO'):
        result = dumb_pull.dumb_pull_repo(str(fullpath), ['*'])

    assert result is False
    assert 'has no defined remotes' in caplog.text


def test_dumb_pull_repo_fetches_new_commits(tree: GrokTree) -> None:
    fullpath = tree.add_repo('test/one.git')
    source = tree.source()
    git('remote', 'add', 'origin', str(source.path), cwd=fullpath)
    # add_repo() already pushed the source's one commit straight into the bare
    # repo, so give the source a second commit that only fetching will bring in.
    newref = source.commit(message='A new commit')

    result = dumb_pull.dumb_pull_repo(str(fullpath), ['*'])

    assert result is True
    assert newref in git('rev-parse', '--all', cwd=fullpath)


def test_dumb_pull_repo_with_nothing_new_returns_false(tree: GrokTree) -> None:
    fullpath = tree.add_repo('test/one.git')
    source = tree.source()
    git('remote', 'add', 'origin', str(source.path), cwd=fullpath)
    # Bring the repo fully up to date first, so the real run has nothing to do.
    dumb_pull.dumb_pull_repo(str(fullpath), ['*'])

    result = dumb_pull.dumb_pull_repo(str(fullpath), ['*'])

    assert result is False


def test_dumb_pull_repo_skips_a_remote_that_does_not_match(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    fullpath = tree.add_repo('test/one.git')
    source = tree.source()
    git('remote', 'add', 'origin', str(source.path), cwd=fullpath)

    with caplog.at_level('INFO'):
        result = dumb_pull.dumb_pull_repo(str(fullpath), ['upstream'])

    assert result is False
    assert 'Could not find any remotes matching upstream' in caplog.text


def test_dumb_pull_repo_defers_a_locked_repo(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    fullpath = tree.add_repo('test/one.git')
    source = tree.source()
    git('remote', 'add', 'origin', str(source.path), cwd=fullpath)

    with grokmirror.locked_repo(str(fullpath)), caplog.at_level('INFO'):
        result = dumb_pull.dumb_pull_repo(str(fullpath), ['*'])

    assert result is False
    assert 'Could not obtain exclusive lock' in caplog.text


def test_dumb_pull_repo_svn_fetch_uses_remote_name_as_fetch_target(
    tree: GrokTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git-svn isn't installed in the test environment (and doesn't need to be):
    # the branch under test is purely the argument-building logic, which is
    # exercised by capturing what dumb_pull_repo asks run_git_command to run.
    fullpath = tree.add_empty_repo('test/one.git')
    calls: list[list[str]] = []

    def fake_run_git_command(_gitdir: str, args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        return 0, '', ''

    monkeypatch.setattr(dumb_pull.grokmirror, 'run_git_command', fake_run_git_command)

    dumb_pull.dumb_pull_repo(str(fullpath), ['origin', '*'], svn=True)

    assert ['svn', 'fetch', 'origin'] in calls
    # A bare '*' remote translates to a plain '--all', not 'svn fetch *'.
    assert ['svn', 'fetch', '--all'] in calls


# -- run_post_update_hook() ---------------------------------------------------


def test_run_post_update_hook_does_nothing_with_no_hook_configured() -> None:
    # Should not raise, and there is nothing else observable: an empty
    # hookscript is the documented "no hook wanted" spelling (see parse_args()'s
    # default of '').
    dumb_pull.run_post_update_hook('', '/some/repo.git')


def test_run_post_update_hook_warns_when_not_executable(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    hookscript = tree.root / 'hook.sh'
    write_script(hookscript, 'echo should not run\n', executable=False)

    with caplog.at_level('WARNING'):
        dumb_pull.run_post_update_hook(str(hookscript), '/some/repo.git')

    assert 'is not executable' in caplog.text


def test_run_post_update_hook_runs_and_logs_output(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    hookscript = tree.root / 'hook.sh'
    write_script(hookscript, 'echo "out: $1"\necho "err: $1" >&2\n')

    with caplog.at_level('DEBUG'):
        dumb_pull.run_post_update_hook(str(hookscript), '/some/repo.git')

    assert 'out: /some/repo.git' in caplog.text
    assert 'err: /some/repo.git' in caplog.text


# -- dumb_pull() ---------------------------------------------------------


def test_dumb_pull_skips_a_nonexistent_git_path(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    missing = str(tree.toplevel / 'does-not-exist.git')

    with caplog.at_level('CRITICAL'):
        dumb_pull.dumb_pull([missing])

    assert f'{missing} does not exist' in caplog.text


def test_dumb_pull_walks_a_directory_for_git_repos(tree: GrokTree) -> None:
    fullpath = tree.add_repo('nested/one.git')
    source = tree.source()
    git('remote', 'add', 'origin', str(source.path), cwd=fullpath)
    newref = source.commit(message='Found via directory walk')

    dumb_pull.dumb_pull([str(tree.toplevel)])

    assert newref in git('rev-parse', '--all', cwd=fullpath)


def test_dumb_pull_runs_post_update_hook_only_when_something_changed(tree: GrokTree) -> None:
    # Two independent sources: sharing one source would mean a commit to it is
    # visible to both remotes, so "unchanged" could never actually stay put.
    changed_source = tree.source('changed_src')
    unchanged_source = tree.source('unchanged_src')
    changed = tree.add_repo('test/changed.git', source=changed_source)
    unchanged = tree.add_repo('test/unchanged.git', source=unchanged_source)
    git('remote', 'add', 'origin', str(changed_source.path), cwd=changed)
    git('remote', 'add', 'origin', str(unchanged_source.path), cwd=unchanged)
    # A first fetch always creates new refs/remotes/origin/* tracking refs, so
    # bring 'unchanged' fully up to date now, before it can look "changed"
    # merely for having never been fetched before.
    dumb_pull.dumb_pull_repo(str(unchanged), ['*'])
    changed_source.commit(message='Only fetched by the changed repo')

    hookscript = tree.root / 'hook.sh'
    write_script(hookscript, 'echo "$1" >> ' + str(tree.root / 'hook.log') + '\n')

    dumb_pull.dumb_pull([str(changed), str(unchanged)], posthook=str(hookscript))

    hooklog = (tree.root / 'hook.log').read_text().splitlines()
    assert hooklog == [str(changed)]


def test_dumb_pull_never_touches_the_current_directory(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    # Same class of bug the manifest/pull cwd-fallback fixes guarded against:
    # an entry with no remotes must not fall through into a git invocation with
    # no repository path, which would silently answer from the decoy cwd.
    empty = tree.add_empty_repo('test/empty.git')

    with caplog.at_level('DEBUG'):
        dumb_pull.dumb_pull([str(empty)])

    assert DECOY_URL not in caplog.text
    assert git('for-each-ref', '--format=%(refname)', cwd=tree.decoy).split() == ['refs/heads/decoybranch']
