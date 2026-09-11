# SPDX-License-Identifier: GPL-3.0-or-later
"""--config-check has to be usable by a person and by a program.

The checks themselves are covered in test_configcheck.py. What matters here
is the part a user touches: that the flag reports instead of running, that
each command opines only on the sections it reads, that the exit code says
whether anything is actually broken, and that --json puts nothing on stdout
but the object -- because something upstream is going to parse it.
"""

from __future__ import annotations

import json
from typing import Any

from support import GrokTree

# Complete enough that grok-pull has no [remote] complaints of its own; the
# URLs are never fetched, since every check here runs --no-network.
GOOD_REMOTE = {'site': 'file:///nonexistent', 'manifest': 'file:///nonexistent/manifest.js.gz'}


def report(tree: GrokTree, *args: str, expect: int = 0) -> str:
    res = tree.run_pull('--config-check', '--no-network', *args, expect=expect)
    return res.stdout


def envelope(tree: GrokTree, *args: str, expect: int = 0) -> dict[str, Any]:
    res = tree.run_pull('--config-check', '--no-network', '--json', *args, expect=expect)
    # Nothing but the object: anything else on stdout and the UI gets a
    # parse error instead of a report.
    return json.loads(res.stdout)


def test_a_config_with_nothing_wrong_passes(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE})
    assert 'nothing to report' in report(tree)


def test_a_broken_config_names_the_option_and_exits_nonzero(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE, 'pull': {'pull_threads': 'many'}})
    out = report(tree, expect=1)
    assert 'pull_threads' in out
    assert '1 error' in out


def test_a_warning_on_its_own_is_not_a_failure(tree: GrokTree) -> None:
    # Warnings are the checks that guess, and a guess is no reason to fail
    # somebody's cron job.
    tree.write_config({'remote': GOOD_REMOTE, 'pull': {'pull_thrads': '4'}})
    out = report(tree, expect=0)
    assert 'warning' in out
    assert '0 errors, 1 warning' in out


def test_the_report_says_which_user_it_answered_for(tree: GrokTree) -> None:
    # os.access() answers for whoever is asking, and grok-pull normally runs
    # as a service user. Without this line "writable" means nothing.
    tree.write_config({'remote': GOOD_REMOTE})
    assert report(tree).rstrip().endswith('was answered for that user.')


def test_the_report_says_when_it_did_not_go_online(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE})
    assert '--no-network' in report(tree)


def test_checking_does_not_mirror_anything(tree: GrokTree) -> None:
    # The whole point is that it is safe to run against a live mirror, and
    # safe to run before pointing grokmirror at a directory at all.
    tree.write_config({'remote': GOOD_REMOTE})
    tree.manifest.unlink(missing_ok=True)
    report(tree)
    assert not tree.manifest.exists()
    assert not list(tree.toplevel.glob('**/*.git'))


def test_a_config_that_will_not_parse_is_reported_not_raised(tree: GrokTree) -> None:
    tree.cfgfile.write_text('[core]\ntoplevel = /tmp\nthis line has no equals sign\n', encoding='utf-8')
    assert 'cannot be parsed, line 3' in report(tree, expect=1)


def test_a_config_file_that_is_not_there_is_reported(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE})
    tree.cfgfile.unlink()
    assert 'does not exist' in report(tree, expect=1)


# -- each command answers for the sections it reads --------------------------


def test_grok_pull_says_nothing_about_fsck(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE, 'fsck': {'frequency': 'often'}})
    assert 'frequency' not in report(tree)


def test_grok_fsck_says_nothing_about_pull(tree: GrokTree) -> None:
    tree.write_config({'pull': {'pull_threads': 'many'}})
    assert 'pull_threads' not in tree.run_fsck('--config-check', '--no-network').stdout


def test_grok_fsck_checks_its_own_section(tree: GrokTree) -> None:
    tree.write_config({'fsck': {'frequency': 'often'}})
    assert 'frequency' in tree.run_fsck('--config-check', '--no-network', expect=1).stdout


def test_grok_fsck_does_not_need_a_remote_section(tree: GrokTree) -> None:
    # Only grok-pull cannot run without one.
    tree.write_config()
    assert 'nothing to report' in tree.run_fsck('--config-check', '--no-network').stdout


def test_grok_manifest_checks_its_own_section(tree: GrokTree) -> None:
    tree.write_config({'manifest': {'pretty': 'sure'}})
    res = tree.run('grok-manifest', '--cfgfile', str(tree.cfgfile), '--config-check', '--no-network', expect=1)
    assert 'pretty' in res.stdout


def test_grok_manifest_wants_to_be_told_which_file_to_check(tree: GrokTree) -> None:
    res = tree.run('grok-manifest', '--config-check', expect=2)
    assert '--cfgfile' in res.stderr


def test_grok_manifest_reports_a_broken_config_rather_than_failing_to_load_it(tree: GrokTree) -> None:
    # parse_args() normally loads the config to fill in the defaults, which
    # raises on exactly the files worth checking.
    tree.write_config({'core': {'toplevel': ''}})
    res = tree.run('grok-manifest', '--cfgfile', str(tree.cfgfile), '--config-check', '--no-network', expect=1)
    assert 'toplevel' in res.stdout


# -- the JSON envelope, which is a compatibility surface ---------------------


def test_the_envelope_has_every_key_the_ui_expects(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE})
    payload = envelope(tree)
    assert set(payload) == {'config', 'checked_as', 'online', 'ok', 'diagnostics', 'summary'}
    assert set(payload['checked_as']) == {'uid', 'user'}
    assert set(payload['summary']) == {'errors', 'warnings'}
    assert payload == {**payload, 'ok': True, 'online': False, 'config': str(tree.cfgfile)}
    assert payload['diagnostics'] == []


def test_a_diagnostic_carries_its_own_five_fields(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE, 'pull': {'purge': 'maybe'}})
    diagnostics = envelope(tree, expect=1)['diagnostics']
    assert diagnostics[0]['severity'] == 'error'
    assert diagnostics[0]['section'] == 'pull'
    assert diagnostics[0]['option'] == 'purge'
    assert 'must be a boolean' in diagnostics[0]['message']
    # Present and null rather than absent, so a consumer can index it.
    assert 'yes/no' in diagnostics[0]['hint']


def test_a_hintless_diagnostic_says_null_rather_than_leaving_it_out(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE, 'pull': {'pull_threads': 'many'}})
    assert envelope(tree, expect=1)['diagnostics'][0]['hint'] is None


def test_warnings_are_counted_but_leave_ok_true(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE, 'pull': {'pull_thrads': '4'}})
    payload = envelope(tree)
    assert payload['ok'] is True
    assert payload['summary'] == {'errors': 0, 'warnings': 1}


# -- the flags that only mean something together -----------------------------


def test_json_without_config_check_is_refused(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE})
    res = tree.run_pull('--json', expect=2)
    assert '--json only means something together with --config-check' in res.stderr


def test_no_network_without_config_check_is_refused(tree: GrokTree) -> None:
    tree.write_config({'remote': GOOD_REMOTE})
    res = tree.run_pull('--no-network', expect=2)
    assert '--no-network only means something together with --config-check' in res.stderr


def test_the_same_refusal_applies_to_the_other_commands(tree: GrokTree) -> None:
    tree.write_config()
    assert '--config-check' in tree.run_fsck('--json', expect=2).stderr
    res = tree.run('grok-manifest', '-m', str(tree.manifest), '-t', str(tree.toplevel), '--json', expect=2)
    assert '--config-check' in res.stderr
