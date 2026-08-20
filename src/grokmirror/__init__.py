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

from __future__ import annotations

import contextlib
import datetime
import fnmatch
import functools
import gzip
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Collection, Iterable, Iterator
from configparser import ConfigParser, ExtendedInterpolation
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, lockf
from pathlib import Path, PurePath
from typing import IO, Literal, TypedDict, Union, overload

import requests
from packaging import version
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Anything that names a path: a str, or a Path, or anything else with a
# __fspath__. Parameters that take a path accept all of them and normalize
# internally, so callers never have to str() a Path on the way in.
# Spelled with Union because this is a runtime assignment, and PEP 604's `|`
# only works in one at 3.10.
StrPath = Union[str, os.PathLike[str]]

VERSION = '3.0-dev'

# The process-wide lock registries. These stay module-level on purpose: POSIX
# fcntl locks belong to the (process, file) pair, not to any Python object.
# Closing any descriptor for a locked file drops all of the process's locks
# on it, and re-locking from the same process always succeeds, so scoping
# these to anything narrower than the process would fake an isolation the
# OS does not provide. Per-run state lives on GrokSession instead.
#
# Since fcntl cannot arbitrate between threads, the registries do double duty
# as the intra-process arbiter for grok-pull's worker threads: a repository
# path present in REPO_LOCKH means "locked by this process", whichever thread
# did it. The mutexes make checking and claiming an entry atomic; a None
# value in REPO_LOCKH marks a claim whose fcntl lock is still being acquired.
MANIFEST_LOCKH: IO[str] | None = None
REPO_LOCKH: dict[str, IO[str] | None] = {}
_MANIFEST_LOCKH_MUTEX = threading.Lock()
_REPO_LOCKH_MUTEX = threading.Lock()
GITBIN = '/usr/bin/git'

# The umask is process-wide state and there is no way to read it without
# setting it, so we read it exactly once, here at import time, while the
# process is still single-threaded. Doing it later -- in the middle of a run,
# as the code used to -- means zeroing the mask of a process whose worker
# threads are busy creating repositories, and anything created inside that
# window lands with mode 0666. Callers that need "the mode a plain create
# would have produced" use file_mode() below rather than touching the mask.
UMASK = os.umask(0)
os.umask(UMASK)

# Ceiling for commands that talk to a remote (fetches, remote manifest
# commands). It exists to unwedge a worker stuck on a dead connection, not to
# police slow transfers, so it is deliberately far above any legitimate
# operation -- even an initial clone of a monster repository over a slow link.
# Hooks and indexing commands run without a timeout: those can legitimately
# take as long as they take.
REMOTE_TIMEOUT = 6 * 3600

# The shared parent logger: every module logs through a getLogger(__name__)
# child of this one, so the handlers init_logger() attaches here serve the
# whole package. No rebinding needed anywhere.
logger = logging.getLogger(__name__)


class RepoInfo(TypedDict, total=False):
    """One repository's entry in the manifest.

    This is the on-the-wire format: it is what grok-manifest writes, what
    grok-pull reads from the origin, and what grokmirror-1.x clients still
    parse. Keys can only ever be added, never renamed or removed.

    Every key is optional, hence ``total=False``. get_repo_defs() always
    supplies ``modified``, ``fingerprint`` and ``head``, and omits the rest
    when it has nothing to say -- an empty description is left out rather
    than written as an empty string. So read with ``.get()`` unless the
    surrounding code has just put the key there itself.

    ``reference`` and ``symlinks`` are added by grok-manifest after the fact,
    and ``private`` only ever exists on grok-pull's own copy of the manifest:
    it is a local judgement about the mirror's ``[pull] private`` config, not
    something the origin tells us.
    """

    modified: int
    fingerprint: str | None
    head: str | None
    owner: str
    description: str
    forkgroup: str | None
    reference: str | None
    symlinks: list[str]
    private: bool


# The manifest itself: repository paths ('/pub/scm/git/git.git', always with
# the leading slash since 2.0) to their entries.
Manifest = dict[str, RepoInfo]

OBST_PREAMBULE = (
    '# WARNING: This is a grokmirror object storage repository.\n'
    '# Deleting or moving it will cause corruption in the following repositories\n'
    '# (caution, this list may be incomplete):\n'
)


class GrokError(Exception):
    """An expected failure: something is wrong with the world, not the code.

    Library functions raise this (or a subclass) instead of calling
    sys.exit(), so that long-running callers can clean up and the test
    suite can assert on failures. Each command() entry point catches it,
    prints the message to stderr, and exits non-zero.
    """


class GrokConfigError(GrokError):
    """The configuration file is missing, unparseable, or incomplete."""


class GrokLockError(GrokError):
    """A repository lock could not be obtained.

    With nonblocking=True this is routine: it means another grokmirror
    process is working on the repository, and the caller should skip it
    or retry later.
    """


class GrokManifestError(GrokError):
    """The remote manifest could not be fetched or parsed."""


class GrokMissingRevisionsError(GrokError):
    """Revisions we expected to find in a repository are not there."""


class GrokSession:
    """Per-run state shared by one grokmirror command.

    Holds what used to live in module globals: the memoized HTTP session
    and the alternates-map caches, the latter keyed by the toplevel they
    were walked from (the old single global served whatever toplevel was
    asked for first). Each command() entry point creates one and threads
    it through; grok-pull's worker threads share it directly. It still
    pickles cleanly (see __getstate__) for anything that needs to send it
    across a process boundary.

    Filesystem lock handles deliberately do NOT live here: fcntl locks
    belong to the process, so the registries next to them are module-level
    (see the comment above MANIFEST_LOCKH).
    """

    def __init__(self) -> None:
        self._requests: requests.Session | None = None
        self._alt_repo_maps: dict[str, dict[str, set[str]]] = {}

    def __getstate__(self) -> dict[str, object]:
        # A live HTTP session cannot cross a process boundary; the
        # alternates caches can, and save each worker a toplevel re-walk.
        state = self.__dict__.copy()
        state['_requests'] = None
        return state

    def get_requests_session(self) -> requests.Session:
        if self._requests is None:
            self._requests = requests.session()
            retry = Retry(connect=3, backoff_factor=0.5)
            adapter = HTTPAdapter(max_retries=retry)
            self._requests.mount('http://', adapter)
            self._requests.mount('https://', adapter)
            self._requests.headers.update({'User-Agent': f'grokmirror/{VERSION}'})
        return self._requests

    def close_requests_session(self) -> None:
        # Forget the session as well as closing it, so a later call gets a
        # fresh one instead of the closed husk.
        if self._requests is not None:
            self._requests.close()
            self._requests = None

    def get_altrepo_map(self, toplevel: StrPath, refresh: bool = False) -> dict[str, set[str]]:
        key = os.path.realpath(toplevel)
        if key not in self._alt_repo_maps or refresh:
            logger.info('   search: finding all repos using alternates')
            amap: dict[str, set[str]] = {}
            tp = Path(toplevel)
            for subp in tp.glob('**/*.git'):
                if subp.is_symlink():
                    # Don't care about symlinks for altrepo mapping
                    continue
                fullpath = subp.resolve().as_posix()
                altrepo = get_altrepo(fullpath)
                if not altrepo:
                    continue
                amap.setdefault(altrepo, set()).add(fullpath)
            self._alt_repo_maps[key] = amap
        return self._alt_repo_maps[key]

    def is_alt_repo(self, toplevel: StrPath, refrepo: str) -> bool:
        amap = self.get_altrepo_map(toplevel)

        looking_for = os.path.realpath(gitdir_to_fullpath(toplevel, refrepo))
        return looking_for in amap

    def find_all_gitdirs(
        self,
        toplevel: StrPath,
        ignore: Collection[str] | None = None,
        normalize: bool = False,
        exclude_objstore: bool = True,
    ) -> set[str]:
        # Opportunistically build the alternates map while we walk, unless
        # one is already cached for this toplevel.
        key = os.path.realpath(toplevel)
        amap: dict[str, set[str]] = {}
        build_amap = key not in self._alt_repo_maps

        if ignore is None:
            ignore = set()

        logger.info('   search: finding all repos in %s', toplevel)
        logger.debug('Ignore list: %s', ' '.join(ignore))
        ignorematch = compile_globs(ignore)
        gitdirs = set()
        for root, dirs, _files in os.walk(toplevel, topdown=True):
            if not dirs:
                continue

            torm = set()
            for name in dirs:
                # os.path.join, not Path: os.walk() is a str API (Path.walk()
                # only arrived in 3.12), and these paths go into a set that is
                # matched against, compared and returned as strings.
                fullpath = os.path.join(root, name)  # noqa: PTH118
                # Should we ignore this dir?
                if ignorematch.match(fullpath):
                    torm.add(name)
                    continue
                if not is_bare_git_repo(fullpath):
                    continue
                if exclude_objstore and Path(fullpath, 'grokmirror.objstore').exists():
                    continue
                if normalize:
                    fullpath = os.path.realpath(fullpath)

                logger.debug('Found %s', os.path.join(root, name))  # noqa: PTH118
                gitdirs.add(fullpath)
                torm.add(name)

                if build_amap:
                    altrepo = get_altrepo(fullpath)
                    if not altrepo:
                        continue
                    amap.setdefault(altrepo, set()).add(fullpath)

            for name in torm:
                # don't recurse into the found *.git dirs
                dirs.remove(name)

        if build_amap:
            self._alt_repo_maps[key] = amap

        return gitdirs


def compile_globs(patterns: Iterable[str]) -> re.Pattern[str]:
    """Compile shell globs from a config option into one regular expression.

    Every one of these lists is tested against every repository, and several
    of them from inside another loop, so on a kernel.org-sized manifest a
    plain "for pattern in patterns: fnmatch()" is hundreds of thousands of
    calls per refresh. One alternation matches them all in a single pass.

    Blank patterns are dropped, and an empty list gives back a pattern that
    matches nothing -- which is what fnmatch() did with a blank glob anyway,
    so an unset config option still means "nothing matches" (as opposed to
    the surprise in reclone_on_errors, where a blank pattern matched
    everything). POSIX only, like fnmatch.fnmatchcase(): no case folding.
    """
    return _compile_globs(tuple(patterns))


@functools.lru_cache(maxsize=128)
def _compile_globs(patterns: tuple[str, ...]) -> re.Pattern[str]:
    # Memoized on the pattern tuple so the callers that rebuild their list per
    # repository (is_private_repo() and friends) do not pay for it every time.
    globs = [fnmatch.translate(x.strip()) for x in patterns if x.strip()]
    # '(?!)' is the regex that can never match, for the empty list.
    return re.compile('|'.join(globs) if globs else '(?!)')


def file_mode(base: int = 0o666) -> int:
    """Return the mode a plain open() would have given a new file.

    Used to put the umask back on files created via tempfile.mkstemp(), which
    deliberately ignores it and creates everything as 0600.
    """
    return base & ~UMASK


def get_config_from_git(
    fullpath: StrPath | None, regexp: str, defaults: dict[str, str] | None = None
) -> dict[str, str]:
    args = ['config', '-z', '--get-regexp', regexp]
    _ecode, out, _err = run_git_command(fullpath, args)
    gitconfig = defaults
    if not gitconfig:
        gitconfig = {}
    if not out:
        return gitconfig

    for line in out.split('\x00'):
        if not line:
            continue
        key, value = line.split('\n', 1)
        try:
            chunks = key.split('.')
            cfgkey = chunks[-1]
            gitconfig[cfgkey.lower()] = value
        except ValueError:
            logger.debug('Ignoring git config entry %s', line)

    return gitconfig


def set_git_config(fullpath: StrPath, param: str, value: str, operation: str = '--replace-all') -> int:
    args = ['config', operation, param, value]
    ecode, _out, _err = run_git_command(fullpath, args)
    return ecode


@functools.cache
def git_newer_than(minver: str) -> bool:
    # Cached because grok-fsck asks this once per repository, and the answer
    # cannot change while we run: it forks git --version every single time.
    (_retcode, output, _error) = run_git_command(None, ['--version'])
    ver = output.split()[-1]
    return version.parse(ver) >= version.parse(minver)


# The decode flag decides whether callers get str or bytes back. Overloads let
# type checkers (and editors) figure that out instead of handing everybody a
# str | bytes union they then have to narrow by hand.
@overload
def run_shell_command(
    cmdargs: list[str],
    stdin: bytes | None = ...,
    decode: Literal[True] = ...,
    env: dict[str, str] | None = ...,
    timeout: float | None = ...,
) -> tuple[int, str, str]: ...


@overload
def run_shell_command(
    cmdargs: list[str],
    stdin: bytes | None = ...,
    *,
    decode: Literal[False],
    env: dict[str, str] | None = ...,
    timeout: float | None = ...,
) -> tuple[int, bytes, bytes]: ...


def run_shell_command(
    cmdargs: list[str],
    stdin: bytes | None = None,
    decode: bool = True,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[int, str, str] | tuple[int, bytes, bytes]:
    # env=None inherits our environment, as subprocess itself does; passing a
    # dict replaces the environment wholesale. Inheriting is load-bearing:
    # git reads proxy and ssh settings from it, and the test suite isolates
    # git purely through environment variables.
    logger.debug('Running: %s', ' '.join(cmdargs))

    try:
        if stdin is None:
            # No stdin means EOF, not our own stdin: a hook that reads its
            # input must not hang on the daemon's inherited descriptor.
            child = subprocess.run(
                cmdargs, stdin=subprocess.DEVNULL, capture_output=True, env=env, timeout=timeout, check=False
            )
        else:
            child = subprocess.run(cmdargs, input=stdin, capture_output=True, env=env, timeout=timeout, check=False)
        returncode = child.returncode
        output, error = child.stdout, child.stderr
    except subprocess.TimeoutExpired as ex:
        logger.critical('Timed out %ss waiting for: %s', ex.timeout, ' '.join(cmdargs))
        # Report it the way timeout(1) would, with whatever output was
        # collected before the command was killed.
        returncode = 124
        output = ex.stdout or b''
        error = ex.stderr or b''

    if decode:
        return returncode, output.decode().strip(), error.decode().strip()

    return returncode, output, error


@overload
def run_git_command(
    fullpath: StrPath | None,
    args: list[str],
    stdin: bytes | None = ...,
    decode: Literal[True] = ...,
    timeout: float | None = ...,
) -> tuple[int, str, str]: ...


@overload
def run_git_command(
    fullpath: StrPath | None,
    args: list[str],
    stdin: bytes | None = ...,
    *,
    decode: Literal[False],
    timeout: float | None = ...,
) -> tuple[int, bytes, bytes]: ...


def run_git_command(
    fullpath: StrPath | None,
    args: list[str],
    stdin: bytes | None = None,
    decode: bool = True,
    timeout: float | None = None,
) -> tuple[int, str, str] | tuple[int, bytes, bytes]:
    _git = os.environ.get('GITBIN', GITBIN)

    if not Path(_git).is_file() and os.access(_git, os.X_OK):
        # we hope for the best by using 'git' without full path
        _git = 'git'

    if fullpath is not None:
        # os.fspath(), not str(): the argv has to be strings all the way
        # through, because run_shell_command() logs it with ' '.join().
        cmdargs = [_git, '--no-pager', '--git-dir', os.fspath(fullpath), *args]
    else:
        cmdargs = [_git, '--no-pager', *args]

    # Spelled out as two calls so the overload above picks the right return type
    if decode:
        return run_shell_command(cmdargs, stdin, decode=True, timeout=timeout)

    return run_shell_command(cmdargs, stdin, decode=False, timeout=timeout)


def _lockname(fullpath: StrPath) -> Path:
    target = Path(fullpath)
    # For a bare filename like "manifest.js.gz" the parent is '.', where
    # os.path.dirname() used to give '' -- so this no longer needs the guard
    # that kept makedirs('') from raising. mkdir('.') with exist_ok is a
    # no-op, and joining onto '.' drops it, so the lock still lands next to
    # the file in the cwd.
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.parent / f'.{target.name}.lock'


def lock_repo(fullpath: StrPath, nonblocking: bool = False) -> None:
    # The registry is keyed by the path *string*. One caller holding a Path
    # and another holding an equal str must land on the same entry, or the
    # double-lock guard below quietly stops guarding anything.
    key = os.fspath(fullpath)
    with _REPO_LOCKH_MUTEX:
        if key in REPO_LOCKH:
            # A second lock would succeed instantly (fcntl locks cannot
            # protect a process from itself) and the first unlock_repo()
            # would release both, so a second lock from anywhere in this
            # process -- same thread or another one -- must fail instead.
            raise GrokLockError(f'{key} is already locked by this process')
        # Claim the entry before touching the lock file. Merely opening a
        # second descriptor of a file this process holds a lock on is enough
        # to lose that lock when the descriptor is closed again, so the
        # check above and this claim must be one atomic step.
        REPO_LOCKH[key] = None

    repolock = _lockname(fullpath)

    logger.debug('Attempting to exclusive-lock %s', repolock)
    try:
        # Deliberately not a context manager: the handle is stashed in
        # REPO_LOCKH and released by unlock_repo(). Callers should prefer
        # locked_repo().
        lockfh = repolock.open('w', encoding='utf-8')
    except OSError:
        with _REPO_LOCKH_MUTEX:
            del REPO_LOCKH[key]
        raise

    flags = (LOCK_EX | LOCK_NB) if nonblocking else LOCK_EX

    try:
        # The fcntl call happens outside the mutex: with nonblocking=False it
        # can wait on another process for a long time, and other threads must
        # still be able to lock and unlock unrelated repositories meanwhile.
        lockf(lockfh, flags)
    except OSError as ex:
        # Don't leak the just-opened handle: with nonblocking=True a held
        # lock is a routine occurrence, not an error. Closing it is safe,
        # because the registry says this process holds no lock on the file.
        lockfh.close()
        with _REPO_LOCKH_MUTEX:
            del REPO_LOCKH[key]
        raise GrokLockError(f'Could not obtain exclusive lock on {repolock}') from ex
    REPO_LOCKH[key] = lockfh


def unlock_repo(fullpath: StrPath) -> None:
    with _REPO_LOCKH_MUTEX:
        lockfh = REPO_LOCKH.pop(os.fspath(fullpath), None)
    if lockfh is not None:
        logger.debug('Unlocking %s', fullpath)
        lockf(lockfh, LOCK_UN)
        lockfh.close()


@contextmanager
def locked_repo(fullpath: StrPath, nonblocking: bool = False) -> Iterator[None]:
    """Hold the exclusive repository lock for the duration of the with block.

    Raises GrokLockError if the lock cannot be obtained; with
    nonblocking=True that happens without waiting whenever another process
    holds the lock. The lock is released however the block exits.
    """
    lock_repo(fullpath, nonblocking=nonblocking)
    try:
        yield
    finally:
        unlock_repo(fullpath)


def gitdir_to_fullpath(toplevel: StrPath, gitdir: str) -> Path:
    """
    Turn a manifest key ('/pub/scm/foo.git') into the path it names under
    toplevel.

    The lstrip is the whole point of having this as a function. Manifest keys
    are spelled with a leading slash, and both os.path.join() and Path()
    *discard everything to the left* of an absolute component, so joining a
    key on directly hands you '/pub/scm/foo.git' on the real filesystem
    rather than a path inside the mirror. That rule was rediscovered by hand
    at eighteen call sites; now it lives in one.
    """
    return Path(toplevel, gitdir.lstrip('/'))


def fullpath_to_gitdir(toplevel: StrPath, fullpath: StrPath) -> str:
    """
    Turn a path under toplevel into the manifest key that names it.

    os.path.relpath, not Path.relative_to: manifest keys have to come out as
    plain strings, and relative_to() raises for anything not under toplevel
    where relpath walks up with '..'. Path only grew walk_up= in 3.12, well
    past our floor. relpath never returns a leading slash of its own, so the
    result always has exactly the one we put there.
    """
    return '/' + os.path.relpath(fullpath, toplevel)


def is_bare_git_repo(path: StrPath) -> bool:
    """
    Return True if path (which is already verified to be a directory)
    sufficiently resembles a base git repo (good enough to fool git
    itself).
    """
    logger.debug('Checking if %s is a git repository', path)
    path = Path(path)
    if (path / 'objects').is_dir() and (path / 'refs').is_dir() and (path / 'HEAD').is_file():
        return True

    logger.debug('Skipping %s: not a git repository', path)
    return False


def get_repo_timestamp(toplevel: StrPath, gitdir: str) -> int:
    ts = 0

    tsfile = gitdir_to_fullpath(toplevel, gitdir) / 'grokmirror.timestamp'
    if tsfile.exists():
        contents = tsfile.read_bytes()
        try:
            ts = int(contents)
            logger.debug('Timestamp for %s: %s', gitdir, ts)
        except ValueError:
            logger.warning('Was not able to parse timestamp in %s', tsfile)
    else:
        logger.debug('No existing timestamp for %s', gitdir)

    return ts


def set_repo_timestamp(toplevel: StrPath, gitdir: str, ts: int) -> None:
    tsfile = gitdir_to_fullpath(toplevel, gitdir) / 'grokmirror.timestamp'

    # int() keeps the truncating behaviour the old '%d' formatting had
    tsfile.write_text(f'{int(ts)}', encoding='utf-8')

    logger.debug('Recorded timestamp for %s: %s', gitdir, ts)


def get_repo_obj_info(fullpath: StrPath) -> dict[str, str]:
    args = ['count-objects', '-v']
    _retcode, output, _error = run_git_command(fullpath, args)
    obj_info = {}

    for line in output.splitlines():
        key, value = line.split(':')
        obj_info[key] = value.strip()

    return obj_info


def get_repo_defs(
    toplevel: StrPath, gitdir: str, usenow: bool = False, ignorerefs: list[str] | None = None
) -> RepoInfo:
    fullpath = gitdir_to_fullpath(toplevel, gitdir)
    description = None
    try:
        contents = (fullpath / 'description').read_bytes().strip()
        if contents and b'edit this file' not in contents:
            # We don't need to tell mirrors to edit this file
            description = contents.decode(errors='replace')
    except OSError:
        pass

    entries = get_config_from_git(fullpath, r'gitweb\..*')
    owner = entries.get('owner', None)

    modified: datetime.datetime | None = None

    if not usenow:
        args = ['for-each-ref', '--sort=-committerdate', '--format=%(committerdate:iso-strict)', '--count=1']
        _ecode, out, _err = run_git_command(fullpath, args)
        if out:
            # Recent git versions render a UTC offset as a trailing 'Z', and
            # datetime.fromisoformat() only understands that from Python 3.11 on,
            # so translate it into the offset spelling every version accepts.
            if out.endswith('Z'):
                out = out[:-1] + '+00:00'
            modified = datetime.datetime.fromisoformat(out)

    if modified is None:
        # Timezone-aware, to match what the committerdate branch above returns.
        # The epoch value we record below is the same either way.
        modified = datetime.datetime.now(tz=datetime.timezone.utc)

    head = None
    with contextlib.suppress(OSError):
        head = Path(fullpath, 'HEAD').read_text(encoding='utf-8').strip()

    forkgroup = None
    altrepo = get_altrepo(fullpath)
    if altrepo and Path(altrepo, 'grokmirror.objstore').exists():
        forkgroup = Path(altrepo).name.removesuffix('.git')

    # we need a way to quickly compare whether mirrored repositories match
    # what is in the master manifest. To this end, we calculate a so-called
    # "state fingerprint" -- basically the output of "git show-ref | sha1sum".
    # git show-ref output is deterministic and should accurately list all refs
    # and their relation to heads/tags/etc.
    fingerprint = get_repo_fingerprint(toplevel, gitdir, force=True, ignorerefs=ignorerefs)
    # Record it in the repo for other use
    set_repo_fingerprint(toplevel, gitdir, fingerprint)
    repoinfo: RepoInfo = {
        'modified': int(modified.timestamp()),
        'fingerprint': fingerprint,
        'head': head,
    }

    # Don't add empty things to manifest
    if owner:
        repoinfo['owner'] = owner
    if description:
        repoinfo['description'] = description
    if forkgroup:
        repoinfo['forkgroup'] = forkgroup

    return repoinfo


def get_altrepo(fullpath: StrPath) -> str | None:
    altdir = None
    try:
        contents = Path(fullpath, 'objects', 'info', 'alternates').read_text(encoding='utf-8').strip()
        altpath = contents.removesuffix('/objects')
        # A bare "/objects" would leave nothing to point at, and realpath('')
        # is the current directory -- exactly the kind of answer we never want.
        if altpath and altpath != contents:
            altdir = os.path.realpath(altpath)
    except OSError:
        pass

    return altdir


def set_altrepo(fullpath: StrPath, altdir: StrPath) -> None:
    # I assume you already checked if this is a sane operation to perform
    altfile = Path(fullpath, 'objects', 'info', 'alternates')
    objpath = Path(altdir, 'objects')
    if objpath.is_dir():
        altfile.write_text(f'{objpath}\n', encoding='utf-8')
    else:
        logger.critical('objdir %s does not exist, not setting alternates file %s', objpath, altfile)


def get_rootsets(
    ses: GrokSession, toplevel: StrPath, obstdir: StrPath
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    top_roots: dict[str, set[str]] = {}
    obst_roots: dict[str, set[str]] = {}
    topdirs = ses.find_all_gitdirs(toplevel, normalize=True, exclude_objstore=True)
    obstdirs = ses.find_all_gitdirs(obstdir, normalize=True, exclude_objstore=False)
    for fullpath in topdirs:
        roots = get_repo_roots(fullpath)
        if roots:
            top_roots[fullpath] = roots

    for fullpath in obstdirs:
        if fullpath in obst_roots:
            continue
        roots = get_repo_roots(fullpath)
        if roots:
            obst_roots[fullpath] = roots

    return top_roots, obst_roots


def get_repo_roots(fullpath: StrPath, force: bool = False) -> set[str] | None:
    if not Path(fullpath).exists():
        logger.debug('Cannot check roots in %s, as it does not exist', fullpath)
        return None
    rfile = Path(fullpath, 'grokmirror.roots')
    if not force and rfile.exists():
        content = rfile.read_text(encoding='utf-8')
        roots = set(content.splitlines())
    else:
        logger.debug('Generating roots for %s', fullpath)
        ecode, out, _err = run_git_command(fullpath, ['rev-list', '--max-parents=0', '--all'])
        if ecode > 0:
            logger.debug('Error listing roots in %s', fullpath)
            return None

        if not out:
            logger.debug('No roots in %s', fullpath)
            return None

        # save it for future use
        rfile.write_text(out, encoding='utf-8')
        logger.debug('Wrote %s', rfile)
        roots = set(out.splitlines())

    return roots


def setup_bare_repo(fullpath: StrPath) -> bool:
    args = ['init', '--bare', os.fspath(fullpath)]
    ecode, _out, _err = run_git_command(None, args)
    if ecode > 0:
        logger.critical('Unable to bare-init %s', fullpath)
        return False

    # Remove .sample files from hooks, because they are just dead weight
    for child in Path(fullpath, 'hooks').iterdir():
        if child.suffix == '.sample':
            child.unlink()
    # We never want auto-gc anywhere
    set_git_config(fullpath, 'gc.auto', '0')
    # We don't care about FETCH_HEAD information and writing to it just
    # wastes IO cycles
    Path(fullpath, 'FETCH_HEAD').symlink_to('/dev/null')
    return True


def setup_objstore_repo(obstdir: StrPath, name: str | None = None) -> str:
    if name is None:
        name = str(uuid.uuid4())
    Path(obstdir).mkdir(parents=True, exist_ok=True)
    obstrepo = str(Path(obstdir, f'{name}.git'))
    logger.debug('Creating objstore repo in %s', obstrepo)
    with locked_repo(obstrepo):
        if not setup_bare_repo(obstrepo):
            raise GrokError(f'Unable to set up an objstore repo in {obstrepo}')
        # All our objects are precious -- we only turn this off when repacking
        set_git_config(obstrepo, 'core.repositoryformatversion', '1')
        set_git_config(obstrepo, 'extensions.preciousObjects', 'true')
        # Set island configs
        set_git_config(obstrepo, 'repack.useDeltaIslands', 'true')
        set_git_config(obstrepo, 'repack.writeBitmaps', 'true')
        set_git_config(obstrepo, 'pack.island', 'refs/virtual/([0-9a-f]+)/', operation='--add')
        Path(obstrepo, 'grokmirror.objstore').write_text(OBST_PREAMBULE, encoding='utf-8')
    return obstrepo


def objstore_virtref(fullpath: StrPath) -> str:
    vh = hashlib.sha1()
    vh.update(os.path.realpath(fullpath).encode())
    return vh.hexdigest()[:12]


def objstore_trim_virtref(obstrepo: StrPath, virtref: str) -> None:
    args = ['for-each-ref', '--format', 'delete %(refname)', f'refs/virtual/{virtref}']
    ecode, out, _err = run_git_command(obstrepo, args)
    if ecode == 0 and out:
        out += '\n'
        args = ['update-ref', '--stdin']
        run_git_command(obstrepo, args, stdin=out.encode())


def remove_from_objstore(obstrepo: StrPath, fullpath: StrPath) -> bool:
    # is fullpath still using us?
    altrepo = get_altrepo(fullpath)
    if altrepo and os.path.realpath(obstrepo) == os.path.realpath(altrepo):
        # Repack the child first, using minimal flags
        args = ['repack', '-abq']
        ecode, _out, _err = run_git_command(fullpath, args)
        if ecode > 0:
            logger.debug('Could not repack child repo %s for removal from %s', fullpath, obstrepo)
            return False
        Path(fullpath, 'objects', 'info', 'alternates').unlink()

    virtref = objstore_virtref(fullpath)
    objstore_trim_virtref(obstrepo, virtref)

    args = ['remote', 'remove', virtref]
    run_git_command(obstrepo, args)
    # Not just missing_ok=True: the old code caught OSError, so a fingerprint
    # we are not allowed to remove has never been fatal here either.
    with contextlib.suppress(OSError):
        Path(obstrepo, f'grokmirror.{virtref}.fingerprint').unlink()
    return True


@overload
def list_repo_remotes(fullpath: StrPath, withurl: Literal[False] = ...) -> list[str]: ...


@overload
def list_repo_remotes(fullpath: StrPath, withurl: Literal[True]) -> list[tuple[str, ...]]: ...


# withurl decides whether callers get bare remote names or (name, url) pairs,
# so overload it the same way run_shell_command() overloads decode.
def list_repo_remotes(fullpath: StrPath, withurl: bool = False) -> list[str] | list[tuple[str, ...]]:
    args = ['remote']
    if withurl:
        args.append('-v')

    _ecode, out, _err = run_git_command(fullpath, args)
    if not out:
        logger.debug('Could not list remotes in %s', fullpath)
        return []

    if not withurl:
        return out.splitlines()

    # git remote -v lists every remote twice (fetch and push), so dedupe --
    # via dict.fromkeys rather than a set, to keep git's ordering.
    return list(dict.fromkeys(tuple(line.split()[:2]) for line in out.splitlines()))


def add_repo_to_objstore(obstrepo: StrPath, fullpath: StrPath) -> bool:
    sibling = os.fspath(fullpath)
    virtref = objstore_virtref(fullpath)
    remotes = list_repo_remotes(obstrepo)
    if virtref in remotes:
        logger.debug('%s is already set up for objstore in %s', fullpath, obstrepo)
        return False

    args = ['remote', 'add', virtref, sibling, '--no-tags']
    ecode, _out, _err = run_git_command(obstrepo, args)
    if ecode > 0:
        logger.critical('Could not add remote to %s', obstrepo)
        raise GrokError(f'Could not add remote to {obstrepo}')
    set_git_config(obstrepo, f'remote.{virtref}.fetch', f'+refs/*:refs/virtual/{virtref}/*')
    telltale = Path(obstrepo, 'grokmirror.objstore')
    knownsiblings = set()
    if telltale.exists():
        for rawline in telltale.read_text(encoding='utf-8').splitlines():
            line = rawline.strip()
            if not line or line[0] == '#':
                continue
            if Path(line).is_dir():
                knownsiblings.add(line)
    knownsiblings.add(sibling)
    telltale.write_text(OBST_PREAMBULE + '\n'.join(sorted(knownsiblings)) + '\n', encoding='utf-8')

    return True


def _fetch_objstore_repo_using_plumbing(srcrepo: StrPath, obstrepo: StrPath, virtref: str) -> bool:
    # Copies objects to objstore repos using direct git plumbing
    # as opposed to using "fetch". See discussion here:
    # http://lore.kernel.org/git/20200720173220.GB2045458@coredump.intra.peff.net
    # First, hardlink all objects and packs
    srcobj = Path(srcrepo, 'objects')
    dstobj = Path(obstrepo, 'objects')
    torm = set()
    for root, dirs, files in os.walk(srcobj, topdown=True):
        if 'info' in dirs:
            dirs.remove('info')
        subpath = os.path.relpath(root, srcobj)
        for file in files:
            srcpath = Path(root, file)
            if file.endswith('.bitmap'):
                torm.add(srcpath)
                continue
            # A multi-pack-index (and its .rev/.bitmap under multi-pack-index.d)
            # describes the packs of the repository it was written in; it must
            # never travel to another one. Child repos are not supposed to have
            # one, but a manual "git repack --write-midx" is enough to create
            # it, so skip them without marking them for removal.
            if file.startswith('multi-pack-index'):
                continue
            # relpath says '.' for the top of the walk, which Path() drops on
            # its own -- the normpath() this used to need is gone with it.
            dstpath = Path(dstobj, subpath, file)
            if not dstpath.exists():
                dstpath.parent.mkdir(parents=True, exist_ok=True)
                os.link(srcpath, dstpath)
                torm.add(srcpath)

    # Now we generate a list of refs on both sides. splitlines() rather than
    # split('\n') matters here: a repository with no refs at all gives us an
    # empty string, and split('\n') would turn that into a set holding one
    # empty string, which the "obj, ref =" unpacking below cannot cope with.
    srcargs = ['for-each-ref', f'--format=%(objectname) refs/virtual/{virtref}/%(refname:lstrip=1)']
    ecode, out, err = run_git_command(srcrepo, srcargs)
    if ecode > 0:
        logger.debug('Could not for-each-ref %s: %s', srcrepo, err)
        return False
    srcset = set(out.splitlines())

    dstargs = ['for-each-ref', '--format=%(objectname) %(refname)', f'refs/virtual/{virtref}']
    ecode, out, err = run_git_command(obstrepo, dstargs)
    if ecode > 0:
        logger.debug('Could not for-each-ref %s: %s', obstrepo, err)
        return False
    dstset = set(out.splitlines())

    # Now we create a stdin list of commands for update-ref
    mapping: dict[str, str] = {}
    for refline in srcset.difference(dstset):
        obj, ref = refline.split(' ', 1)
        mapping[ref] = obj

    cmdlines = []
    for refline in dstset.difference(srcset):
        obj, ref = refline.split(' ', 1)
        if ref in mapping:
            cmdlines.append(f'update {ref} {mapping[ref]} {obj}')
            mapping.pop(ref)
        else:
            cmdlines.append(f'delete {ref} {obj}')

    cmdlines.extend(f'create {ref} {obj}' for ref, obj in mapping.items())

    # One join beats quadratic string concatenation: an objstore repo can hold
    # refs for hundreds of forks.
    commands = ''.join(f'{x}\n' for x in cmdlines)
    logger.debug('stdin=%s', commands)
    args = ['update-ref', '--stdin']
    ecode, out, err = run_git_command(obstrepo, args, stdin=commands.encode())
    if ecode > 0:
        logger.debug('Could not update-ref %s: %s', obstrepo, err)
        return False

    for stale in torm:
        stale.unlink()

    return True


def fetch_objstore_repo(
    obstrepo: StrPath, fullpath: StrPath | None = None, pack_refs: bool = False, use_plumbing: bool = False
) -> bool:
    my_remotes = list_repo_remotes(obstrepo, withurl=True)
    remotes: list[tuple[str, ...]]
    if fullpath:
        # The remotes come back from git as strings, so the sibling has to be
        # one too -- a Path would never match the tuple and every caller
        # holding one would silently be told it is not a known sibling.
        sibling = os.fspath(fullpath)
        virtref = objstore_virtref(sibling)
        if (virtref, sibling) in my_remotes:
            remotes = [(virtref, sibling)]
        else:
            logger.debug('%s is not in remotes for %s', fullpath, obstrepo)
            return False
    else:
        remotes = my_remotes

    success = True
    for virtref, url in remotes:
        if use_plumbing:
            success = _fetch_objstore_repo_using_plumbing(url, obstrepo, virtref)
        else:
            ecode, _out, _err = run_git_command(obstrepo, ['fetch', virtref, '--prune'])
            if ecode > 0:
                success = False

        if success:
            r_fp = Path(url, 'grokmirror.fingerprint')
            if r_fp.exists():
                shutil.copy(r_fp, Path(obstrepo, f'grokmirror.{virtref}.fingerprint'))
            if pack_refs:
                try:
                    with locked_repo(obstrepo, nonblocking=True):
                        run_git_command(obstrepo, ['pack-refs'])
                except (OSError, GrokLockError):
                    # Next run will take care of it
                    pass

        else:
            logger.info('Could not fetch objects from %s to %s', url, obstrepo)

    return success


def is_private_repo(config: ConfigParser, fullpath: StrPath) -> bool:
    privmasks = config['core'].get('private', '')
    # os.fspath(), because this is a glob match against the path *as text*,
    # and re never matches a Path.
    return bool(compile_globs(privmasks.splitlines()).match(os.fspath(fullpath)))


def find_siblings(
    fullpath: str, my_roots: set[str] | None, known_roots: dict[str, set[str]], exact: bool = False
) -> set[str]:
    siblings = set()
    for gitpath, gitroots in known_roots.items():
        # Of course we're going to match ourselves
        if fullpath == gitpath or not my_roots or not gitroots or not gitroots.intersection(my_roots):
            continue
        if gitroots == my_roots:
            siblings.add(gitpath)
            continue
        if exact:
            continue
        if gitroots.issubset(my_roots) or my_roots.issubset(gitroots):
            siblings.add(gitpath)
            continue
        sumdiff = len(gitroots.difference(my_roots)) + len(my_roots.difference(gitroots))
        # If we only differ by a single root, consider us siblings
        if sumdiff <= 2:
            siblings.add(gitpath)
            continue

    return siblings


def find_best_obstrepo(
    mypath: str, obst_roots: dict[str, set[str]], toplevel: str, baselines: list[str], minratio: float = 0.2
) -> str | None:
    # We want to find a repo with best intersect len to total roots len ratio,
    # but we'll ignore any repos where the ratio is too low, in order not to lump
    # together repositories that have very weak common histories.
    myroots = get_repo_roots(mypath)
    if not myroots:
        return None
    obstrepo = None
    bestratio = 0.0
    baselinematch = compile_globs(baselines)
    for path, roots in obst_roots.items():
        if path == mypath or not roots:
            continue
        icount = len(roots.intersection(myroots))
        if icount == 0:
            # No match at all
            continue
        # Baseline repos win over the ratio logic
        if baselines:
            # Any of its member siblings match baselines?
            s_remotes = list_repo_remotes(path, withurl=True)
            for _virtref, childpath in s_remotes:
                gitdir = fullpath_to_gitdir(toplevel, childpath)
                if baselinematch.match(gitdir):
                    # Use this one
                    return path

        ratio = icount / len(roots)
        if ratio < minratio:
            continue
        if ratio > bestratio:
            obstrepo = path
            bestratio = ratio

    return obstrepo


def get_obstrepo_mapping(obstdir: StrPath) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not Path(obstdir).is_dir():
        return mapping
    for child in Path(obstdir).iterdir():
        if child.is_dir() and child.suffix == '.git':
            obstrepo = child.as_posix()
            ecode, out, _err = run_git_command(obstrepo, ['remote', '-v'])
            if ecode > 0:
                # weird
                continue
            for line in out.splitlines():
                chunks = line.split()
                if len(chunks) < 2:
                    continue
                _name, url = chunks[:2]
                if url in mapping:
                    continue
                # Does it still exist?
                if not Path(url).is_dir():
                    continue
                mapping[url] = obstrepo
    return mapping


def find_objstore_repo_for(obstdir: StrPath, fullpath: StrPath) -> str | None:
    if not Path(obstdir).is_dir():
        return None

    logger.debug('Finding an objstore repo matching %s', fullpath)
    virtref = objstore_virtref(fullpath)
    for child in Path(obstdir).iterdir():
        if child.is_dir() and child.suffix == '.git':
            obstrepo = child.as_posix()
            remotes = list_repo_remotes(obstrepo)
            if virtref in remotes:
                logger.debug('Found %s', child.name)
                return obstrepo

    logger.debug('No matching objstore repos for %s', fullpath)
    return None


def get_forkgroups(obstdir: StrPath, toplevel: StrPath) -> dict[str, set[str]]:
    forkgroups: dict[str, set[str]] = {}
    if not Path(obstdir).exists():
        return forkgroups
    for child in Path(obstdir).iterdir():
        if child.is_dir() and child.suffix == '.git':
            forkgroup = child.stem
            forkgroups[forkgroup] = set()
            obstrepo = child.as_posix()
            remotes = list_repo_remotes(obstrepo, withurl=True)
            for _virtref, url in remotes:
                # Objstore remotes point at local child repos; anything not
                # under our toplevel (as a path, not a string prefix) is not ours.
                if not PurePath(url).is_relative_to(toplevel):
                    continue
                forkgroups[forkgroup].add(url)
    return forkgroups


def get_ignorerefs(config: GrokConfigParser) -> list[str]:
    """Refs to leave out of fingerprints, from [manifest]ignore_refs.

    A fingerprint is only useful next to another one, and the other one is
    usually the origin's. Every command that fingerprints has to leave out the
    same refs, or a replica disagrees with the manifest about repositories that
    are perfectly up to date -- and grok-pull refetches them every run, for
    ever, because the fingerprints can never converge.
    """
    if 'manifest' not in config:
        return []
    return [x.strip() for x in config['manifest'].get('ignore_refs', '').splitlines() if x.strip()]


def get_repo_fingerprint(
    toplevel: StrPath, gitdir: str, force: bool = False, ignorerefs: list[str] | None = None
) -> str | None:
    fullpath = gitdir_to_fullpath(toplevel, gitdir)
    if not fullpath.exists():
        logger.debug('Cannot fingerprint %s, as it does not exist', fullpath)
        return None

    fpfile = fullpath / 'grokmirror.fingerprint'
    if not force and fpfile.exists():
        fingerprint = fpfile.read_text(encoding='utf-8')
        logger.debug('Fingerprint for %s: %s', gitdir, fingerprint)
    else:
        logger.debug('Generating fingerprint for %s', gitdir)
        ecode, out, err = run_git_command(fullpath, ['show-ref'])
        if ecode > 0:
            # Not the same thing as having no refs: git refuses to list any of
            # them if it cannot parse even one, so this is usually a damaged
            # ref file. grok-fsck reports it when the repository drops out of
            # the manifest; here we only make the log say what happened.
            logger.debug('Could not list refs in %s: %s', fullpath, err.strip())
            return None
        if not out:
            logger.debug('No heads in %s, nothing to fingerprint.', fullpath)
            return None

        if ignorerefs:
            hasher = hashlib.sha1()
            ignorematch = compile_globs(ignorerefs)
            for line in out.splitlines():
                _rhash, rname = line.split(maxsplit=1)
                if ignorematch.match(rname):
                    continue
                hasher.update(line.encode() + b'\n')

            fingerprint = hasher.hexdigest()
        else:
            # We add the final "\n" to be compatible with cmdline output
            # of git-show-ref
            fingerprint = hashlib.sha1(out.encode() + b'\n').hexdigest()

        # Save it for future use
        if not force:
            set_repo_fingerprint(toplevel, gitdir, fingerprint)

    return fingerprint


def set_repo_fingerprint(
    toplevel: StrPath, gitdir: str, fingerprint: str | None = None, ignorerefs: list[str] | None = None
) -> str | None:
    fpfile = gitdir_to_fullpath(toplevel, gitdir) / 'grokmirror.fingerprint'

    if fingerprint is None:
        # Whatever lands in the file is what every later comparison reads, so
        # it has to be calculated the same way the caller calculates the rest.
        fingerprint = get_repo_fingerprint(toplevel, gitdir, force=True, ignorerefs=ignorerefs)
        if fingerprint is None:
            # The repo has no refs at all (or is gone), so there is nothing to
            # record. Writing it out anyway stores the literal string "None",
            # which every reader then treats as a valid fingerprint.
            logger.debug('No fingerprint to record for %s', gitdir)
            return None

    fpfile.write_text(fingerprint, encoding='utf-8')

    logger.debug('Recorded fingerprint for %s: %s', gitdir, fingerprint)
    return fingerprint


def is_obstrepo(fullpath: StrPath, obstdir: StrPath | None = None) -> bool:
    if obstdir:
        # At this point, both should be normalized. Compare as paths, not as
        # strings: /srv/objstore-private/x.git is not inside /srv/objstore.
        return PurePath(fullpath).is_relative_to(obstdir)
    # Just check if it has a grokmirror.objstore file in the repo
    return Path(fullpath, 'grokmirror.objstore').exists()


def manifest_lock(manifile: StrPath) -> None:
    global MANIFEST_LOCKH  # noqa: PLW0603 -- process-wide by design; see the comment on the declaration
    # The mutex is held across the blocking fcntl call. That is safe from
    # deadlock: if the check below passes, no thread of this process holds
    # the manifest lock, so no thread can be inside manifest_unlock() -- the
    # wait, if any, is on another process. It is a separate mutex from the
    # repository registry one, so repo locking is not stalled meanwhile.
    with _MANIFEST_LOCKH_MUTEX:
        if MANIFEST_LOCKH is not None:
            # Opening a second handle would not block (fcntl locks cannot
            # protect a process from itself), and worse: the moment the
            # replaced handle got garbage-collected, the lock would silently
            # drop while we believed we still held it.
            raise GrokLockError(f'Manifest {manifile} is already locked by this process')

        manilock = _lockname(manifile)
        # Deliberately not a context manager: released by manifest_unlock().
        # Callers should prefer locked_manifest().
        lockfh = manilock.open('w', encoding='utf-8')
        logger.debug('Attempting to lock %s', manilock)
        try:
            lockf(lockfh, LOCK_EX)
        except OSError:
            lockfh.close()
            raise
        MANIFEST_LOCKH = lockfh
        logger.debug('Manifest lock obtained')


def manifest_unlock(manifile: StrPath) -> None:
    global MANIFEST_LOCKH  # noqa: PLW0603 -- process-wide by design; see the comment on the declaration
    with _MANIFEST_LOCKH_MUTEX:
        lockfh = MANIFEST_LOCKH
        MANIFEST_LOCKH = None
    if lockfh is not None:
        logger.debug('Unlocking manifest %s', manifile)
        lockf(lockfh, LOCK_UN)
        lockfh.close()


@contextmanager
def locked_manifest(manifile: StrPath) -> Iterator[None]:
    """Hold the exclusive manifest lock for the duration of the with block.

    The lock is released however the block exits. Note that read_manifest's
    wait loop drops and re-takes this lock while it waits for the manifest
    to appear, which is why the lock handle lives at module level.
    """
    manifest_lock(manifile)
    try:
        yield
    finally:
        manifest_unlock(manifile)


def read_manifest(manifile: StrPath, wait: bool = False) -> Manifest:
    manipath = Path(manifile)
    while True:
        if not wait or manipath.exists():
            break
        logger.info(' manifest: manifest does not exist yet, waiting ...')
        # Unlock the manifest so other processes aren't waiting for us
        was_locked = False
        if MANIFEST_LOCKH is not None:
            was_locked = True
            manifest_unlock(manifile)
        time.sleep(1)
        if was_locked:
            manifest_lock(manifile)

    if not manipath.exists():
        logger.info(' manifest: no local manifest, assuming initial run')
        return {}

    opener = gzip.open if manipath.suffix == '.gz' else open

    logger.debug('Reading %s', manifile)
    with opener(manifile, 'rt', encoding='utf-8') as fh:
        jdata = fh.read()

    try:
        manifest = json.loads(jdata)
    except ValueError:
        # We'll regenerate the file entirely on failure to parse
        logger.critical('Unable to parse %s, will regenerate', manifile)
        manifest = {}

    logger.debug('Manifest contains %s entries', len(manifest.keys()))

    return manifest


def write_manifest(manifile: StrPath, manifest: Manifest, mtime: int | None = None, pretty: bool = False) -> None:
    manipath = Path(manifile)
    logger.debug('Writing new %s', manipath)

    (fd, tmpname) = tempfile.mkstemp(prefix=manipath.name, dir=manipath.parent)
    tmpfile = Path(tmpname)
    fh = os.fdopen(fd, 'wb', 0)
    logger.debug('Created a temporary file in %s', tmpfile)
    logger.debug('Writing to %s', tmpfile)
    try:
        jdata = json.dumps(manifest, indent=2, sort_keys=True) if pretty else json.dumps(manifest)

        jbytes = jdata.encode('utf-8')
        if manipath.suffix == '.gz':
            gfh = gzip.GzipFile(fileobj=fh, mode='wb')
            gfh.write(jbytes)
            gfh.close()
        else:
            fh.write(jbytes)

        os.fsync(fd)
        fh.close()
        # mkstemp() always creates 0600, so put the umask back on
        tmpfile.chmod(file_mode())
        if mtime is not None:
            logger.debug('Setting mtime to %s', mtime)
            os.utime(tmpfile, (mtime, mtime))
        logger.debug('Moving %s to %s', tmpfile, manipath)
        # Path.replace (i.e. os.replace), not shutil.move: the whole tempfile
        # dance exists to make this step atomic, and shutil.move quietly
        # degrades to copy+unlink. Same directory as the target, so this can
        # never cross a filesystem.
        tmpfile.replace(manipath)

    finally:
        # If something failed, don't leave these trailing around
        if tmpfile.exists():
            logger.debug('Removing %s', tmpfile)
            tmpfile.unlink()


class GrokConfigParser(ConfigParser):
    # Carries the config file's mtime, so we can notice when it changes
    last_modified: int = 0


def load_config_file(cfgfile: StrPath) -> GrokConfigParser:
    if not Path(cfgfile).exists():
        raise GrokConfigError(f'File does not exist: {cfgfile}')
    config = GrokConfigParser(interpolation=ExtendedInterpolation())
    config.read(cfgfile, encoding='utf-8')

    if 'core' not in config:
        raise GrokConfigError(
            f'Section [core] must exist in: {cfgfile}\n       Perhaps this is a grokmirror-1.x config file?'
        )

    cfgtoplevel = config['core'].get('toplevel')
    if not cfgtoplevel:
        raise GrokConfigError(f'Section [core] must define "toplevel" in: {cfgfile}')

    toplevel = os.path.realpath(Path(cfgtoplevel).expanduser())
    if not os.access(toplevel, os.W_OK):
        raise GrokConfigError(f'Toplevel {toplevel} does not exist or is not writable')
    # Just in case we did expanduser
    config['core']['toplevel'] = toplevel

    obstdir = config['core'].get('objstore', None)
    if obstdir is None:
        obstdir = str(Path(toplevel, 'objstore'))
        config['core']['objstore'] = obstdir

    # Handle some other defaults
    manifile = config['core'].get('manifest')
    if not manifile:
        config['core']['manifest'] = str(Path(toplevel, 'manifest.js.gz'))

    fstat = Path(cfgfile).stat()
    # stick last config file modification date into the config object,
    # so we can catch config file updates. Whole seconds, same as the old
    # fstat[8] indexing gave us -- it goes into the manifest status file and
    # gets compared for equality, so it must stay an int.
    config.last_modified = int(fstat.st_mtime)

    return config


def is_precious(fullpath: StrPath) -> bool:
    args = ['config', '--get', 'extensions.preciousObjects']
    _retcode, output, _error = run_git_command(fullpath, args)
    return output.strip().lower() in ('yes', 'true', '1')


def get_repack_level(
    obj_info: dict[str, str],
    max_loose_objects: int = 1200,
    max_packs: int = 20,
    pc_loose_objects: int = 10,
    pc_loose_size: int = 10,
) -> int:
    # for now, hardcode the maximum loose objects and packs
    # XXX: we can probably set this in git config values?
    #      I don't think this makes sense as a global setting, because
    #      optimal values will depend on the size of the repo as a whole
    packs = int(obj_info['packs'])
    count_loose = int(obj_info['count'])

    needs_repack = 0

    # first, compare against max values:
    if packs >= max_packs:
        logger.debug('Triggering full repack because packs > %s', max_packs)
        needs_repack = 2
    elif count_loose >= max_loose_objects:
        logger.debug('Triggering quick repack because loose objects > %s', max_loose_objects)
        needs_repack = 1
    else:
        # is the number of loose objects or their size more than 10% of
        # the overall total?
        in_pack = int(obj_info['in-pack'])
        size_loose = int(obj_info['size'])
        size_pack = int(obj_info['size-pack'])
        total_obj = count_loose + in_pack
        total_size = size_loose + size_pack
        # If we have an alternate, then add those numbers in
        alternate = obj_info.get('alternate')
        altpath = alternate.removesuffix('/objects') if alternate else None
        if altpath and altpath != alternate:
            alt_obj_info = get_repo_obj_info(altpath)
            total_obj += int(alt_obj_info['in-pack'])
            total_size += int(alt_obj_info['size-pack'])

        # set some arbitrary "worth bothering" limits so we don't
        # continuously repack tiny repos.
        if total_obj > 500 and count_loose / total_obj * 100 >= pc_loose_objects:
            logger.debug('Triggering repack because loose objects > %s%% of total', pc_loose_objects)
            needs_repack = 1
        elif total_size > 1024 and size_loose / total_size * 100 >= pc_loose_size:
            logger.debug('Triggering repack because loose size > %s%% of total', pc_loose_size)
            needs_repack = 1

    return needs_repack


def init_logger(subcommand: str, logfile: str | None, loglevel: int, verbose: bool) -> logging.Logger:
    logger.setLevel(logging.DEBUG)

    if logfile:
        fh = logging.handlers.WatchedFileHandler(Path(logfile).expanduser(), encoding='utf-8')
        formatter = logging.Formatter(subcommand + '[%(process)d] %(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        fh.setLevel(loglevel)
        logger.addHandler(fh)

    ch = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    ch.setFormatter(formatter)

    if verbose:
        ch.setLevel(logging.INFO)
    else:
        ch.setLevel(logging.CRITICAL)

    logger.addHandler(ch)
    return logger
