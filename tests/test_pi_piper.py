# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-pi-piper feeds new public-inbox messages to an arbitrary command.

It runs as a post_update_hook, so it is invoked once per updated repository
during a mirror run. That makes "exits promptly when there is nothing to do" a
hard requirement: it used to read from stdin instead, which hung the whole
mirror run behind it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from grokmirror.pi_piper import git_get_new_revs

from support import GrokTree, git, pi_message


def write_piper_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding='utf-8')
    return path


@pytest.fixture
def inbox(tree: GrokTree) -> Path:
    """A public-inbox-shaped repository: <name>/git/<n>.git, with one message."""
    source = tree.source('mylist')
    source.commit(message='First message', content=pi_message('First message'), filename='m')
    return tree.add_repo('mylist/git/0.git', source=source)


def deliver(tree: GrokTree, inbox: Path, subject: str) -> None:
    """Add one more message to the list and push it to the mirrored inbox."""
    source = tree.source('mylist')
    source.commit(message=subject, content=pi_message(subject), filename='m')
    source.push(inbox)


@pytest.mark.parametrize(
    'config',
    [
        pytest.param('[DEFAULT]\n', id='no-pipe-at-all'),
        pytest.param('[DEFAULT]\npipe =\n', id='empty-pipe'),
        # grokmirror's own example config uses the literal string None to mean
        # "no piping", and pi-piper documents it that way.
        pytest.param('[DEFAULT]\npipe = None\n', id='pipe-is-None'),
    ],
)
def test_exits_promptly_without_a_pipe(tree: GrokTree, inbox: Path, config: str) -> None:
    # shlex.shlex(None) reads from *stdin*, so with no pipe configured the hook
    # blocked forever instead of exiting. run() has a timeout, so a return of
    # this bug fails the test rather than hanging the suite.
    cfgfile = write_piper_config(tree.root / 'pi-piper.conf', config)
    tree.run('grok-pi-piper', '-c', str(cfgfile), str(inbox), timeout=30)


def test_pipes_new_messages(tree: GrokTree, inbox: Path) -> None:
    # The pipe command gets each new message on stdin; collect them in a file so
    # the test can prove the hook actually ran.
    outfile = tree.root / 'piped.txt'
    script = tree.root / 'pipe.sh'
    script.write_text(f'#!/bin/sh\ncat >> {outfile}\necho "---" >> {outfile}\n')
    script.chmod(0o755)
    cfgfile = write_piper_config(tree.root / 'pi-piper.conf', f'[DEFAULT]\npipe = {script}\n')

    # First run only records where we are, since piping the entire history of a
    # list nobody has read yet is not what anyone wants.
    tree.run('grok-pi-piper', '-c', str(cfgfile), str(inbox))
    assert (inbox / 'pi-piper.latest').exists()
    assert not outfile.exists()

    # Now a new message arrives and gets piped.
    deliver(tree, inbox, 'Second message')
    tree.run('grok-pi-piper', '-c', str(cfgfile), str(inbox))

    piped = outfile.read_text()
    assert 'Subject: Second message' in piped
    assert piped.endswith('---\n')
    # Only the new message, not the whole history.
    assert 'First message' not in piped


def test_exotic_subject_characters_do_not_split_a_record(tree: GrokTree, inbox: Path) -> None:
    # These subjects are email Subject: headers, so they can hold anything a
    # mail client put there -- including \v, \f and U+0085, all of which
    # str.splitlines() treats as line breaks and git escapes in none of its
    # newline-delimited output. Records are NUL-terminated instead, so the
    # subject stays in one piece no matter what is in it.
    (inbox / 'pi-piper.latest').write_text(git('rev-parse', 'master', cwd=inbox).strip())
    subject = 'weird \v subject \x85 here \f end'
    deliver(tree, inbox, subject)

    revs = git_get_new_revs(str(inbox))

    assert revs == [(git('rev-parse', 'master', cwd=inbox).strip(), subject)]


def test_dry_run_changes_nothing(tree: GrokTree, inbox: Path) -> None:
    script = tree.root / 'pipe.sh'
    script.write_text('#!/bin/sh\ncat > /dev/null\n')
    script.chmod(0o755)
    cfgfile = write_piper_config(tree.root / 'pi-piper.conf', f'[DEFAULT]\npipe = {script}\n')

    tree.run('grok-pi-piper', '-c', str(cfgfile), '-d', str(inbox))

    assert not (inbox / 'pi-piper.latest').exists()


def test_unexecutable_pipe_is_reported(tree: GrokTree, inbox: Path) -> None:
    # The check used os.EX_OK, which is 0 -- the same value as os.F_OK -- so it
    # only tested that the file exists. A pipe command without the execute bit
    # got as far as being run, and failed with a confusing error instead.
    script = tree.root / 'pipe.sh'
    script.write_text('#!/bin/sh\ntrue\n')
    script.chmod(0o644)
    cfgfile = write_piper_config(tree.root / 'pi-piper.conf', f'[DEFAULT]\npipe = {script}\n')

    res = tree.run('grok-pi-piper', '-c', str(cfgfile), str(inbox), expect=1)
    assert 'Cannot execute' in res.stdout + res.stderr


def test_missing_pipe_command_is_reported(tree: GrokTree, inbox: Path) -> None:
    cfgfile = write_piper_config(tree.root / 'pi-piper.conf', f'[DEFAULT]\npipe = {tree.root}/no-such-command\n')

    res = tree.run('grok-pi-piper', '-c', str(cfgfile), str(inbox), expect=1)
    assert 'Cannot execute' in res.stdout + res.stderr


def test_per_list_section_wins_over_default(tree: GrokTree, inbox: Path) -> None:
    # Sections are matched against */<name>/git/*.git, which is how one config
    # file can pipe different lists to different commands.
    outfile = tree.root / 'piped.txt'
    script = tree.root / 'pipe.sh'
    script.write_text(f'#!/bin/sh\ncat >> {outfile}\n')
    script.chmod(0o755)
    cfgfile = write_piper_config(
        tree.root / 'pi-piper.conf',
        f'[DEFAULT]\npipe = None\n\n[mylist]\npipe = {script}\n',
    )

    tree.run('grok-pi-piper', '-c', str(cfgfile), str(inbox))
    assert (inbox / 'pi-piper.latest').exists()

    deliver(tree, inbox, 'Second message')
    tree.run('grok-pi-piper', '-c', str(cfgfile), str(inbox))

    assert 'Subject: Second message' in outfile.read_text()


def test_repo_with_no_master_is_skipped(tree: GrokTree) -> None:
    # A public-inbox repository that has just been created has no master yet.
    inbox = tree.add_empty_repo('mylist/git/0.git')
    script = tree.root / 'pipe.sh'
    script.write_text('#!/bin/sh\ncat > /dev/null\n')
    script.chmod(0o755)
    cfgfile = write_piper_config(tree.root / 'pi-piper.conf', f'[DEFAULT]\npipe = {script}\n')

    res = tree.run('grok-pi-piper', '-c', str(cfgfile), '-v', str(inbox))

    assert not (inbox / 'pi-piper.latest').exists()
    assert 'Could not list revs' in res.stdout + res.stderr
