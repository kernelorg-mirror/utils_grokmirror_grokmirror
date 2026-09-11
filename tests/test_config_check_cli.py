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
import subprocess
from pathlib import Path
from typing import Any

from support import GrokTree

# Complete enough that grok-pull has no [remote] complaints of its own. The
# manifest URL is never fetched, since every check here runs --no-network,
# but "site" is a git URL and a local one has to be a directory that is
# really there, so it is filled in per-tree by good_remote().
GOOD_MANIFEST = 'file:///nonexistent/manifest.js.gz'


def good_remote(tree: GrokTree) -> dict[str, str]:
    return {'site': f'file://{tree.toplevel}', 'manifest': GOOD_MANIFEST}


def report(tree: GrokTree, *args: str, expect: int = 0) -> str:
    res = tree.run_pull('--config-check', '--no-network', *args, expect=expect)
    return res.stdout


def envelope(tree: GrokTree, *args: str, expect: int = 0) -> dict[str, Any]:
    res = tree.run_pull('--config-check', '--no-network', '--json', *args, expect=expect)
    # Nothing but the object: anything else on stdout and the UI gets a
    # parse error instead of a report.
    return json.loads(res.stdout)


def test_a_config_with_nothing_wrong_passes(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree)})
    assert 'nothing to report' in report(tree)


def test_a_broken_config_names_the_option_and_exits_nonzero(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree), 'pull': {'pull_threads': 'many'}})
    out = report(tree, expect=1)
    assert 'pull_threads' in out
    assert '1 error' in out


def test_a_warning_on_its_own_is_not_a_failure(tree: GrokTree) -> None:
    # Warnings are the checks that guess, and a guess is no reason to fail
    # somebody's cron job.
    tree.write_config({'remote': good_remote(tree), 'pull': {'pull_thrads': '4'}})
    out = report(tree, expect=0)
    assert 'warning' in out
    assert '0 errors, 1 warning' in out


def test_the_report_says_which_user_it_answered_for(tree: GrokTree) -> None:
    # os.access() answers for whoever is asking, and grok-pull normally runs
    # as a service user. Without this line "writable" means nothing.
    tree.write_config({'remote': good_remote(tree)})
    assert report(tree).rstrip().endswith('was answered for that user.')


def test_the_report_says_when_it_did_not_go_online(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree)})
    assert '--no-network' in report(tree)


def test_checking_does_not_mirror_anything(tree: GrokTree) -> None:
    # The whole point is that it is safe to run against a live mirror, and
    # safe to run before pointing grokmirror at a directory at all.
    tree.write_config({'remote': good_remote(tree)})
    tree.manifest.unlink(missing_ok=True)
    report(tree)
    assert not tree.manifest.exists()
    assert not list(tree.toplevel.glob('**/*.git'))


def test_a_config_that_will_not_parse_is_reported_not_raised(tree: GrokTree) -> None:
    tree.cfgfile.write_text('[core]\ntoplevel = /tmp\nthis line has no equals sign\n', encoding='utf-8')
    assert 'cannot be parsed, line 3' in report(tree, expect=1)


def test_a_config_file_that_is_not_there_is_reported(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree)})
    tree.cfgfile.unlink()
    assert 'does not exist' in report(tree, expect=1)


# -- each command answers for the sections it reads --------------------------


def test_grok_pull_says_nothing_about_fsck(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree), 'fsck': {'frequency': 'often'}})
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
    tree.write_config({'remote': good_remote(tree)})
    payload = envelope(tree)
    assert set(payload) == {'config', 'checked_as', 'online', 'ok', 'diagnostics', 'summary'}
    assert set(payload['checked_as']) == {'uid', 'user'}
    assert set(payload['summary']) == {'errors', 'warnings'}
    assert payload == {**payload, 'ok': True, 'online': False, 'config': str(tree.cfgfile)}
    assert payload['diagnostics'] == []


def test_a_diagnostic_carries_its_own_five_fields(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree), 'pull': {'purge': 'maybe'}})
    diagnostics = envelope(tree, expect=1)['diagnostics']
    assert diagnostics[0]['severity'] == 'error'
    assert diagnostics[0]['section'] == 'pull'
    assert diagnostics[0]['option'] == 'purge'
    assert 'must be a boolean' in diagnostics[0]['message']
    # Present and null rather than absent, so a consumer can index it.
    assert 'yes/no' in diagnostics[0]['hint']


def test_a_hintless_diagnostic_says_null_rather_than_leaving_it_out(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree), 'pull': {'pull_threads': 'many'}})
    assert envelope(tree, expect=1)['diagnostics'][0]['hint'] is None


def test_warnings_are_counted_but_leave_ok_true(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree), 'pull': {'pull_thrads': '4'}})
    payload = envelope(tree)
    assert payload['ok'] is True
    assert payload['summary'] == {'errors': 0, 'warnings': 1}


# -- the flags that only mean something together -----------------------------


def test_json_without_config_check_is_refused(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree)})
    res = tree.run_pull('--json', expect=2)
    assert '--json only means something together with --config-check' in res.stderr


def test_no_network_without_config_check_is_refused(tree: GrokTree) -> None:
    tree.write_config({'remote': good_remote(tree)})
    res = tree.run_pull('--no-network', expect=2)
    assert '--no-network only means something together with --config-check' in res.stderr


def test_the_same_refusal_applies_to_the_other_commands(tree: GrokTree) -> None:
    tree.write_config()
    assert '--config-check' in tree.run_fsck('--json', expect=2).stderr
    res = tree.run('grok-manifest', '-m', str(tree.manifest), '-t', str(tree.toplevel), '--json', expect=2)
    assert '--config-check' in res.stderr


# -- the sample config, and the promise that nothing is written --------------


REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CONF = REPO_ROOT / 'grokmirror.conf'


def sample_config(tree: GrokTree) -> Path:
    """The shipped grokmirror.conf, pointed at a toplevel that exists.

    Everything else in the file interpolates from ${toplevel}, so moving
    that one line is enough to make the sample checkable here.
    """
    text = SAMPLE_CONF.read_text(encoding='utf-8')
    moved = text.replace('toplevel = /var/lib/git/mirror', f'toplevel = {tree.toplevel}', 1)
    assert moved != text, 'grokmirror.conf no longer sets toplevel the way this test expects'
    cfgfile = tree.root / 'sample.conf'
    cfgfile.write_text(moved, encoding='utf-8')
    return cfgfile


def check_all(tree: GrokTree, cfgfile: Path) -> list[subprocess.CompletedProcess[str]]:
    """Check one config file with all three commands that can check one.

    The run_* helpers write this tree's own config file when it is missing,
    which is one write too many for a test about writing nothing, so these
    go through run() directly.
    """
    return [
        tree.run('grok-pull', '--config-check', '--no-network', '-c', str(cfgfile)),
        tree.run('grok-fsck', '--config-check', '--no-network', '-c', str(cfgfile)),
        tree.run('grok-manifest', '--config-check', '--no-network', '--cfgfile', str(cfgfile)),
    ]


def test_the_shipped_sample_config_passes_its_own_check(tree: GrokTree) -> None:
    # The sample config is what everybody starts from, so a check that
    # complains about it is either wrong or the sample is. Either way it is
    # our bug and not the reader's.
    cfgfile = sample_config(tree)
    for res in check_all(tree, cfgfile):
        assert 'nothing to report' in res.stdout


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under root, with its size and mtime in nanoseconds."""
    found = {}
    for path in sorted(root.rglob('*')):
        st = path.lstat()
        found[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return found


def test_checking_writes_nothing_at_all(tree: GrokTree) -> None:
    # "It only reads" is the promise that makes it safe to run this against
    # a live mirror, so pin it by comparing the whole tree rather than by
    # trusting that no writing code got called.
    tree.add_repo('test/repo.git', 'one')
    tree.run_manifest('-m', str(tree.manifest), '-t', str(tree.toplevel))
    cfgfile = sample_config(tree)

    before = snapshot(tree.root)
    check_all(tree, cfgfile)
    assert snapshot(tree.root) == before
