# SPDX-License-Identifier: GPL-3.0-or-later
"""The config checker has to find every problem, not just the first one.

Every command bails out on the first thing wrong with a config file, which
is the right thing to do when it is about to start mirroring, and exactly
the wrong thing for someone trying to get a hand-written config correct.
So these tests care about two things: that a problem is found at all, and
that the message names the option it is about -- an error that does not say
which line to go and look at is barely better than no error.
"""

from __future__ import annotations

import contextlib
import os
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from grokmirror.configcheck import Diagnostic, check_config, command_argv

ALL_SECTIONS = {'core', 'manifest', 'remote', 'pull', 'fsck'}


def check(tmp_path: Path, text: str, sections: set[str] | None = None, online: bool = False) -> list[Diagnostic]:
    """Write a config file and check it.

    The config is written as text rather than through ConfigParser, since
    several of the problems worth catching are ones ConfigParser would
    refuse to write in the first place.

    Offline by default: the test suite must never depend on a name server
    or on example.com answering. The reachability tests below opt back in
    with a file:// URL, which goes nowhere near the network.
    """
    cfgfile = tmp_path / 'grokmirror.conf'
    cfgfile.write_text(text, encoding='utf-8')
    return check_config(cfgfile, sections if sections is not None else ALL_SECTIONS, online=online)


def messages(diagnostics: list[Diagnostic], severity: str | None = None) -> list[str]:
    return [str(d) for d in diagnostics if severity is None or d.severity == severity]


def about(diagnostics: list[Diagnostic], section: str, option: str | None = None) -> list[Diagnostic]:
    return [d for d in diagnostics if d.section == section and (option is None or d.option == option)]


def section_level(diagnostics: list[Diagnostic], section: str) -> list[Diagnostic]:
    """Only the diagnostics about a section as a whole, not about an option."""
    return [d for d in diagnostics if d.section == section and d.option is None]


def good_config(tmp_path: Path) -> str:
    """A config with nothing wrong with it, for tests to spoil one line of."""
    return f"""
[core]
toplevel = {tmp_path}
manifest = ${{toplevel}}/manifest.js.gz
log = ${{toplevel}}/grokmirror.log
loglevel = info

[remote]
site = https://git.example.com
manifest = ${{site}}/manifest.js.gz

[pull]
projectslist = ${{core:toplevel}}/projects.list
purge = yes
pull_threads = 4
"""


def test_a_good_config_produces_nothing_at_all(tmp_path: Path) -> None:
    assert check(tmp_path, good_config(tmp_path)) == []


# -- problems with the file itself -------------------------------------------


def test_a_missing_file_is_reported_rather_than_raising(tmp_path: Path) -> None:
    diagnostics = check_config(tmp_path / 'nope.conf', ALL_SECTIONS)
    assert len(diagnostics) == 1
    assert 'does not exist' in diagnostics[0].message


def test_a_directory_is_not_a_config_file(tmp_path: Path) -> None:
    diagnostics = check_config(tmp_path, ALL_SECTIONS)
    assert 'is a directory' in diagnostics[0].message


@pytest.mark.skipif(os.geteuid() == 0, reason='root can read anything, so the check cannot fail')
def test_an_unreadable_file_says_which_user_could_not_read_it(tmp_path: Path) -> None:
    cfgfile = tmp_path / 'grokmirror.conf'
    cfgfile.write_text(good_config(tmp_path), encoding='utf-8')
    cfgfile.chmod(stat.S_IWUSR)

    diagnostics = check_config(cfgfile, ALL_SECTIONS)

    assert 'not readable' in diagnostics[0].message
    # Which user matters: the admin is usually not the user that runs the
    # mirror, so "not readable" alone sends them looking in the wrong place.
    assert str(os.geteuid()) in diagnostics[0].message


def test_a_duplicated_section_is_reported(tmp_path: Path) -> None:
    diagnostics = check(tmp_path, f'[core]\ntoplevel = {tmp_path}\n\n[core]\nloglevel = debug\n')
    assert 'more than once' in diagnostics[0].message


def test_a_duplicated_option_names_the_option(tmp_path: Path) -> None:
    diagnostics = check(tmp_path, f'[core]\ntoplevel = {tmp_path}\ntoplevel = /elsewhere\n')
    assert diagnostics[0].option == 'toplevel'
    assert 'more than once' in diagnostics[0].message


def test_an_option_before_any_section_is_reported(tmp_path: Path) -> None:
    diagnostics = check(tmp_path, 'toplevel = /var/lib/mirror\n[core]\n')
    assert 'outside any section' in diagnostics[0].message


def test_a_line_that_is_not_an_option_is_reported_with_its_number(tmp_path: Path) -> None:
    diagnostics = check(tmp_path, f'[core]\ntoplevel = {tmp_path}\nthis is not an option\n')
    assert 'line 3' in diagnostics[0].message


def test_nothing_else_is_checked_once_the_file_will_not_parse(tmp_path: Path) -> None:
    # A file ConfigParser cannot read tells us nothing about its options, so
    # piling guesses on top of the parse error would only be noise.
    diagnostics = check(tmp_path, '[core]\ntoplevel = /does/not/exist\nbroken line here\n')
    assert len(diagnostics) == 1


# -- the things load_config_file() refuses to run without ---------------------


def test_a_missing_core_section_is_an_error(tmp_path: Path) -> None:
    diagnostics = check(tmp_path, '[pull]\npurge = yes\n')
    assert [d for d in diagnostics if d.severity == 'error' and 'core' in d.message]


def test_a_missing_toplevel_is_an_error(tmp_path: Path) -> None:
    diagnostics = check(tmp_path, '[core]\nloglevel = debug\n')
    assert about(diagnostics, 'core', 'toplevel')


def test_a_toplevel_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    # Unlike every other directory in the config, this one is never created.
    diagnostics = about(check(tmp_path, f'[core]\ntoplevel = {tmp_path}/nowhere\n'), 'core', 'toplevel')
    assert diagnostics[0].severity == 'error'
    assert 'does not exist' in diagnostics[0].message


@pytest.mark.skipif(os.geteuid() == 0, reason='root can write anywhere, so the check cannot fail')
def test_an_unwritable_toplevel_says_which_user_cannot_write_to_it(tmp_path: Path) -> None:
    toplevel = tmp_path / 'toplevel'
    toplevel.mkdir()
    toplevel.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        diagnostics = about(check(tmp_path, f'[core]\ntoplevel = {toplevel}\n'), 'core', 'toplevel')
        assert 'not writable' in diagnostics[0].message
        assert str(os.geteuid()) in diagnostics[0].message
    finally:
        toplevel.chmod(stat.S_IRWXU)


def test_a_toplevel_that_is_a_file_is_an_error(tmp_path: Path) -> None:
    notadir = tmp_path / 'file'
    notadir.write_text('', encoding='utf-8')
    diagnostics = about(check(tmp_path, f'[core]\ntoplevel = {notadir}\n'), 'core', 'toplevel')
    assert 'not a directory' in diagnostics[0].message


# -- interpolation, which read() never checks --------------------------------


def test_an_interpolation_that_does_not_resolve_is_reported(tmp_path: Path) -> None:
    # ExtendedInterpolation is lazy, so read() is perfectly happy with this
    # and the command only finds out when it reads the option -- which for
    # several of them is well into a run.
    text = good_config(tmp_path).replace('${core:toplevel}/projects.list', '${core:toplvel}/projects.list')
    diagnostics = about(check(tmp_path, text), 'pull', 'projectslist')
    assert diagnostics[0].severity == 'error'
    assert 'toplvel' in str(diagnostics[0])


def test_an_unresolvable_reference_is_named_without_configparsers_wording(tmp_path: Path) -> None:
    # The diagnostic already says which option it is about, so the message
    # only has to carry the part ConfigParser alone knows.
    text = good_config(tmp_path).replace('${core:toplevel}/projects.list', '${core:toplvel}/projects.list')
    diagnostics = about(check(tmp_path, text), 'pull', 'projectslist')
    assert diagnostics[0].message == 'refers to ${core:toplvel}, which is not set'


def test_a_cross_section_interpolation_that_resolves_is_not_reported(tmp_path: Path) -> None:
    assert not about(check(tmp_path, good_config(tmp_path)), 'pull', 'projectslist')


def test_one_broken_interpolation_does_not_hide_the_rest_of_the_file(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('${core:toplevel}/projects.list', '${nosuch:thing}')
    text = text.replace('pull_threads = 4', 'pull_threads = four')
    diagnostics = check(tmp_path, text)
    assert about(diagnostics, 'pull', 'projectslist')
    assert about(diagnostics, 'pull', 'pull_threads')


# -- typos, which are the whole point ----------------------------------------


def test_an_unknown_section_is_a_warning_with_a_suggestion(tmp_path: Path) -> None:
    diagnostics = about(check(tmp_path, good_config(tmp_path) + '\n[fsckk]\nfrequency = 30\n'), 'fsckk')
    assert diagnostics[0].severity == 'warning'
    assert diagnostics[0].hint is not None
    assert 'fsck' in diagnostics[0].hint


def test_an_unknown_option_is_a_warning_with_a_suggestion(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('pull_threads = 4', 'pul_threads = 4')
    diagnostics = about(check(tmp_path, text), 'pull', 'pul_threads')
    assert diagnostics[0].severity == 'warning'
    assert diagnostics[0].hint is not None
    assert 'pull_threads' in diagnostics[0].hint


def test_a_real_section_this_command_does_not_read_is_left_alone(tmp_path: Path) -> None:
    # grok-pull has no business opining on [fsck]: not reading a section is
    # not the same as not recognising it.
    text = good_config(tmp_path) + '\n[fsck]\nfrequency = not-a-number\n'
    assert not about(check(tmp_path, text, sections={'core', 'remote', 'pull'}), 'fsck')


def test_default_section_values_are_not_mistaken_for_typos(tmp_path: Path) -> None:
    # [DEFAULT] is usually there to be interpolated into other values, and
    # its keys show up in every section. Warning about each one in each
    # section would make the feature unusable.
    text = f'[DEFAULT]\nbase = {tmp_path}\n\n[core]\ntoplevel = ${{base}}\n'
    # [core] alone, as grok-manifest reads it: a config with no [remote] is
    # only incomplete for the commands that pull.
    assert check(tmp_path, text, sections={'core'}) == []


# -- values of the wrong shape -----------------------------------------------


def test_an_int_that_is_not_a_number_is_an_error(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('pull_threads = 4', 'pull_threads = four')
    diagnostics = about(check(tmp_path, text), 'pull', 'pull_threads')
    assert diagnostics[0].severity == 'error'
    assert 'whole number' in diagnostics[0].message


def test_a_boolean_that_is_neither_is_an_error_listing_what_is_accepted(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('purge = yes', 'purge = please')
    diagnostics = about(check(tmp_path, text), 'pull', 'purge')
    assert diagnostics[0].severity == 'error'
    assert diagnostics[0].hint is not None
    assert 'yes/no' in diagnostics[0].hint


@pytest.mark.parametrize('value', ['yes', 'no', 'true', 'false', 'on', 'off', '1', '0'])
def test_every_boolean_spelling_configparser_takes_is_accepted(tmp_path: Path, value: str) -> None:
    text = good_config(tmp_path).replace('purge = yes', f'purge = {value}')
    assert not about(check(tmp_path, text), 'pull', 'purge')


def test_an_enum_lists_the_values_it_accepts(tmp_path: Path) -> None:
    text = f'[core]\ntoplevel = {tmp_path}\n\n[fsck]\nprecious = maybe\n'
    diagnostics = about(check(tmp_path, text), 'fsck', 'precious')
    assert diagnostics[0].severity == 'error'
    assert 'always' in diagnostics[0].message


def test_a_near_miss_enum_value_gets_a_suggestion(tmp_path: Path) -> None:
    text = f'[core]\ntoplevel = {tmp_path}\n\n[fsck]\nobstrepo_merge_strategy = exactly\n'
    diagnostics = about(check(tmp_path, text), 'fsck', 'obstrepo_merge_strategy')
    assert diagnostics[0].hint is not None
    assert 'exact' in diagnostics[0].hint


@pytest.mark.parametrize('value', ['root', 'mirror@example.com', 'root, admin@example.com'])
def test_an_address_that_could_be_delivered_to_is_accepted(tmp_path: Path, value: str) -> None:
    # A bare local user name is both the default and perfectly deliverable,
    # and the value goes into a header that accepts a list.
    text = f'[core]\ntoplevel = {tmp_path}\n\n[fsck]\nreport_to = {value}\n'
    assert not about(check(tmp_path, text), 'fsck', 'report_to')


@pytest.mark.parametrize('value', ['mirror at example.com', '@example.com', 'mirror@', 'a@b@c', 'root,'])
def test_an_address_that_could_not_be_is_an_error(tmp_path: Path, value: str) -> None:
    text = f'[core]\ntoplevel = {tmp_path}\n\n[fsck]\nreport_to = {value}\n'
    assert about(check(tmp_path, text), 'fsck', 'report_to')


@pytest.mark.parametrize('value', ['git.example.com/manifest.js.gz', 'rsync://git.example.com/m.js', 'https:///m.js'])
def test_a_url_grokmirror_cannot_fetch_is_an_error(tmp_path: Path, value: str) -> None:
    text = good_config(tmp_path).replace('manifest = ${site}/manifest.js.gz', f'manifest = {value}')
    diagnostics = about(check(tmp_path, text), 'remote', 'manifest')
    assert diagnostics[0].severity == 'error'


def test_a_file_url_is_accepted(tmp_path: Path) -> None:
    # fetch_remote_manifest() special-cases these, so the checker has to too.
    text = good_config(tmp_path).replace('manifest = ${site}/manifest.js.gz', f'manifest = file://{tmp_path}/m.js.gz')
    assert not about(check(tmp_path, text), 'remote', 'manifest')


def with_preload(tmp_path: Path, value: str) -> str:
    """good_config() with a preload URL added to [remote], not to the end."""
    return good_config(tmp_path).replace(
        'manifest = ${site}/manifest.js.gz',
        f'manifest = ${{site}}/manifest.js.gz\npreload_bundle_url = {value}',
    )


def test_a_preload_bundle_url_on_a_local_file_is_an_error(tmp_path: Path) -> None:
    # Bundles are fetched with a plain requests get(), and requests has no
    # file:// adapter -- so this would fail into the fallback clone without
    # anybody being told why. The manifest URL does accept file://, which is
    # what makes this worth checking separately.
    text = with_preload(tmp_path, f'file://{tmp_path}/preload/')
    diagnostics = about(check(tmp_path, text), 'remote', 'preload_bundle_url')
    assert diagnostics[0].severity == 'error'
    assert diagnostics[0].hint == 'Expected one of http://, https://'


def test_a_preload_bundle_url_over_http_is_accepted(tmp_path: Path) -> None:
    text = with_preload(tmp_path, 'https://cdn.example.com/preload/')
    assert not about(check(tmp_path, text), 'remote', 'preload_bundle_url')


# -- [remote] site, which is git's to interpret and not ours -----------------


def with_site(tmp_path: Path, value: str) -> str:
    return good_config(tmp_path).replace('site = https://git.example.com', f'site = {value}')


@pytest.mark.parametrize(
    'value',
    [
        # The one that started this: kernel.org pulls over ssh, and the
        # checker used to call its production config broken.
        'ssh://gitolite.kernel.org',
        'git://git.example.com',
        'https://git.example.com',
        'git+ssh://git.example.com',
        # scp-style, which git reads as ssh even though it has no scheme.
        'git@gitolite.kernel.org:',
        'git@git.example.com:pub/scm',
    ],
)
def test_a_transport_git_understands_is_not_our_business(tmp_path: Path, value: str) -> None:
    assert not about(check(tmp_path, with_site(tmp_path, value)), 'remote', 'site')


def test_a_local_site_directory_is_accepted(tmp_path: Path) -> None:
    # git clones happily from a path, and so does grok-pull.
    assert not about(check(tmp_path, with_site(tmp_path, str(tmp_path))), 'remote', 'site')


@pytest.mark.parametrize('value', ['{tmp}/nosuch', 'file://{tmp}/nosuch'])
def test_a_local_site_that_is_not_there_is_an_error(tmp_path: Path, value: str) -> None:
    diagnostics = about(check(tmp_path, with_site(tmp_path, value.format(tmp=tmp_path))), 'remote', 'site')
    assert diagnostics[0].severity == 'error'
    assert 'does not exist' in diagnostics[0].message


def test_a_transport_git_has_never_heard_of_is_only_a_warning(tmp_path: Path) -> None:
    # Never an error: git learns transports from git-remote-<scheme> helpers
    # on PATH, and the checker has no way to know what the host that runs
    # grok-pull has installed.
    diagnostics = about(check(tmp_path, with_site(tmp_path, 'carrierpigeon://git.example.com')), 'remote', 'site')
    assert diagnostics[0].severity == 'warning'
    assert 'git-remote-carrierpigeon' in (diagnostics[0].hint or '')


def test_an_unfamiliar_transport_with_a_helper_installed_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    (tmp_path / 'bin').mkdir()
    helper = tmp_path / 'bin' / 'git-remote-carrierpigeon'
    helper.write_text('#!/bin/sh\n')
    helper.chmod(0o755)
    assert not about(check(tmp_path, with_site(tmp_path, 'carrierpigeon://git.example.com')), 'remote', 'site')


def test_a_site_url_with_no_host_is_an_error(tmp_path: Path) -> None:
    assert about(check(tmp_path, with_site(tmp_path, 'ssh:///pub/scm')), 'remote', 'site')


# -- commands, which grokmirror looks up more strictly than a shell does ------


def test_a_manifest_command_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace(
        'manifest = ${site}/manifest.js.gz',
        f'manifest_command = {tmp_path}/nosuch',
    )
    diagnostics = about(check(tmp_path, text), 'remote', 'manifest_command')
    assert diagnostics[0].severity == 'error'
    assert 'does not exist' in diagnostics[0].message


def test_a_hook_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    text = good_config(tmp_path) + f'post_update_hook = {tmp_path}/nosuch\n'
    diagnostics = about(check(tmp_path, text), 'pull', 'post_update_hook')
    assert diagnostics[0].severity == 'error'
    assert 'does not exist' in diagnostics[0].message


@pytest.mark.skipif(os.geteuid() == 0, reason='root can execute anything, so the check cannot fail')
def test_a_hook_that_is_not_executable_says_which_user_cannot_run_it(tmp_path: Path) -> None:
    hook = tmp_path / 'hook.sh'
    hook.write_text('#!/bin/sh\n', encoding='utf-8')
    hook.chmod(stat.S_IRUSR | stat.S_IWUSR)

    diagnostics = about(check(tmp_path, good_config(tmp_path) + f'post_update_hook = {hook}\n'), 'pull')

    assert 'not executable' in diagnostics[0].message
    assert str(os.geteuid()) in diagnostics[0].message


def test_an_executable_hook_with_arguments_is_accepted(tmp_path: Path) -> None:
    hook = tmp_path / 'hook.sh'
    hook.write_text('#!/bin/sh\n', encoding='utf-8')
    hook.chmod(stat.S_IRWXU)
    text = good_config(tmp_path) + f'post_update_hook = {hook} --quiet\n'
    assert not about(check(tmp_path, text), 'pull', 'post_update_hook')


def test_a_command_on_the_path_is_still_an_error_but_says_so(tmp_path: Path) -> None:
    # grokmirror checks these with os.access() and never consults PATH, so a
    # bare name works in the shell the admin tested it in and fails in the
    # cron job. Saying where it was found is the whole value of the message.
    diagnostics = about(check(tmp_path, good_config(tmp_path) + 'post_update_hook = true\n'), 'pull')
    assert diagnostics[0].severity == 'error'
    assert diagnostics[0].hint is not None
    assert 'PATH' in diagnostics[0].hint


def test_an_unbalanced_quote_in_a_command_is_reported(tmp_path: Path) -> None:
    text = good_config(tmp_path) + 'post_update_hook = /bin/echo "unterminated\n'
    diagnostics = about(check(tmp_path, text), 'pull', 'post_update_hook')
    assert 'cannot be parsed' in diagnostics[0].message


# -- paths -------------------------------------------------------------------


def test_a_file_whose_directory_does_not_exist_is_an_error(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('log = ${toplevel}/grokmirror.log', f'log = {tmp_path}/nodir/grokmirror.log')
    diagnostics = about(check(tmp_path, text), 'core', 'log')
    assert diagnostics[0].severity == 'error'


@pytest.mark.skipif(os.geteuid() == 0, reason='root can write anywhere, so the check cannot fail')
def test_a_file_in_an_unwritable_directory_is_an_error(tmp_path: Path) -> None:
    readonly = tmp_path / 'readonly'
    readonly.mkdir()
    readonly.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        text = good_config(tmp_path).replace('log = ${toplevel}/grokmirror.log', f'log = {readonly}/grokmirror.log')
        diagnostics = about(check(tmp_path, text), 'core', 'log')
        assert 'not writable' in diagnostics[0].message
    finally:
        readonly.chmod(stat.S_IRWXU)


def test_a_relative_path_warns_about_the_directory_it_depends_on(tmp_path: Path) -> None:
    # grok-pull runs from wherever cron or systemd left it, so a relative
    # path means something different depending on who started it.
    text = good_config(tmp_path).replace('log = ${toplevel}/grokmirror.log', 'log = grokmirror.log')
    diagnostics = about(check(tmp_path, text), 'core', 'log')
    assert diagnostics[0].severity == 'warning'
    assert 'relative' in diagnostics[0].message


def test_an_objstore_that_does_not_exist_yet_is_fine(tmp_path: Path) -> None:
    # Unlike toplevel, grokmirror creates this one when it needs it, so its
    # absence is not a problem as long as it could be created.
    text = good_config(tmp_path).replace('loglevel = info', f'objstore = {tmp_path}/objstore')
    assert not about(check(tmp_path, text), 'core', 'objstore')


def test_an_objstore_that_could_not_be_created_is_an_error(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('loglevel = info', f'objstore = {tmp_path}/nodir/objstore')
    diagnostics = about(check(tmp_path, text), 'core', 'objstore')
    assert diagnostics[0].severity == 'error'


# -- everything at once ------------------------------------------------------


def test_every_problem_is_reported_in_one_pass(tmp_path: Path) -> None:
    # The point of the whole feature: one run, one list, rather than fixing
    # one typo at a time and running again to find the next.
    text = f"""
[core]
toplevel = {tmp_path}
loglevel = verbose

[remote]
site = https://git.example.com
manifest = git.example.com/manifest.js.gz

[pull]
pull_threads = lots
purge = please
pul_threads = 4
"""
    diagnostics = check(tmp_path, text)
    reported = {(d.section, d.option) for d in diagnostics}
    assert ('core', 'loglevel') in reported
    assert ('remote', 'manifest') in reported
    assert ('pull', 'pull_threads') in reported
    assert ('pull', 'purge') in reported
    assert ('pull', 'pul_threads') in reported
    assert len(messages(diagnostics, 'error')) == 4
    assert len(messages(diagnostics, 'warning')) == 1


def test_one_bad_toplevel_is_reported_once_and_not_once_per_path_under_it(tmp_path: Path) -> None:
    # Nearly every path in a config is interpolated from ${toplevel}, so
    # without this the one line worth reading is buried under a copy of
    # itself for every option that lives underneath it.
    text = good_config(tmp_path).replace(f'toplevel = {tmp_path}', f'toplevel = {tmp_path}/nowhere')

    diagnostics = check(tmp_path, text)

    assert [(d.section, d.option) for d in diagnostics] == [('core', 'toplevel')]


def test_toplevel_is_checked_first_whatever_order_the_file_uses(tmp_path: Path) -> None:
    # The suppression above works by knowing toplevel is broken before it
    # looks at anything underneath it, which must not depend on where the
    # admin happened to put the line.
    text = f"""
[core]
manifest = ${{toplevel}}/manifest.js.gz
log = ${{toplevel}}/grokmirror.log
toplevel = {tmp_path}/nowhere
"""
    assert [(d.section, d.option) for d in check(tmp_path, text, sections={'core'})] == [('core', 'toplevel')]


def test_a_path_problem_of_its_own_is_still_reported(tmp_path: Path) -> None:
    # Suppression must only cover the paths that fail *because* of the one
    # already reported, not silence the section.
    text = good_config(tmp_path).replace('log = ${toplevel}/grokmirror.log', f'log = {tmp_path}/nodir/grokmirror.log')
    reported = {(d.section, d.option) for d in check(tmp_path, text)}
    assert ('core', 'log') in reported


# -- what grok-pull refuses to start without ---------------------------------


def test_a_config_with_no_remote_section_is_incomplete_for_pulling(tmp_path: Path) -> None:
    # The same rule validate_pull_config() enforces, which is why it lives
    # in configcheck and not in pull.py.
    text = f'[core]\ntoplevel = {tmp_path}\n'
    assert 'must exist in the config file' in messages(check(tmp_path, text))[0]


def test_a_remote_with_no_site_is_incomplete(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('site = https://git.example.com\n', '')
    assert 'must define "site"' in [d.message for d in section_level(check(tmp_path, text), 'remote')]


def test_a_remote_with_neither_manifest_nor_manifest_command_is_incomplete(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace('manifest = ${site}/manifest.js.gz\n', '')
    assert [d.message for d in section_level(check(tmp_path, text), 'remote')] == [
        'must define "manifest" or "manifest_command"'
    ]


def test_both_remote_omissions_are_reported_in_one_pass(tmp_path: Path) -> None:
    # validate_pull_config() stops at the first one, because it is about to
    # give up anyway; the checker exists to list them all.
    text = f'[core]\ntoplevel = {tmp_path}\n\n[remote]\n'
    assert len(section_level(check(tmp_path, text), 'remote')) == 2


def test_a_command_of_nothing_but_whitespace_does_not_crash_the_caller() -> None:
    # Not reachable through a config file -- ConfigParser strips values --
    # but fetch_remote_manifest() tests the string for truth and then goes
    # straight for argv[0], so anything that hands it one had better get a
    # ValueError rather than an IndexError.
    with pytest.raises(ValueError, match='contains no command'):
        command_argv('   ')


def test_a_command_with_an_unbalanced_quote_is_reported_as_such() -> None:
    with pytest.raises(ValueError, match='cannot be parsed'):
        command_argv('/bin/echo "unbalanced')


# -- reachability, which is the only thing that leaves the machine -----------


def test_a_missing_file_url_manifest_is_reported_when_online(tmp_path: Path) -> None:
    text = good_config(tmp_path).replace(
        'manifest = ${site}/manifest.js.gz', f'manifest = file://{tmp_path}/nosuch.js.gz'
    )
    diagnostics = about(check(tmp_path, text, online=True), 'remote', 'manifest')
    assert 'does not exist' in diagnostics[0].message


def test_a_file_url_manifest_that_is_there_is_reachable(tmp_path: Path) -> None:
    (tmp_path / 'm.js.gz').write_bytes(b'')
    text = good_config(tmp_path).replace('manifest = ${site}/manifest.js.gz', f'manifest = file://{tmp_path}/m.js.gz')
    assert not about(check(tmp_path, text, online=True), 'remote', 'manifest')


def test_nothing_is_probed_when_offline(tmp_path: Path) -> None:
    # The same config, with the same missing file, and not a word about it.
    text = good_config(tmp_path).replace(
        'manifest = ${site}/manifest.js.gz', f'manifest = file://{tmp_path}/nosuch.js.gz'
    )
    assert not about(check(tmp_path, text, online=False), 'remote', 'manifest')


def test_a_file_url_with_a_host_in_it_is_an_error(tmp_path: Path) -> None:
    # fetch_remote_manifest() matches "file:///" literally, so file://host/x
    # is handed to requests, which has no adapter for it.
    text = good_config(tmp_path).replace('manifest = ${site}/manifest.js.gz', 'manifest = file://host/manifest.js.gz')
    assert 'three slashes' in str(about(check(tmp_path, text), 'remote', 'manifest')[0].hint)


# -- glob lists, tried against the repositories we actually have --------------


def write_manifest(tmp_path: Path, *gitdirs: str) -> None:
    entries = ',\n'.join(f'"{gitdir}": {{"fingerprint": "abc"}}' for gitdir in gitdirs)
    (tmp_path / 'manifest.js').write_text(f'{{{entries}}}', encoding='utf-8')


def test_a_glob_that_matches_nothing_we_mirror_is_a_warning(tmp_path: Path) -> None:
    # compile_globs() accepts any string, so the only way to find a typo in
    # a glob is to try it against real repository names.
    write_manifest(tmp_path, '/pub/scm/linux/kernel/git/torvalds/linux.git')
    text = good_config(tmp_path).replace('manifest = ${toplevel}/manifest.js.gz', 'manifest = ${toplevel}/manifest.js')
    diagnostics = about(check(tmp_path, text + 'include = /pub/scm/linus/*\n'), 'pull', 'include')
    assert diagnostics[0].severity == 'warning'
    assert 'matches none of the 1 repositories' in diagnostics[0].message


def test_a_glob_that_matches_something_is_left_alone(tmp_path: Path) -> None:
    write_manifest(tmp_path, '/pub/scm/linux/kernel/git/torvalds/linux.git')
    text = good_config(tmp_path).replace('manifest = ${toplevel}/manifest.js.gz', 'manifest = ${toplevel}/manifest.js')
    assert not about(check(tmp_path, text + 'include = /pub/scm/linux/*\n'), 'pull', 'include')


def test_globs_are_not_second_guessed_before_the_first_run(tmp_path: Path) -> None:
    # With no local manifest every pattern matches nothing, which says
    # nothing about the patterns.
    assert not about(check(tmp_path, good_config(tmp_path) + 'include = /pub/scm/linus/*\n'), 'pull', 'include')


# -- lists that are read a line at a time ------------------------------------


def test_a_comma_separated_error_list_is_a_warning(tmp_path: Path) -> None:
    # splitlines() makes "a, b" one entry with a comma in it, which then
    # matches nothing and silently stops ignoring anything.
    text = good_config(tmp_path) + '\n[fsck]\nignore_errors = dangling commit, dangling blob\n'
    diagnostics = about(check(tmp_path, text), 'fsck', 'ignore_errors')
    assert diagnostics[0].severity == 'warning'
    assert 'own indented line' in str(diagnostics[0].hint)


def test_one_entry_per_line_is_what_the_warning_asks_for(tmp_path: Path) -> None:
    text = good_config(tmp_path) + '\n[fsck]\nignore_errors = dangling commit\n    dangling blob\n'
    assert not about(check(tmp_path, text), 'fsck', 'ignore_errors')


def test_a_single_entry_with_no_comma_is_not_second_guessed(tmp_path: Path) -> None:
    text = good_config(tmp_path) + '\n[fsck]\nignore_errors = dangling commit\n'
    assert not about(check(tmp_path, text), 'fsck', 'ignore_errors')


# -- probing something that talks HTTP ---------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Answers GET but refuses HEAD, like a CGI-generated manifest."""

    head_status = 405
    get_status = 200

    def do_HEAD(self) -> None:
        self.send_response(self.head_status)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self) -> None:
        body = b'{}'
        self.send_response(self.get_status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def serving(handler: type[_Handler]) -> Iterator[str]:
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_address[1]}/manifest.js.gz'
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def remote_manifest(tmp_path: Path, url: str) -> str:
    return good_config(tmp_path).replace('manifest = ${site}/manifest.js.gz', f'manifest = {url}')


def test_a_server_that_refuses_head_is_asked_with_get(tmp_path: Path) -> None:
    # A 405 says nothing about whether the manifest is there, so reporting
    # it as unreachable would fail exactly the origins that need checking.
    with serving(_Handler) as url:
        assert not about(check(tmp_path, remote_manifest(tmp_path, url), online=True), 'remote', 'manifest')


def test_a_manifest_url_that_answers_is_left_alone(tmp_path: Path) -> None:
    class Plain(_Handler):
        head_status = 200

    with serving(Plain) as url:
        assert not about(check(tmp_path, remote_manifest(tmp_path, url), online=True), 'remote', 'manifest')


def test_a_manifest_url_that_is_not_there_is_an_error(tmp_path: Path) -> None:
    class Missing(_Handler):
        head_status = 404
        get_status = 404

    with serving(Missing) as url:
        diagnostics = about(check(tmp_path, remote_manifest(tmp_path, url), online=True), 'remote', 'manifest')
        assert diagnostics[0].severity == 'error'
        assert 'HTTP 404' in diagnostics[0].message


def test_a_manifest_url_that_only_404s_on_get_is_still_an_error(tmp_path: Path) -> None:
    # The fallback has to look at what the GET said, not at the 405.
    class Refuses(_Handler):
        get_status = 404

    with serving(Refuses) as url:
        diagnostics = about(check(tmp_path, remote_manifest(tmp_path, url), online=True), 'remote', 'manifest')
    assert 'HTTP 404' in diagnostics[0].message


def test_a_manifest_url_nothing_is_listening_on_is_an_error(tmp_path: Path) -> None:
    with serving(_Handler) as url:
        pass  # The server is gone by the time we check, and the port is free.
    diagnostics = about(check(tmp_path, remote_manifest(tmp_path, url), online=True), 'remote', 'manifest')
    assert 'could not be reached' in diagnostics[0].message


def test_a_malformed_url_is_not_also_probed(tmp_path: Path) -> None:
    # Saying "file://host/m.js.gz is not spelled right" and then "the file
    # host/m.js.gz does not exist" is one error too many, and the second one
    # sends the reader looking for the wrong thing.
    text = remote_manifest(tmp_path, 'file://host/manifest.js.gz')
    assert len(about(check(tmp_path, text, online=True), 'remote', 'manifest')) == 1
