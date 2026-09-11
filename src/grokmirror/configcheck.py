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

import configparser
import difflib
import os
import pwd
import shlex
import shutil
from configparser import ConfigParser, ExtendedInterpolation
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from grokmirror import StrPath, configopts
from grokmirror.configopts import KNOWN, Option

Severity = Literal['error', 'warning']

# Schemes fetch_remote_manifest() knows how to handle. Anything else is not
# going to be fetched, whatever else it might mean.
URL_SCHEMES = ('http', 'https', 'file')


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


def _check_url(report: _Report, section: str, option: str, value: str) -> None:
    parsed = urlparse(value)
    if not parsed.scheme:
        report.error(
            f'has no scheme, so it cannot be fetched: {value}',
            section,
            option,
            hint=f'Expected one of {", ".join(f"{scheme}://" for scheme in URL_SCHEMES)}',
        )
        return
    if parsed.scheme not in URL_SCHEMES:
        report.error(
            f'has a scheme grokmirror cannot fetch: {parsed.scheme}://',
            section,
            option,
            hint=f'Expected one of {", ".join(f"{scheme}://" for scheme in URL_SCHEMES)}',
        )
        return
    if parsed.scheme in ('http', 'https') and not parsed.netloc:
        report.error(f'has no host: {value}', section, option)


def split_command(value: str) -> list[str]:
    """Split a configured command line the way grokmirror runs it.

    Returns an empty list for a value that is only whitespace, which is
    worth catching: the code that runs these goes straight for the first
    word.
    """
    return shlex.split(value)


def _check_command(report: _Report, section: str, option: str, value: str) -> None:
    try:
        args = split_command(value)
    except ValueError as ex:
        # An unbalanced quote, which shlex refuses to guess about.
        report.error(f'cannot be parsed as a command: {ex}', section, option)
        return
    if not args:
        report.error('is set but contains no command', section, option)
        return
    executable = args[0]
    if os.access(executable, os.X_OK):
        return
    if Path(executable).exists():
        report.error(f'is not executable by {_as_user()}: {executable}', section, option)
        return
    hint = None
    if shutil.which(executable):
        # A bare name that happens to be on PATH is the tempting mistake:
        # it works in a shell and fails in grokmirror, which checks the
        # value with os.access() and never consults PATH.
        hint = f'Found "{executable}" on PATH, but grokmirror needs the full path: {shutil.which(executable)}'
    report.error(f'does not exist: {executable}', section, option, hint)


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


def _check_value(report: _Report, section: str, option: str, value: str, known: Option) -> None:
    """Run whichever check the registry says this option's kind deserves."""
    if not value.strip() and known.kind != 'email':
        # An option set to nothing is the same as one that is not set,
        # except that somebody meant to set it -- but every caller here
        # falls back cleanly, so there is nothing to report.
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
        _check_url(report, section, option, value)
    elif known.kind == 'command':
        _check_command(report, section, option, value)
    elif known.kind == 'path':
        _check_path(report, section, option, value, known)
    # str, globlist, strlist and args have no shape to be wrong about:
    # compile_globs() accepts any string, and the list kinds are splitlines()
    # where a single line is a perfectly good list of one.


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


def check_config(cfgfile: StrPath, sections: set[str]) -> list[Diagnostic]:
    """Check a config file and return everything found wrong with it.

    `sections` is the set of sections the calling command actually reads,
    so that grok-pull does not opine on [fsck]. Sections nobody named are
    still recognised as real ones -- an unread section is not a mistake,
    an unknown one is.
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
