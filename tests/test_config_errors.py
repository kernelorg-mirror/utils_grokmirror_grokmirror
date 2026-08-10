# SPDX-License-Identifier: GPL-3.0-or-later
"""Bad or incomplete configuration files must produce errors, not tracebacks.

A mirror admin's first run is usually against a half-written config, and these
commands run from cron, so "tell them what is missing and exit non-zero" is the
only useful behaviour. Every case here used to end in a traceback, sometimes
from inside a worker process where the real cause was well hidden.
"""

from __future__ import annotations

import pytest

from support import GrokTree

# A [remote] section complete enough that only the setting under test is wrong.
GOOD_REMOTE = {'site': 'file:///nonexistent', 'manifest': 'file:///nonexistent/manifest.js.gz'}


def test_missing_toplevel(tree: GrokTree) -> None:
    # os.path.expanduser(None) -> TypeError before this was checked.
    tree.write_config({'core': {'toplevel': ''}})
    res = tree.run('grok-manifest', '--cfgfile', str(tree.cfgfile), expect=1)
    assert 'must define "toplevel"' in res.stderr


def test_missing_core_section(tree: GrokTree) -> None:
    tree.cfgfile.write_text('[pull]\nrefresh = 300\n')
    res = tree.run('grok-pull', '-c', str(tree.cfgfile), expect=1)
    assert 'Section [core] must exist' in res.stderr


def test_nonexistent_config_file(tree: GrokTree) -> None:
    missing = tree.root / 'nope.conf'
    res = tree.run('grok-pull', '-c', str(missing), expect=1)
    assert str(missing) in res.stderr


def test_pull_without_remote_section(tree: GrokTree) -> None:
    tree.write_config()
    res = tree.run_pull('-v', expect=1)
    assert 'Section [remote] must exist' in res.stdout + res.stderr


def test_pull_without_remote_site(tree: GrokTree) -> None:
    # This one reached os.path.join(None, gitdir) inside a pull worker.
    tree.write_config({'remote': {'manifest': GOOD_REMOTE['manifest']}})
    res = tree.run_pull('-v', expect=1)
    assert 'must define "site"' in res.stdout + res.stderr


def test_pull_without_any_manifest_setting(tree: GrokTree) -> None:
    # And this one reached None.find('file:///') in the manifest worker.
    tree.write_config({'remote': {'site': GOOD_REMOTE['site']}})
    res = tree.run_pull('-v', expect=1)
    assert 'must define "manifest" or "manifest_command"' in res.stdout + res.stderr


@pytest.mark.parametrize('setting', ['manifest', 'manifest_command'])
def test_pull_accepts_either_manifest_setting(tree: GrokTree, setting: str) -> None:
    # The [remote] check must not reject a config that only has
    # manifest_command, which is what mirrors behind a custom fetcher use. Both
    # of these then fail to actually get a manifest, which is the next test.
    value = GOOD_REMOTE['manifest'] if setting == 'manifest' else '/bin/false'
    tree.write_config({'remote': {'site': GOOD_REMOTE['site'], setting: value}})
    res = tree.run_pull('-v', expect=1)
    assert 'must define' not in res.stdout + res.stderr


def test_pull_reports_a_missing_remote_manifest(tree: GrokTree) -> None:
    # The message was already there, but the OSError behind it escaped all the
    # way out and buried it under a traceback.
    tree.write_config({'remote': GOOD_REMOTE})
    res = tree.run_pull('-v', expect=1)
    assert 'Remote manifest not found' in res.stdout + res.stderr


def test_pull_reports_a_failing_manifest_command(tree: GrokTree) -> None:
    tree.write_config({'remote': {'site': GOOD_REMOTE['site'], 'manifest_command': '/bin/false'}})
    res = tree.run_pull('-v', expect=1)
    assert 'failed with exit code 1' in res.stdout + res.stderr


def test_pull_reports_an_unexecutable_manifest_command(tree: GrokTree) -> None:
    script = tree.root / 'not-executable.sh'
    script.write_text('#!/bin/sh\necho "{}"\n')
    tree.write_config({'remote': {'site': GOOD_REMOTE['site'], 'manifest_command': str(script)}})
    res = tree.run_pull('-v', expect=1)
    assert 'not executable' in res.stdout + res.stderr


def test_pull_reports_unparseable_manifest_command_output(tree: GrokTree) -> None:
    script = tree.root / 'garbage.sh'
    script.write_text('#!/bin/sh\necho "not json at all"\n')
    script.chmod(0o755)
    tree.write_config({'remote': {'site': GOOD_REMOTE['site'], 'manifest_command': str(script)}})
    res = tree.run_pull('-v', expect=1)
    assert 'Failed to parse output' in res.stdout + res.stderr


@pytest.mark.slow
def test_pull_without_pull_section(origin: GrokTree, tree: GrokTree) -> None:
    # Everything in [pull] has a default, so a config without the section is
    # legitimate; it used to raise KeyError('pull') before doing any work.
    origin.add_repo('test/one.git')
    origin.run_manifest()
    tree.write_config(
        {'remote': {'site': f'file://{origin.toplevel}', 'manifest': f'file://{origin.manifest}'}},
    )
    tree.run_pull('-v')

    assert tree.path('test/one.git').is_dir()


def test_fsck_requires_a_config(tree: GrokTree) -> None:
    res = tree.run('grok-fsck', expect=2)
    assert 'required' in res.stderr
