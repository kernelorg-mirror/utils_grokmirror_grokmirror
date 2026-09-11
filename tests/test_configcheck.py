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

import os
import stat
from pathlib import Path

import pytest

from grokmirror.configcheck import Diagnostic, check_config

ALL_SECTIONS = {'core', 'manifest', 'remote', 'pull', 'fsck'}


def check(tmp_path: Path, text: str, sections: set[str] | None = None) -> list[Diagnostic]:
    """Write a config file and check it.

    The config is written as text rather than through ConfigParser, since
    several of the problems worth catching are ones ConfigParser would
    refuse to write in the first place.
    """
    cfgfile = tmp_path / 'grokmirror.conf'
    cfgfile.write_text(text, encoding='utf-8')
    return check_config(cfgfile, sections if sections is not None else ALL_SECTIONS)


def messages(diagnostics: list[Diagnostic], severity: str | None = None) -> list[str]:
    return [str(d) for d in diagnostics if severity is None or d.severity == severity]


def about(diagnostics: list[Diagnostic], section: str, option: str | None = None) -> list[Diagnostic]:
    return [d for d in diagnostics if d.section == section and (option is None or d.option == option)]


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
    assert check(tmp_path, text) == []


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
    assert [(d.section, d.option) for d in check(tmp_path, text)] == [('core', 'toplevel')]


def test_a_path_problem_of_its_own_is_still_reported(tmp_path: Path) -> None:
    # Suppression must only cover the paths that fail *because* of the one
    # already reported, not silence the section.
    text = good_config(tmp_path).replace('log = ${toplevel}/grokmirror.log', f'log = {tmp_path}/nodir/grokmirror.log')
    reported = {(d.section, d.option) for d in check(tmp_path, text)}
    assert ('core', 'log') in reported
