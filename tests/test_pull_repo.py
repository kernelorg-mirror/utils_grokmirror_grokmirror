# SPDX-License-Identifier: GPL-3.0-or-later
"""pull_repo(), the thin wrapper around `git remote update --prune`.

Only ever exercised indirectly before, through pull_worker() tests that cover
the surrounding bookkeeping but always fetch an empty-to-populated repo once
and never look at how pull_repo() itself classifies git's stderr. That
classification is the whole point of the function: real git puts its normal
"From ... / branch -> remote/branch" progress lines on stderr even on success,
and pull_repo() has to tell those apart from a genuine problem so a healthy
mirror run doesn't spam admins at warning level.
"""

from __future__ import annotations

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree, git


def wire_up(origin: GrokTree, tree: GrokTree, gitdir: str) -> tuple[str, str]:
    """Bare-init `gitdir` in `tree` and point its remote at `origin`."""
    origin.run_manifest()
    cfgfile = tree.write_mirror_config(origin)
    config = grokmirror.load_config_file(str(cfgfile))
    fullpath = str(tree.path(gitdir))
    assert grokmirror.setup_bare_repo(fullpath)
    assert grokmirror.pull.fix_remotes(str(tree.toplevel), gitdir, config['remote']['site'], config)
    remotename = config['pull'].get('remotename', '_grokmirror')
    return fullpath, remotename


def test_successful_fetch_with_new_commits_returns_true_and_logs_at_debug(
    origin: GrokTree, tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    origin.add_repo('test/one.git')
    fullpath, remotename = wire_up(origin, tree, '/test/one.git')

    with caplog.at_level('DEBUG'):
        result = grokmirror.pull.pull_repo(fullpath, remotename)

    assert result is True
    theirs = git('rev-parse', 'refs/heads/master', cwd=origin.path('test/one.git')).strip()
    ours = git('rev-parse', 'refs/heads/master', cwd=tree.path('test/one.git')).strip()
    assert ours == theirs
    # Real git's own "From ../origin" / "-> remotename/master" progress lines
    # landed on stderr, and were recognized as normal rather than a warning.
    assert 'Stderr' in caplog.text
    assert not any(record.levelname == 'WARNING' for record in caplog.records)
    assert any(record.levelname == 'DEBUG' and 'Stderr' in record.message for record in caplog.records)


def test_up_to_date_fetch_returns_true_with_no_stderr_logging(
    origin: GrokTree, tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    origin.add_repo('test/one.git')
    fullpath, remotename = wire_up(origin, tree, '/test/one.git')
    # First fetch brings us up to date; the interesting one is the second,
    # where git has nothing new to report at all.
    assert grokmirror.pull.pull_repo(fullpath, remotename) is True

    with caplog.at_level('DEBUG'):
        result = grokmirror.pull.pull_repo(fullpath, remotename)

    assert result is True
    # Nothing on stderr at all this time, so pull_repo()'s classification loop
    # never runs -- neither bucket should have fired.
    assert 'Stderr' not in caplog.text


def test_recognized_lines_are_bucketed_as_debug_even_when_the_fetch_fails(
    tree: GrokTree, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recognized-ness only ever changes where a line lands when the fetch
    # failed: on success every line ends up in debug regardless (the final
    # `else` branch catches whatever the prefix check didn't), so a real
    # failing fetch that also emits one of git's own recognized lines is the
    # only way to observe the prefix check actually doing something. Real git
    # doesn't offer an easy way to make that combination happen on demand, so
    # run_git_command is stubbed here the same way test_dumb_pull.py stubs it
    # to check pure argument/output handling -- this isn't standing in for the
    # network, just for a git stderr shape that would otherwise be
    # impractical to provoke reliably.
    fullpath = tree.add_empty_repo('test/one.git')
    error = 'From somewhere\nfatal: unrecognized failure\n'

    def fake_run_git_command(_gitdir: str, _args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        return 1, '', error

    monkeypatch.setattr(grokmirror.pull.grokmirror, 'run_git_command', fake_run_git_command)

    with caplog.at_level('DEBUG'):
        result = grokmirror.pull.pull_repo(str(fullpath), '_grokmirror')

    assert result is False
    debug_records = [r.message for r in caplog.records if r.levelname == 'DEBUG' and 'Stderr' in r.message]
    warn_records = [r.message for r in caplog.records if r.levelname == 'WARNING' and 'Stderr' in r.message]
    assert len(debug_records) == 1 and 'From somewhere' in debug_records[0]
    assert len(warn_records) == 1 and 'unrecognized failure' in warn_records[0]


def test_fetch_from_a_broken_remote_returns_false_and_logs_a_warning(
    origin: GrokTree, tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    origin.add_repo('test/one.git')
    fullpath, remotename = wire_up(origin, tree, '/test/one.git')
    # Break the remote after fix_remotes() has already pointed it somewhere
    # real, the way a origin repository disappearing out from under a mirror
    # would.
    git('remote', 'set-url', remotename, str(origin.toplevel / 'test/does-not-exist.git'), cwd=fullpath)

    with caplog.at_level('DEBUG'):
        result = grokmirror.pull.pull_repo(fullpath, remotename)

    assert result is False
    assert any(record.levelname == 'WARNING' and 'Stderr' in record.message for record in caplog.records)
    assert not any(record.levelname == 'DEBUG' and 'Stderr' in record.message for record in caplog.records)
