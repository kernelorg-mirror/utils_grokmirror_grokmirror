# SPDX-License-Identifier: GPL-3.0-or-later
"""grok-pull's post-clone/post-work/post-update hook scripts.

get_hookscripts() turns a [pull] config value into argv lists, skipping
anything that is not executable; the three run_post_*_hook() wrappers each
run those argv lists through a real subprocess and log whatever comes back.
None of this had direct test coverage before this file -- the identically
named hooks in dumb_pull.py are a different, unrelated set of functions.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree

# -- get_hookscripts() ---------------------------------------------------------


def test_get_hookscripts_is_empty_when_unconfigured(tree: GrokTree) -> None:
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {}})))

    assert grokmirror.pull.get_hookscripts(config, 'post_clone_complete_hook') == []


def test_get_hookscripts_skips_a_non_executable_script_and_warns(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    hookscript = tree.root / 'hook.sh'
    hookscript.write_text('#!/bin/sh\necho should not run\n')
    hookscript.chmod(0o644)
    config = grokmirror.load_config_file(
        str(tree.write_config(sections={'pull': {'post_clone_complete_hook': str(hookscript)}}))
    )

    with caplog.at_level('WARNING'):
        hookscripts = grokmirror.pull.get_hookscripts(config, 'post_clone_complete_hook')

    assert hookscripts == []
    assert 'hook not executable' in caplog.text


def test_get_hookscripts_returns_one_argv_per_configured_line(tree: GrokTree) -> None:
    first = tree.root / 'first.sh'
    second = tree.root / 'second.sh'
    for hookscript in (first, second):
        hookscript.write_text('#!/bin/sh\ntrue\n')
        hookscript.chmod(hookscript.stat().st_mode | stat.S_IEXEC)
    hookline = f'{first} --one\n{second} --two extra'
    config = grokmirror.load_config_file(
        str(tree.write_config(sections={'pull': {'post_clone_complete_hook': hookline}}))
    )

    hookscripts = grokmirror.pull.get_hookscripts(config, 'post_clone_complete_hook')

    assert hookscripts == [[str(first), '--one'], [str(second), '--two', 'extra']]


def test_get_hookscripts_expands_a_leading_tilde(tree: GrokTree) -> None:
    # isolated_env (autouse) has already pointed $HOME at a real, writable
    # directory for the test's git config -- reuse it rather than pointing
    # $HOME somewhere else, which would just have to be undone afterwards.
    home = Path(os.environ['HOME'])
    hookscript = home / 'hook.sh'
    hookscript.write_text('#!/bin/sh\ntrue\n')
    hookscript.chmod(hookscript.stat().st_mode | stat.S_IEXEC)
    config = grokmirror.load_config_file(
        str(tree.write_config(sections={'pull': {'post_clone_complete_hook': '~/hook.sh'}}))
    )

    hookscripts = grokmirror.pull.get_hookscripts(config, 'post_clone_complete_hook')

    assert hookscripts == [[str(hookscript)]]


# -- run_post_clone_complete_hook() --------------------------------------------


def test_run_post_clone_complete_hook_does_nothing_when_unconfigured(tree: GrokTree) -> None:
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {}})))

    grokmirror.pull.run_post_clone_complete_hook(config, ['/test/one.git', '/test/two.git'])


def test_run_post_clone_complete_hook_pipes_the_clone_list_as_stdin(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    hookscript = tree.root / 'hook.sh'
    hookscript.write_text('#!/bin/sh\ncat\n')
    hookscript.chmod(hookscript.stat().st_mode | stat.S_IEXEC)
    config = grokmirror.load_config_file(
        str(tree.write_config(sections={'pull': {'post_clone_complete_hook': str(hookscript)}}))
    )

    with caplog.at_level('INFO'):
        grokmirror.pull.run_post_clone_complete_hook(config, ['/test/one.git', '/test/two.git'])

    assert 'Hook Stdout: /test/one.git\n/test/two.git' in caplog.text


# -- run_post_work_complete_hook() ---------------------------------------------


def test_run_post_work_complete_hook_does_nothing_when_unconfigured(tree: GrokTree) -> None:
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {}})))

    grokmirror.pull.run_post_work_complete_hook(config)


def test_run_post_work_complete_hook_runs_and_logs_output(tree: GrokTree, caplog: pytest.LogCaptureFixture) -> None:
    hookscript = tree.root / 'hook.sh'
    hookscript.write_text('#!/bin/sh\necho all done\necho went wrong >&2\n')
    hookscript.chmod(hookscript.stat().st_mode | stat.S_IEXEC)
    config = grokmirror.load_config_file(
        str(tree.write_config(sections={'pull': {'post_work_complete_hook': str(hookscript)}}))
    )

    with caplog.at_level('INFO'):
        grokmirror.pull.run_post_work_complete_hook(config)

    assert 'Hook Stdout: all done' in caplog.text
    assert 'Hook Stderr: went wrong' in caplog.text


# -- run_post_update_hook() -----------------------------------------------------


def test_run_post_update_hook_does_nothing_when_unconfigured(tree: GrokTree) -> None:
    config = grokmirror.load_config_file(str(tree.write_config(sections={'pull': {}})))

    grokmirror.pull.run_post_update_hook(config, '/test/one.git')


def test_run_post_update_hook_appends_the_fullpath_and_logs_output(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    hookscript = tree.root / 'hook.sh'
    hookscript.write_text('#!/bin/sh\necho "updated: $1"\necho "trouble: $1" >&2\n')
    hookscript.chmod(hookscript.stat().st_mode | stat.S_IEXEC)
    config = grokmirror.load_config_file(
        str(tree.write_config(sections={'pull': {'post_update_hook': str(hookscript)}}))
    )

    with caplog.at_level('INFO'):
        grokmirror.pull.run_post_update_hook(config, '/test/one.git')

    assert 'Hook Stdout (/test/one.git): updated: /test/one.git' in caplog.text
    assert 'Hook Stderr (/test/one.git): trouble: /test/one.git' in caplog.text
