# Copyright (C) 2013-2020 by The Linux Foundation and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Check a config file and report everything wrong with it at once.

The commands themselves fail on the first problem, which is right for a
command that is about to do work but wrong for somebody trying to get a
hand-written config correct: fix one typo, run again, find the next. So
nothing here raises for a config problem. Each check appends a Diagnostic
and carries on, and the caller decides what to do with the list.

Nothing here writes anything, and nothing here runs a configured command:
checking a config must be safe to do against a mirror that is running, and
safe to do before pointing grokmirror at a directory for the first time.
"""

from __future__ import annotations

import argparse
import configparser
import difflib
import json
import os
import pwd
import re
import shlex
import shutil
import sys
from configparser import ConfigParser, ExtendedInterpolation
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import requests

from grokmirror import GrokSession, StrPath, compile_globs, configopts, read_manifest
from grokmirror.configopts import KNOWN, Option

Severity = Literal['error', 'warning']

# Schemes fetch_remote_manifest() knows how to handle. Anything else is not
# going to be fetched, whatever else it might mean. An individual option can
# accept fewer than these -- see Option.schemes -- but never more.
URL_SCHEMES = ('http', 'https', 'file')

# Transports git has built in. A scheme outside this list is not wrong by
# itself -- git will look for a git-remote-<scheme> helper on PATH -- so it
# is only worth mentioning when no such helper is installed.
GIT_SCHEMES = ('ssh', 'git', 'http', 'https', 'ftp', 'ftps', 'file', 'git+ssh', 'ssh+git')

# "[user@]host:path", which git reads as ssh even though it has no scheme.
# A value with a slash before the colon is a path that happens to contain
# one, not an scp-style address, which is why the pattern anchors.
SCP_LIKE = re.compile(r'^[^/:]+(:\d+)?:')

# Kinds whose readers cannot make anything of a blank value: get_bool() and
# get_int() both raise GrokConfigError on one, at the moment the option is
# first read. In an unattended run that is a long way from here, so a blank
# one of these has to be reported rather than passed over as "the same as not
# set" -- otherwise a config this check calls clean is one the command refuses
# to start on.
BLANK_IS_FATAL = ('int', 'bool')


@dataclass(frozen=True)
class Diagnostic:
    """One thing wrong with the config, or one thing worth a second look."""

    severity: Severity
    # Where it was found. Both are None for a problem with the file itself.
    section: str | None
    option: str | None
    # One line, human-readable, no colour and no trailing punctuation.
    message: str
    # What to do about it, when there is something useful to say.
    hint: str | None = None

    def __str__(self) -> str:
        where = ''
        if self.section and self.option:
            where = f'[{self.section}] {self.option}: '
        elif self.section:
            where = f'[{self.section}]: '
        return f'{self.severity}: {where}{self.message}'


class _Report:
    """Collects diagnostics in the order they are found."""

    def __init__(self) -> None:
        self.diagnostics: list[Diagnostic] = []
        # Paths already reported as missing or unusable. Nearly every path
        # in a config is interpolated from ${toplevel}, so one wrong
        # toplevel otherwise reports the same missing directory once per
        # option that lives under it, burying the one line worth reading.
        self.bad_paths: set[Path] = set()

    def path_is_bad(self, path: Path) -> None:
        self.bad_paths.add(path)

    def already_reported(self, path: Path) -> bool:
        return bool(self.bad_paths.intersection({path, *path.parents}))

    def has_errors_about(self, section: str, option: str) -> bool:
        return any(d.severity == 'error' and d.section == section and d.option == option for d in self.diagnostics)

    def error(
        self, message: str, section: str | None = None, option: str | None = None, hint: str | None = None
    ) -> None:
        self.diagnostics.append(Diagnostic('error', section, option, message, hint))

    def warning(
        self, message: str, section: str | None = None, option: str | None = None, hint: str | None = None
    ) -> None:
        self.diagnostics.append(Diagnostic('warning', section, option, message, hint))


def checking_as() -> tuple[int, str]:
    """Return the effective uid and user name the checks are running as.

    Every writability answer here is os.access()'s, which answers for the
    calling user. grok-pull normally runs as a service user out of a systemd
    unit, so a check run by hand can cheerfully report "writable" about a
    directory the mirror itself cannot write to. Naming the user is what
    makes that answer readable rather than misleading.
    """
    uid = os.geteuid()
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        # A uid with no passwd entry is normal in a container.
        name = str(uid)
    return uid, name


def _as_user() -> str:
    uid, name = checking_as()
    return f'{name} (uid {uid})'


def _check_int(report: _Report, section: str, option: str, value: str) -> None:
    try:
        int(value)
    except ValueError:
        report.error(f'must be a whole number, not: {value}', section, option)


def _check_bool(report: _Report, section: str, option: str, value: str) -> None:
    if value.strip().lower() not in ConfigParser.BOOLEAN_STATES:
        report.error(
            f'must be a boolean, not: {value}',
            section,
            option,
            hint='Accepted values are yes/no, true/false, on/off and 1/0',
        )


def _check_enum(report: _Report, section: str, option: str, value: str, known: Option) -> None:
    if value not in known.choices:
        hint = None
        close = difflib.get_close_matches(value, known.choices, n=1)
        if close:
            hint = f'Did you mean "{close[0]}"?'
        report.error(
            f'must be one of {", ".join(known.choices)}, not: {value}',
            section,
            option,
            hint,
        )


def _check_email(report: _Report, section: str, option: str, value: str) -> None:
    # These go straight into an email header, which accepts a list, and a
    # bare local user name like "root" is both the default and perfectly
    # valid. So this only looks for the shapes that cannot be an address.
    for raw in value.split(','):
        address = raw.strip()
        if not address:
            report.error(f'has an empty address in: {value}', section, option)
            continue
        if len(address.split()) > 1:
            report.error(f'does not look like an address: {address}', section, option)
            continue
        if '@' in address:
            local, _, domain = address.rpartition('@')
            if not local or not domain or '@' in local:
                report.error(f'does not look like an address: {address}', section, option)


def _check_url(report: _Report, section: str, option: str, value: str, known: Option) -> None:
    schemes = known.schemes or URL_SCHEMES
    expected = f'Expected one of {", ".join(f"{scheme}://" for scheme in schemes)}'
    parsed = urlparse(value)
    if not parsed.scheme:
        report.error(f'has no scheme, so it cannot be fetched: {value}', section, option, hint=expected)
        return
    if parsed.scheme not in schemes:
        # Naming the general set here would be a lie for an option that
        # accepts fewer, so the hint is built from this option's own.
        report.error(f'has a scheme grokmirror cannot fetch: {parsed.scheme}://', section, option, hint=expected)
        return
    if parsed.scheme in ('http', 'https') and not parsed.netloc:
        report.error(f'has no host: {value}', section, option)
    if parsed.scheme == 'file' and not value.startswith('file:///'):
        # fetch_remote_manifest() matches the file:/// spelling literally
        # and strips it; anything else is handed to requests, which has no
        # file:// adapter, so it fails much later and much less clearly.
        report.error(
            f'is a file URL with a host in it, which grokmirror cannot fetch: {value}',
            section,
            option,
            hint='A local path is written file:///absolute/path, with three slashes',
        )


def _check_giturl(report: _Report, section: str, option: str, value: str) -> None:
    """Check a URL that only ever gets handed to git.

    grok-pull joins the gitdir onto [remote] site and passes the result to
    "git remote add", so the set of things that work here is git's, not
    requests': ssh://, git:// and a plain local path are all fine, and
    restricting this to what grokmirror can fetch would report a working
    production config as broken.

    What is left to check is thin on purpose. git can be taught new
    transports by dropping a git-remote-<scheme> helper on PATH, so an
    unfamiliar scheme is a warning when no helper is there and silence when
    one is -- never an error, because the checker cannot know what the host
    running grok-pull has installed.
    """
    parsed = urlparse(value)
    if not parsed.scheme:
        if SCP_LIKE.match(value):
            # git@host:path. Nothing here is ours to verify: whether the
            # host answers and whether the key is authorized are questions
            # for ssh, and asking them is not a config check.
            return
        # Everything else without a scheme is a local path, which git clones
        # from directly.
        _check_local_path(report, section, option, Path(value).expanduser())
        return

    if parsed.scheme == 'file':
        _check_local_path(report, section, option, Path(value.removeprefix('file://')))
        return

    if parsed.scheme not in GIT_SCHEMES:
        if shutil.which(f'git-remote-{parsed.scheme}'):
            return
        report.warning(
            f'uses a transport git does not have built in: {parsed.scheme}://',
            section,
            option,
            hint=f'git would look for git-remote-{parsed.scheme} on PATH, and there is none here',
        )
        return

    if not parsed.netloc:
        report.error(f'has no host: {value}', section, option)


def _check_local_path(report: _Report, section: str, option: str, path: Path) -> None:
    """Complain about a local path we are supposed to read from but cannot."""
    if not path.exists():
        report.error(f'does not exist: {path}', section, option)
    elif not os.access(path, os.R_OK):
        report.error(f'is not readable by {_as_user()}: {path}', section, option)


def command_argv(value: str) -> list[str]:
    """Split a configured command line into argv, or say why it cannot run.

    Raises ValueError with a message that reads as the continuation of the
    option name it came from ("[remote] manifest_command does not exist:
    ..."), so that the config check and the code that actually runs the
    command can each put their own subject in front of the same sentence.

    PATH is deliberately not consulted, because the code that runs these
    does not consult it either: a bare "true" works in the shell the admin
    tested it in and fails from cron. A value that is only whitespace is a
    ValueError here rather than an IndexError at argv[0] later.
    """
    try:
        args = shlex.split(value)
    except ValueError as ex:
        # An unbalanced quote, which shlex refuses to guess about.
        raise ValueError(f'cannot be parsed as a command: {ex}') from ex
    if not args:
        raise ValueError('is set but contains no command')
    if os.access(args[0], os.X_OK):
        return args
    if Path(args[0]).exists():
        raise ValueError(f'is not executable by {_as_user()}: {args[0]}')
    raise ValueError(f'does not exist: {args[0]}')


def _check_command(report: _Report, section: str, option: str, value: str) -> None:
    try:
        command_argv(value)
    except ValueError as ex:
        problem = str(ex)
    else:
        return

    # The tempting mistake is a bare name that happens to be on PATH, so
    # look there for the hint -- and only for the hint.
    hint = None
    try:
        args = shlex.split(value)
    except ValueError:
        args = []
    if args and (found := shutil.which(args[0])) and not Path(args[0]).exists():
        hint = f'Found "{args[0]}" on PATH, but grokmirror needs the full path: {found}'
    report.error(problem, section, option, hint)


def _check_path(report: _Report, section: str, option: str, value: str, known: Option) -> None:
    path = Path(value).expanduser()
    if not path.is_absolute():
        # Every command resolves these against its own cwd, and grok-pull
        # runs from wherever cron or systemd left it.
        report.warning(
            f'is a relative path, resolved against the current directory: {value}',
            section,
            option,
            hint=f'It currently means {path.resolve()}',
        )
        path = path.resolve()

    if known.writable:
        if path.is_dir():
            if not os.access(path, os.W_OK):
                report.error(f'is not writable by {_as_user()}: {path}', section, option)
                report.path_is_bad(path)
        elif path.exists():
            report.error(f'is not a directory: {path}', section, option)
            report.path_is_bad(path)
        elif known.must_exist:
            report.error(f'does not exist: {path}', section, option)
            report.path_is_bad(path)
        else:
            _check_parent(report, section, option, path, creating=True)
        return

    if known.parent_writable:
        if path.is_dir():
            report.error(f'is a directory, but should be a file: {path}', section, option)
        elif path.exists():
            if not os.access(path, os.W_OK):
                report.error(f'is not writable by {_as_user()}: {path}', section, option)
        else:
            _check_parent(report, section, option, path, creating=False)


def _check_parent(report: _Report, section: str, option: str, path: Path, creating: bool) -> None:
    parent = path.parent
    what = 'it' if creating else 'the file'
    if report.already_reported(parent):
        # Whatever is wrong with the directory above has been said once
        # already, and saying it again for every option underneath it only
        # buries the line worth reading.
        return
    if not parent.is_dir():
        report.error(
            f'does not exist, and neither does the directory it would go in: {parent}',
            section,
            option,
        )
        report.path_is_bad(parent)
    elif not os.access(parent, os.W_OK):
        report.error(
            f'does not exist, and {parent} is not writable by {_as_user()}, so {what} cannot be created',
            section,
            option,
        )
        report.path_is_bad(parent)


def _check_strlist(report: _Report, section: str, option: str, value: str) -> None:
    """Guess at a comma-separated list written where lines were wanted.

    These options are read with splitlines(), so "a, b" is one item with a
    comma in it rather than two items -- and for [fsck] ignore_errors that
    silently stops matching anything. It is only a guess, because a git
    error message really can contain a comma, so it says so.
    """
    if len(value.splitlines()) > 1 or ', ' not in value:
        return
    report.warning(
        f'looks like a comma-separated list, but is read as one line: {value}',
        section,
        option,
        hint='Put each entry on its own indented line, as in the example config',
    )


def _check_value(report: _Report, section: str, option: str, value: str, known: Option) -> None:
    """Run whichever check the registry says this option's kind deserves."""
    if not value.strip() and known.kind != 'email':
        if known.kind in BLANK_IS_FATAL:
            report.error(
                'is set to nothing, which is not a value it can have',
                section,
                option,
                hint='Give it a value, or take the line out to use the default',
            )
        # Every other kind falls back cleanly, so an option set to nothing is
        # the same as one that is not set, except that somebody meant to set
        # it -- and that is not enough to report.
        return
    if known.kind == 'int':
        _check_int(report, section, option, value)
    elif known.kind == 'bool':
        _check_bool(report, section, option, value)
    elif known.kind == 'enum':
        _check_enum(report, section, option, value, known)
    elif known.kind == 'email':
        _check_email(report, section, option, value)
    elif known.kind == 'url':
        _check_url(report, section, option, value, known)
    elif known.kind == 'giturl':
        _check_giturl(report, section, option, value)
    elif known.kind == 'command':
        _check_command(report, section, option, value)
    elif known.kind == 'path':
        _check_path(report, section, option, value, known)
    elif known.kind == 'strlist':
        _check_strlist(report, section, option, value)
    # str, globlist and args have no shape to be wrong about: compile_globs()
    # accepts any string, and a globlist is splitlines() where a single line
    # is a perfectly good list of one.


def _read_config(report: _Report, cfgfile: StrPath) -> ConfigParser | None:
    """Parse the file, or explain why it cannot be parsed.

    Returns None when nothing downstream would be meaningful: there is no
    point checking option values in a file ConfigParser could not read.
    """
    path = Path(cfgfile)
    if not path.exists():
        report.error(f'file does not exist: {path}')
        return None
    if path.is_dir():
        report.error(f'is a directory, not a config file: {path}')
        return None
    if not os.access(path, os.R_OK):
        report.error(f'file is not readable by {_as_user()}: {path}')
        return None

    config = ConfigParser(interpolation=ExtendedInterpolation())
    try:
        config.read(path, encoding='utf-8')
    except UnicodeDecodeError:
        report.error(f'file is not valid UTF-8: {path}')
        return None
    except configparser.DuplicateSectionError as ex:
        report.error(f'section [{ex.section}] appears more than once', hint='Merge the two into one section')
        return None
    except configparser.DuplicateOptionError as ex:
        report.error(f'option "{ex.option}" appears more than once in [{ex.section}]', ex.section, ex.option)
        return None
    except configparser.MissingSectionHeaderError:
        report.error(
            f'file begins with an option outside any section: {path}',
            hint='Every option belongs under a [section] header',
        )
        return None
    except configparser.ParsingError as ex:
        for lineno, line in ex.errors:
            report.error(f'cannot be parsed, line {lineno}: {line.strip()}')
        return None
    return config


def _resolve(report: _Report, config: ConfigParser, section: str, option: str) -> str | None:
    """Read one option, reporting an interpolation that does not resolve.

    ExtendedInterpolation is lazy: read() is perfectly happy with a
    ${core:toplvel} that resolves to nothing, and the command only finds
    out when it reads that option, which may be well into a run.
    """
    hint = 'References are written ${option} within a section, or ${section:option} across them'
    try:
        return config.get(section, option)
    except configparser.InterpolationMissingOptionError as ex:
        # ConfigParser's own message restates the section and option we are
        # already reporting against, so use just the part it alone knows:
        # which reference it could not resolve.
        report.error(f'refers to ${{{ex.reference}}}, which is not set', section, option, hint)
    except configparser.InterpolationError as ex:
        report.error(f'cannot be resolved: {ex.message.strip()}', section, option, hint)
    return None


def _check_names(report: _Report, config: ConfigParser, sections: set[str]) -> None:
    """Warn about sections and options nothing will ever read.

    This is the one that catches an ordinary typo. ConfigParser cannot: a
    misspelled option is indistinguishable from one that was never set, so
    the command runs and quietly does not do what was written down.
    """
    for section in config.sections():
        if section in KNOWN:
            continue
        hint = None
        close = difflib.get_close_matches(section, list(KNOWN), n=1)
        if close:
            hint = f'Did you mean [{close[0]}]?'
        report.warning('unknown section, nothing will read it', section, hint=hint)

    inherited = set(config.defaults())
    for section in sorted(sections):
        if section not in config:
            continue
        known_options = KNOWN[section]
        for option in config[section]:
            # Options from [DEFAULT] show up in every section. They are
            # usually there to be interpolated into other values rather
            # than read, so they are not this section's business.
            if option in inherited or option in known_options:
                continue
            hint = None
            close = difflib.get_close_matches(option, list(known_options), n=1)
            if close:
                hint = f'Did you mean "{close[0]}"?'
            report.warning('unknown option, nothing will read it', section, option, hint)


def check_remote_completeness(config: ConfigParser) -> list[Diagnostic]:
    """Report what [remote] is missing before grok-pull can do anything.

    grok-pull refuses to start without these, and validate_pull_config()
    logs exactly these diagnostics to say so, which is why the rule lives
    here: a config the checker passes and grok-pull then rejects would be
    worse than no checker at all.

    Values are read raw, because whether an option is set is a different
    question from whether its interpolation resolves, and the latter is
    reported against the option itself.
    """
    report = _Report()
    if 'remote' not in config:
        report.error('must exist in the config file', 'remote')
        return report.diagnostics
    if not config.get('remote', 'site', raw=True, fallback='').strip():
        report.error('must define "site"', 'remote')
    manifest = config.get('remote', 'manifest', raw=True, fallback='').strip()
    command = config.get('remote', 'manifest_command', raw=True, fallback='').strip()
    if not manifest and not command:
        report.error('must define "manifest" or "manifest_command"', 'remote')
    return report.diagnostics


def _probe_url(report: _Report, ses: GrokSession, section: str, option: str, value: str) -> None:
    """Ask whether a URL answers, without fetching what is behind it.

    Uses the same requests session grok-pull fetches with, so the
    User-Agent and the retry policy are the ones the origin will actually
    see. A manifest can be tens of megabytes, so this never reads a body:
    HEAD, and a GET abandoned at the headers for the servers that will not
    answer HEAD.
    """
    parsed = urlparse(value)
    if parsed.scheme == 'file':
        # fetch_remote_manifest() strips the scheme and stats the path, so
        # "reachable" here means the same thing it means there.
        _check_local_path(report, section, option, Path(value.removeprefix('file://')))
        return

    session = ses.get_requests_session()
    try:
        # 30 seconds to connect, 60 to answer: the real fetch allows five
        # minutes for the body, and there is no body here.
        res = session.head(value, timeout=(30, 60), allow_redirects=True)
        if res.status_code in (405, 501):
            # Plenty of manifests are generated by a CGI that only knows
            # GET, and a refused HEAD says nothing about whether the
            # manifest is there. Ask again and hang up at the headers.
            res = session.get(value, timeout=(30, 60), allow_redirects=True, stream=True)
            res.close()
    except requests.exceptions.RequestException as ex:
        report.error(f'could not be reached: {ex}', section, option)
        return
    if res.status_code >= 400:
        report.error(
            f'returned HTTP {res.status_code} ({res.reason})',
            section,
            option,
            hint='Checked with HEAD, falling back to GET' if res.request.method == 'GET' else None,
        )


def _check_reachable(report: _Report, config: ConfigParser, ses: GrokSession) -> None:
    """Probe the one remote URL that names a single file.

    [remote] site is a base URL for git clones and [remote]
    preload_bundle_url is a directory of bundles; neither names anything a
    web server has to answer for, so probing them would report a 403 on a
    directory listing as though the mirror were broken. Only the manifest
    URL is a file grokmirror will really ask for.
    """
    if 'remote' not in config:
        return
    value = _resolve(report, config, 'remote', 'manifest') if 'manifest' in config['remote'] else None
    if not value or not value.strip():
        return
    if report.has_errors_about('remote', 'manifest'):
        # _check_url() has already said the URL is malformed, and probing
        # it can only say the same thing again in a less useful way.
        return
    _probe_url(report, ses, 'remote', 'manifest', value)


def _check_globs(report: _Report, config: ConfigParser, sections: set[str]) -> None:
    """Warn about glob lists that match nothing we are currently mirroring.

    A glob list is not wrong in itself -- compile_globs() accepts any
    string -- so the only way to find a typo in one is to try it against
    real repository names. The local manifest is the list grokmirror
    itself works from, it costs nothing to read, and a pattern matching
    none of it is the shape a misplaced path prefix takes.

    Nothing is said when there is no local manifest yet: on a first run
    every pattern matches nothing, which is not news.
    """
    if 'core' not in config:
        return
    manifest_path = _resolve(report, config, 'core', 'manifest') if 'manifest' in config['core'] else None
    if not manifest_path:
        return
    try:
        manifest = read_manifest(manifest_path)
    except OSError:
        # An unreadable manifest is _check_path()'s business, not ours.
        return
    if not manifest:
        return

    for section, option in configopts.options_of_kind('globlist'):
        if section not in sections or section not in config or option not in config[section]:
            continue
        value = _resolve(report, config, section, option)
        if value is None or not value.strip():
            continue
        patterns = compile_globs(value.splitlines())
        if any(patterns.match(gitdir) for gitdir in manifest):
            continue
        report.warning(
            f'matches none of the {len(manifest)} repositories in the local manifest',
            section,
            option,
            hint='Patterns are matched against the manifest path, which starts with a /',
        )


def check_config(cfgfile: StrPath, sections: set[str], online: bool = True) -> list[Diagnostic]:
    """Check a config file and return everything found wrong with it.

    `sections` is the set of sections the calling command actually reads,
    so that grok-pull does not opine on [fsck]. Sections nobody named are
    still recognised as real ones -- an unread section is not a mistake,
    an unknown one is.

    With `online` false nothing leaves the machine. The rest of the checks
    are identical, so a config that passes offline has not been told the
    origin is unreachable -- it has been told nothing about the origin.
    """
    report = _Report()
    config = _read_config(report, cfgfile)
    if config is None:
        return report.diagnostics

    # load_config_file() refuses a file without these, whichever command is
    # running, so they are checked regardless of which sections were asked
    # for.
    if 'core' not in config:
        report.error(
            'section [core] is missing',
            hint='Every grokmirror config needs [core] with at least a toplevel. '
            'A config with no [core] at all may be a grokmirror-1.x one',
        )
    elif not config['core'].get('toplevel', '').strip():
        report.error('is missing, and nothing can run without it', 'core', 'toplevel')

    _check_names(report, config, sections)

    if 'remote' in sections:
        report.diagnostics.extend(check_remote_completeness(config))

    # [core] toplevel goes first, whatever order the file lists things in.
    # Almost every other path is interpolated from it, so knowing it is
    # broken is what lets those paths stay quiet about the same directory.
    checked: set[tuple[str, str]] = set()
    if 'core' in config and config['core'].get('toplevel', '').strip():
        _check_one(report, config, 'core', 'toplevel')
        checked.add(('core', 'toplevel'))

    for section in sorted(sections):
        if section not in config:
            continue
        for option in config[section]:
            if (section, option) not in checked:
                _check_one(report, config, section, option)

    _check_globs(report, config, sections)

    if online and 'remote' in sections:
        ses = GrokSession()
        try:
            _check_reachable(report, config, ses)
        finally:
            ses.close_requests_session()

    return report.diagnostics


def _check_one(report: _Report, config: ConfigParser, section: str, option: str) -> None:
    known = configopts.lookup(section, option)
    if known is None:
        # Already reported by _check_names(), and with nothing in the
        # registry there is nothing to check the value against.
        return
    value = _resolve(report, config, section, option)
    if value is not None:
        _check_value(report, section, option, value, known)


# -- the command-line side ---------------------------------------------------
#
# One config file serves every command, but each command only reads part of
# it, so --config-check means something slightly different in each of them.
# The flags, the output and the exit code are shared from here so that they
# cannot answer differently depending on which command you asked.

# Sections are printed in this order rather than in the order the file
# happens to list them, so that two runs against two configs are comparable.
SECTION_ORDER = tuple(KNOWN)


def add_check_arguments(op: argparse.ArgumentParser) -> None:
    """Add --config-check and its two modifiers to a command's parser."""
    op.add_argument(
        '--config-check',
        dest='config_check',
        action='store_true',
        default=False,
        help='Check the configuration file and exit, reporting every problem found',
    )
    op.add_argument(
        '--json',
        dest='as_json',
        action='store_true',
        default=False,
        help='With --config-check, write the report as a JSON object instead of text',
    )
    op.add_argument(
        '--no-network',
        dest='no_network',
        action='store_true',
        default=False,
        help='With --config-check, skip the checks that contact the remote site',
    )


def check_arguments(op: argparse.ArgumentParser, opts: argparse.Namespace) -> None:
    """Reject the modifiers when there is nothing for them to modify.

    Quietly ignoring --json would be worse than refusing it: whatever was
    going to parse the output gets a mirror run instead.
    """
    if opts.config_check:
        return
    for flag, used in (('--json', opts.as_json), ('--no-network', opts.no_network)):
        if used:
            op.error(f'{flag} only means something together with --config-check')


def _by_section(diagnostics: list[Diagnostic]) -> list[tuple[str | None, list[Diagnostic]]]:
    """Group diagnostics by section, in a stable order.

    Anything about the file itself comes first, since a file that will not
    parse makes every other line beside the point.
    """
    sections = {d.section for d in diagnostics}
    known = [s for s in SECTION_ORDER if s in sections]
    unknown = sorted(s for s in sections if s is not None and s not in KNOWN)
    order: list[str | None] = [None] if None in sections else []
    order += known + unknown
    return [(section, [d for d in diagnostics if d.section == section]) for section in order]


def _counts(diagnostics: list[Diagnostic]) -> tuple[int, int]:
    errors = sum(1 for d in diagnostics if d.severity == 'error')
    return errors, len(diagnostics) - errors


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def format_report(cfgfile: StrPath, diagnostics: list[Diagnostic], online: bool) -> str:
    """Render the diagnostics as the text a person reads."""
    errors, warnings = _counts(diagnostics)
    lines = []
    for section, found in _by_section(diagnostics):
        lines.append(f'[{section}]' if section else f'{cfgfile}')
        for diag in found:
            where = f'{diag.option}: ' if diag.option else ''
            lines.append(f'  {diag.severity}: {where}{diag.message}')
            if diag.hint:
                lines.append(f'    {diag.hint}')
        lines.append('')

    if not diagnostics:
        lines.append(f'{cfgfile}: nothing to report')
    else:
        lines.append(f'{_plural(errors, "error")}, {_plural(warnings, "warning")}')
    if not online:
        lines.append('The remote site was not contacted (--no-network), so nothing here is about reachability.')
    # Last, and always, because it is the one thing that makes a "writable"
    # answer mean anything: os.access() answers for whoever is asking, and
    # grok-pull normally runs as somebody else entirely.
    lines.append(f'Checked as {_as_user()}; whether a path is writable was answered for that user.')
    return '\n'.join(lines)


def format_json(cfgfile: StrPath, diagnostics: list[Diagnostic], online: bool) -> str:
    """Render the diagnostics as the object the maintainer UI parses.

    The shape is a compatibility surface as soon as it ships, so it is
    deliberately dull: no nesting beyond the list, every key always
    present, and `hint` null rather than absent when there is none.
    """
    uid, user = checking_as()
    errors, warnings = _counts(diagnostics)
    return json.dumps(
        {
            'config': str(cfgfile),
            'checked_as': {'uid': uid, 'user': user},
            'online': online,
            'ok': not errors,
            'diagnostics': [
                {
                    'severity': d.severity,
                    'section': d.section,
                    'option': d.option,
                    'message': d.message,
                    'hint': d.hint,
                }
                for d in diagnostics
            ],
            'summary': {'errors': errors, 'warnings': warnings},
        },
        indent=2,
    )


def run_check(cfgfile: StrPath, sections: set[str], as_json: bool = False, online: bool = True) -> int:
    """Check a config, print the report and return the exit code to use.

    Warnings do not fail the run: they are the checks that guess, and a
    guess is not grounds for failing somebody's cron job.
    """
    diagnostics = check_config(cfgfile, sections, online=online)
    render = format_json if as_json else format_report
    sys.stdout.write(render(cfgfile, diagnostics, online) + '\n')
    return 1 if any(d.severity == 'error' for d in diagnostics) else 0
