# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the parts of grokmirror that need no repository.

These are all cheap, so they cover the awkward inputs too: the sizes nobody has
on disk yet, the relative paths nobody passes on purpose, the empty strings that
turn up when a config option is present but blank. Several of the crashes fixed
in this tree were exactly that kind of input.
"""

from __future__ import annotations

import errno
import gzip
import json
import os
import pathlib
import pickle
import subprocess
import threading
from configparser import ExtendedInterpolation
from pathlib import Path
from typing import IO

import pytest

import grokmirror
import grokmirror.fsck

KIB = 1
MIB = 1024
GIB = 1024 * 1024
TIB = 1024 * 1024 * 1024


class TestHumanSize:
    """get_human_size() takes a size in KiB and formats it for the report."""

    @pytest.mark.parametrize(
        ('kbsize', 'expected'),
        [
            (0, '0.00 KiB'),
            (1, '1.00 KiB'),
            (1023, '1023.00 KiB'),
            (MIB, '1.00 MiB'),
            (MIB * 1023, '1023.00 MiB'),
            (GIB, '1.00 GiB'),
            (GIB * 1023, '1023.00 GiB'),
            # 1 TiB and up used to raise IndexError: the unit list ran out.
            # Thankfully few git repositories are this big, which is why nobody
            # hit it -- at git.kernel.org the whole collection is around 50GB,
            # thanks to object sharing.
            (TIB, '1.00 TiB'),
            (TIB * 5, '5.00 TiB'),
            (TIB * 1024, '1024.00 TiB'),
            (TIB * 1024 * 1024, '1048576.00 TiB'),
        ],
    )
    def test_boundaries(self, kbsize: int, expected: str) -> None:
        assert grokmirror.fsck.get_human_size(kbsize) == expected

    def test_no_size_raises(self) -> None:
        # Every size the report can be handed must format, without exception.
        for exponent in range(64):
            assert grokmirror.fsck.get_human_size(2**exponent)


class TestLockName:
    """Lock files live next to the file they protect."""

    def test_absolute_path(self, tmp_path: Path) -> None:
        target = tmp_path / 'sub' / 'manifest.js.gz'
        assert grokmirror._lockname(str(target)) == str(tmp_path / 'sub' / '.manifest.js.gz.lock')
        # The containing directory gets created, since the manifest may not
        # exist yet on a mirror's first run.
        assert (tmp_path / 'sub').is_dir()

    def test_bare_filename(self) -> None:
        # os.path.dirname('manifest.js.gz') is '', and makedirs('') raises
        # FileNotFoundError instead of being a no-op, so `grok-manifest -m
        # manifest.js.gz` used to die before doing any work. A cron job running
        # from the manifest's own directory is a perfectly plausible setup.
        assert grokmirror._lockname('manifest.js.gz') == '.manifest.js.gz.lock'

    def test_manifest_lock_round_trip_with_a_relative_name(self, tmp_path: Path) -> None:
        os.chdir(tmp_path)
        grokmirror.manifest_lock('manifest.js.gz')
        try:
            assert (tmp_path / '.manifest.js.gz.lock').exists()
        finally:
            grokmirror.manifest_unlock('manifest.js.gz')
        assert grokmirror.MANIFEST_LOCKH is None

    def test_repo_lock_round_trip(self, tmp_path: Path) -> None:
        target = str(tmp_path / 'repo.git')
        grokmirror.lock_repo(target)
        try:
            assert target in grokmirror.REPO_LOCKH
        finally:
            grokmirror.unlock_repo(target)
        assert target not in grokmirror.REPO_LOCKH

    def test_repo_lock_is_exclusive(self, tmp_path: Path) -> None:
        target = str(tmp_path / 'repo.git')
        grokmirror.lock_repo(target)
        try:
            # A second holder must not be able to take it. This is what keeps two
            # grok-fsck runs out of the same repository.
            code = (
                'import sys, grokmirror\n'
                f'try:\n    grokmirror.lock_repo({target!r}, nonblocking=True)\n'
                'except grokmirror.GrokLockError:\n    sys.exit(7)\n'
                'sys.exit(0)\n'
            )
            res = subprocess.run(['python3', '-c', code], capture_output=True, text=True, check=False)
            assert res.returncode == 7, res.stderr
        finally:
            grokmirror.unlock_repo(target)


class TestLockedRepo:
    """locked_repo() must release the lock however the block exits."""

    def test_releases_on_success(self, tmp_path: Path) -> None:
        target = str(tmp_path / 'repo.git')
        with grokmirror.locked_repo(target):
            assert target in grokmirror.REPO_LOCKH
        assert target not in grokmirror.REPO_LOCKH

    def test_releases_on_exception(self, tmp_path: Path) -> None:
        # This is how sys.exit() deep inside library code used to kill a
        # process while it still held repository locks: an exception raised
        # under a manual lock/unlock pair skipped the unlock.
        target = str(tmp_path / 'repo.git')
        with pytest.raises(grokmirror.GrokError, match='boom'), grokmirror.locked_repo(target):
            raise grokmirror.GrokError('boom')
        assert target not in grokmirror.REPO_LOCKH

    def test_failed_lock_does_not_leak_the_handle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The nonblocking path fails routinely (it means "someone else is
        # working on this repo"), so it must close the lockfile handle it
        # just opened rather than leak it.
        target = str(tmp_path / 'repo.git')
        seen: list[IO[str]] = []

        def deny(fh: IO[str], _flags: int) -> None:
            seen.append(fh)
            raise BlockingIOError(errno.EAGAIN, 'locked elsewhere')

        monkeypatch.setattr(grokmirror, 'lockf', deny)
        with pytest.raises(grokmirror.GrokLockError):
            grokmirror.lock_repo(target, nonblocking=True)
        assert target not in grokmirror.REPO_LOCKH
        assert len(seen) == 1
        assert seen[0].closed


class TestDoubleLocking:
    """Locking what this process already holds is a bug, and now says so.

    fcntl locks cannot protect a process from itself: taking the same lock a
    second time succeeds instantly, and closing either handle silently drops
    both locks. Worse, manifest_lock() used to overwrite the stored handle,
    so the "held" lock quietly evaporated as soon as the replaced handle was
    garbage-collected.
    """

    def test_repo_double_lock_raises(self, tmp_path: Path) -> None:
        target = str(tmp_path / 'repo.git')
        with grokmirror.locked_repo(target), pytest.raises(grokmirror.GrokLockError, match='already locked'):
            grokmirror.lock_repo(target)
        # The refusal must not have clobbered the original lock's bookkeeping
        assert target not in grokmirror.REPO_LOCKH

    def test_manifest_double_lock_raises(self, tmp_path: Path) -> None:
        manifile = str(tmp_path / 'manifest.js.gz')
        with grokmirror.locked_manifest(manifile), pytest.raises(grokmirror.GrokLockError, match='already locked'):
            grokmirror.manifest_lock(manifile)
        assert grokmirror.MANIFEST_LOCKH is None

    def test_threaded_lock_race_has_a_single_winner(self, tmp_path: Path) -> None:
        # grok-pull's workers are threads now, and fcntl cannot arbitrate
        # between the threads of one process: every thread's lockf() call on
        # the same file succeeds. The lock registry is the real gate, so
        # checking for an entry and claiming it must be one atomic step --
        # with a plain check-then-set, two threads starting together could
        # both pass the check and both believe they hold the repository.
        target = str(tmp_path / 'repo.git')
        nthreads = 8
        barrier = threading.Barrier(nthreads)
        outcomes: list[bool] = []

        def contend() -> None:
            barrier.wait()
            try:
                grokmirror.lock_repo(target, nonblocking=True)
                outcomes.append(True)
            except grokmirror.GrokLockError:
                outcomes.append(False)

        threads = [threading.Thread(target=contend) for _ in range(nthreads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert outcomes.count(True) == 1
        grokmirror.unlock_repo(target)
        assert target not in grokmirror.REPO_LOCKH


class TestLockedManifest:
    """locked_manifest() must release the lock however the block exits."""

    def test_releases_on_success(self, tmp_path: Path) -> None:
        manifile = str(tmp_path / 'manifest.js.gz')
        with grokmirror.locked_manifest(manifile):
            assert grokmirror.MANIFEST_LOCKH is not None
        assert grokmirror.MANIFEST_LOCKH is None

    def test_releases_on_exception(self, tmp_path: Path) -> None:
        # grok-fsck used to keep the manifest locked when it bailed out on an
        # unparseable status file: the early return skipped the manual
        # manifest_unlock() call.
        manifile = str(tmp_path / 'manifest.js.gz')
        with pytest.raises(grokmirror.GrokError, match='boom'), grokmirror.locked_manifest(manifile):
            raise grokmirror.GrokError('boom')
        assert grokmirror.MANIFEST_LOCKH is None


def fake_bare_repo(path: Path, altrepo: Path | None = None) -> Path:
    """The bare minimum that is_bare_git_repo() accepts, no git needed."""
    (path / 'objects' / 'info').mkdir(parents=True)
    (path / 'refs').mkdir()
    (path / 'HEAD').write_text('ref: refs/heads/master\n', encoding='utf-8')
    if altrepo is not None:
        (path / 'objects' / 'info' / 'alternates').write_text(f'{altrepo}/objects\n', encoding='utf-8')
    return path


class TestGrokSession:
    """GrokSession carries the per-run state that used to be module globals."""

    def test_requests_session_is_memoized(self) -> None:
        ses = grokmirror.GrokSession()
        first = ses.get_requests_session()
        assert first.headers['User-Agent'] == f'grokmirror/{grokmirror.VERSION}'
        assert ses.get_requests_session() is first

    def test_close_forgets_the_session(self) -> None:
        # grok-pull closes the HTTP session after fetching the remote
        # manifest. With the old module global the *closed* session stayed
        # memoized, so any later caller would get a dead object.
        ses = grokmirror.GrokSession()
        first = ses.get_requests_session()
        ses.close_requests_session()
        assert ses.get_requests_session() is not first

    def test_close_without_open_is_fine(self) -> None:
        grokmirror.GrokSession().close_requests_session()

    def test_altrepo_map_and_is_alt_repo(self, tmp_path: Path) -> None:
        tmp_path = tmp_path.resolve()
        shared = fake_bare_repo(tmp_path / 'objstore' / 'shared.git')
        child = fake_bare_repo(tmp_path / 'toplevel' / 'child.git', altrepo=shared)
        fake_bare_repo(tmp_path / 'toplevel' / 'loner.git')

        ses = grokmirror.GrokSession()
        assert ses.get_altrepo_map(str(tmp_path)) == {str(shared): {str(child)}}
        assert ses.is_alt_repo(str(tmp_path), 'objstore/shared.git')
        assert not ses.is_alt_repo(str(tmp_path), 'toplevel/loner.git')

    def test_altrepo_map_is_cached_per_toplevel(self, tmp_path: Path) -> None:
        # The old module-level cache ignored which toplevel it was built
        # from: whoever asked first won, and every later caller got that
        # same answer regardless of the path they passed.
        tmp_path = tmp_path.resolve()
        shared = fake_bare_repo(tmp_path / 'shared.git')
        fake_bare_repo(tmp_path / 'a' / 'child.git', altrepo=shared)
        (tmp_path / 'b').mkdir()

        ses = grokmirror.GrokSession()
        assert ses.get_altrepo_map(str(tmp_path / 'a'))
        assert not ses.get_altrepo_map(str(tmp_path / 'b'))

    def test_refresh_rescans(self, tmp_path: Path) -> None:
        tmp_path = tmp_path.resolve()
        shared = fake_bare_repo(tmp_path / 'shared.git')
        toplevel = tmp_path / 'toplevel'
        toplevel.mkdir()

        ses = grokmirror.GrokSession()
        assert not ses.get_altrepo_map(str(toplevel))
        child = fake_bare_repo(toplevel / 'child.git', altrepo=shared)
        # The cached answer stays stale until a refresh is asked for
        assert not ses.get_altrepo_map(str(toplevel))
        assert ses.get_altrepo_map(str(toplevel), refresh=True) == {str(shared): {str(child)}}

    def test_find_all_gitdirs_primes_the_altrepo_map(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tmp_path = tmp_path.resolve()
        shared = fake_bare_repo(tmp_path / 'shared.git')
        child = fake_bare_repo(tmp_path / 'toplevel' / 'child.git', altrepo=shared)

        ses = grokmirror.GrokSession()
        assert ses.find_all_gitdirs(str(tmp_path / 'toplevel')) == {str(child)}

        # The walk doubles as the alternates scan, so asking for the map now
        # must not kick off a second sweep of the tree.
        def no_glob(_self: pathlib.Path, _pattern: str) -> None:
            raise AssertionError('the cache was primed, nothing should be rescanning')

        monkeypatch.setattr(pathlib.Path, 'glob', no_glob)
        assert ses.get_altrepo_map(str(tmp_path / 'toplevel')) == {str(shared): {str(child)}}

    def test_survives_pickling(self, tmp_path: Path) -> None:
        # Workers receive the session through mp.Process args, which pickles
        # it under the forkserver and spawn start methods (forkserver is the
        # Linux default since Python 3.14). The live HTTP session must be
        # dropped; the alternates caches must come along.
        tmp_path = tmp_path.resolve()
        shared = fake_bare_repo(tmp_path / 'shared.git')
        child = fake_bare_repo(tmp_path / 'toplevel' / 'child.git', altrepo=shared)

        ses = grokmirror.GrokSession()
        ses.get_requests_session()
        ses.get_altrepo_map(str(tmp_path / 'toplevel'))

        clone = pickle.loads(pickle.dumps(ses))
        assert clone._requests is None
        assert clone.get_altrepo_map(str(tmp_path / 'toplevel')) == {str(shared): {str(child)}}


class TestMergeSiblings:
    """merge_siblings() folds objstore repos into whichever has most children."""

    def test_all_siblings_orphaned(self) -> None:
        # mdest stayed None and siblings.remove(None) raised KeyError.
        siblings = {'/objstore/one.git', '/objstore/two.git'}
        amap: dict[str, set[str]] = {'/objstore/one.git': set(), '/objstore/two.git': set()}
        assert grokmirror.fsck.merge_siblings(siblings, amap) is None

    def test_siblings_missing_from_the_map(self) -> None:
        siblings = {'/objstore/one.git'}
        assert grokmirror.fsck.merge_siblings(siblings, {}) is None

    def test_no_siblings_at_all(self) -> None:
        assert grokmirror.fsck.merge_siblings(set(), {}) is None


class TestRepackLevel:
    """get_repack_level() decides between no repack (0), quick (1) and full (2)."""

    @staticmethod
    def obj_info(**overrides: int) -> dict[str, str]:
        """Build a `git count-objects -v` result, as run_git_command reports it.

        Keys are spelled as git spells them, so they are passed as
        obj_info(**{'in-pack': 600}) rather than as identifiers.
        """
        info = {'count': 0, 'size': 0, 'in-pack': 0, 'packs': 0, 'size-pack': 0}
        unknown = set(overrides) - set(info)
        assert not unknown, f'not a count-objects key: {sorted(unknown)}'
        info.update(overrides)
        return {key: str(value) for key, value in info.items()}

    def test_tidy_repo_needs_nothing(self) -> None:
        info = self.obj_info(**{'in-pack': 1000, 'size-pack': 4096, 'packs': 1})
        assert grokmirror.get_repack_level(info) == 0

    def test_too_many_packs_means_full_repack(self) -> None:
        assert grokmirror.get_repack_level(self.obj_info(packs=20)) == 2

    def test_too_many_loose_objects_means_quick_repack(self) -> None:
        assert grokmirror.get_repack_level(self.obj_info(count=1200, packs=1)) == 1

    def test_thresholds_are_configurable(self) -> None:
        info = self.obj_info(count=50, packs=3)
        assert grokmirror.get_repack_level(info) == 0
        assert grokmirror.get_repack_level(info, max_loose_objects=50) == 1
        assert grokmirror.get_repack_level(info, max_packs=3) == 2

    def test_loose_objects_as_a_share_of_the_total(self) -> None:
        # Over 500 objects in total and more than 10% of them loose.
        assert grokmirror.get_repack_level(self.obj_info(count=100, packs=1, **{'in-pack': 600})) == 1
        assert grokmirror.get_repack_level(self.obj_info(count=10, packs=1, **{'in-pack': 600})) == 0

    def test_loose_size_as_a_share_of_the_total(self) -> None:
        # Over 1KiB in total and more than 10% of it loose.
        assert grokmirror.get_repack_level(self.obj_info(size=200, packs=1, **{'size-pack': 1000})) == 1
        assert grokmirror.get_repack_level(self.obj_info(size=10, packs=1, **{'size-pack': 1000})) == 0

    def test_tiny_repos_are_left_alone(self) -> None:
        # All loose, but too small to be worth repacking repeatedly.
        assert grokmirror.get_repack_level(self.obj_info(count=100)) == 0


class TestIsObstrepo:
    """is_obstrepo(path, obstdir) asks whether a path is inside the objstore."""

    def test_repo_inside_obstdir(self, tmp_path: Path) -> None:
        obstdir = str(tmp_path / 'objstore')
        assert grokmirror.is_obstrepo(str(tmp_path / 'objstore' / 'x.git'), obstdir)

    def test_sibling_sharing_a_name_prefix_is_not_inside(self, tmp_path: Path) -> None:
        # A string-prefix check said /srv/objstore-private/x.git was inside
        # /srv/objstore. Directory containment is not string containment.
        obstdir = str(tmp_path / 'objstore')
        assert not grokmirror.is_obstrepo(str(tmp_path / 'objstore-private' / 'x.git'), obstdir)


class TestFsckErrorClassification:
    """Blank patterns must not classify anything.

    Splitting an empty config value on newlines yields [''], and every
    string "contains" the empty string, so an unset ignore_errors used to
    swallow every error into the debug log, and an unset reclone_on_errors
    used to request a reclone for any error at all. The two masked each
    other just well enough to go unnoticed.
    """

    @staticmethod
    def _config(toplevel: Path) -> grokmirror.GrokConfigParser:
        config = grokmirror.GrokConfigParser(interpolation=ExtendedInterpolation())
        config.read_dict({'core': {'toplevel': str(toplevel)}, 'fsck': {}})
        return config

    def test_empty_ignore_errors_ignores_nothing(self, tmp_path: Path) -> None:
        config = self._config(tmp_path)
        warn = grokmirror.fsck.remove_ignored_errors('error: it went badly', config)
        assert warn == ['error: it went badly']

    def test_configured_ignore_pattern_still_matches_substring(self, tmp_path: Path) -> None:
        config = self._config(tmp_path)
        config['fsck']['ignore_errors'] = 'went badly\ndangling commit'
        warn = grokmirror.fsck.remove_ignored_errors('error: it went badly\nerror: novel problem', config)
        assert warn == ['error: novel problem']

    def test_empty_reclone_on_errors_reclones_nothing(self, tmp_path: Path) -> None:
        repo = tmp_path / 'mirror' / 'test.git'
        repo.mkdir(parents=True)
        config = self._config(tmp_path / 'mirror')
        ses = grokmirror.GrokSession()
        grokmirror.fsck.check_reclone_error(ses, str(repo), config, ['fatal: any error at all'])
        assert not (repo / 'grokmirror.reclone').exists()

    def test_configured_reclone_pattern_triggers_reclone(self, tmp_path: Path) -> None:
        repo = tmp_path / 'mirror' / 'test.git'
        repo.mkdir(parents=True)
        config = self._config(tmp_path / 'mirror')
        config['fsck']['reclone_on_errors'] = 'fatal: bad tree object'
        ses = grokmirror.GrokSession()
        grokmirror.fsck.check_reclone_error(ses, str(repo), config, ['fatal: bad tree object deadbeef'])
        assert (repo / 'grokmirror.reclone').exists()


class TestRunShellCommand:
    """run_shell_command() is the choke point for every external command.

    Every git invocation and every hook goes through here, so the environment
    and stdin contracts below are load-bearing for the whole tree.
    """

    def test_inherits_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Commands must inherit the caller's environment: the test harness
        # points git at its own config purely through environment variables,
        # and mirror operators expect proxy and ssh settings to apply. (A
        # 2021 refactor accidentally ran every command with an empty
        # environment instead; it never shipped in a release.)
        monkeypatch.setenv('GROK_TEST_CANARY', 'chirp')
        ecode, out, _err = grokmirror.run_shell_command(['sh', '-c', 'printf %s "${GROK_TEST_CANARY-unset}"'])
        assert ecode == 0
        assert out == 'chirp'

    def test_explicit_env_replaces_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('GROK_TEST_CANARY', 'chirp')
        ecode, out, _err = grokmirror.run_shell_command(
            ['sh', '-c', 'printf %s "${GROK_TEST_CANARY-unset}:${PI_CONFIG-unset}"'], env={'PI_CONFIG': '/pi'}
        )
        assert ecode == 0
        assert out == 'unset:/pi'

    def test_timeout_kills_the_command(self) -> None:
        # 124 is what timeout(1) exits with, so it reads familiarly in logs.
        ecode, _out, _err = grokmirror.run_shell_command(['sleep', '30'], timeout=0.5)
        assert ecode == 124

    def test_no_stdin_means_eof_not_the_callers_terminal(self) -> None:
        # A hook that reads stdin must see EOF immediately, not hang waiting
        # on whatever stdin the daemon happens to have inherited.
        ecode, out, _err = grokmirror.run_shell_command(['cat'])
        assert ecode == 0
        assert out == ''

    def test_stdin_bytes_are_delivered(self) -> None:
        ecode, out, _err = grokmirror.run_shell_command(['cat'], stdin=b'hello there')
        assert ecode == 0
        assert out == 'hello there'


class TestReadManifest:
    """read_manifest() has to cope with gzipped, plain, missing and broken files."""

    def test_gzipped(self, tmp_path: Path) -> None:
        manifile = tmp_path / 'manifest.js.gz'
        with gzip.open(manifile, 'wb') as fh:
            fh.write(json.dumps({'/test/one.git': {'modified': 5}}).encode())
        assert grokmirror.read_manifest(str(manifile))['/test/one.git']['modified'] == 5

    def test_plain(self, tmp_path: Path) -> None:
        manifile = tmp_path / 'manifest.js'
        manifile.write_text('{"/test/one.git": {"modified": 5}}')
        assert grokmirror.read_manifest(str(manifile))['/test/one.git']['modified'] == 5

    def test_gz_in_the_directory_name_does_not_mean_gzip(self, tmp_path: Path) -> None:
        # The opener used to be picked by looking for '.gz' anywhere in the
        # path, so a plain-text manifest under /srv/my.gz-mirrors/ was fed
        # to gzip and blew up.
        mdir = tmp_path / 'my.gz-mirrors'
        mdir.mkdir()
        manifile = mdir / 'manifest.js'
        manifile.write_text('{"/test/one.git": {"modified": 5}}')
        assert grokmirror.read_manifest(str(manifile))['/test/one.git']['modified'] == 5

    def test_missing_is_an_empty_manifest(self, tmp_path: Path) -> None:
        # A mirror's first run: no manifest yet, and that is not an error.
        assert grokmirror.read_manifest(str(tmp_path / 'nope.js.gz')) == {}

    def test_unparseable_is_an_empty_manifest(self, tmp_path: Path) -> None:
        # Better to regenerate than to refuse to run: a truncated manifest is
        # what a mirror finds after the disk filled up mid-write.
        manifile = tmp_path / 'manifest.js'
        manifile.write_text('{"/test/one.git": {"modif')
        assert grokmirror.read_manifest(str(manifile)) == {}

    def test_write_then_read_round_trip(self, tmp_path: Path) -> None:
        manifile = str(tmp_path / 'manifest.js.gz')
        manifest = {'/test/one.git': {'modified': 5, 'fingerprint': 'abc', 'symlinks': ['/test/link.git']}}
        grokmirror.write_manifest(manifile, manifest)
        assert grokmirror.read_manifest(manifile) == manifest

    def test_write_with_an_explicit_mtime(self, tmp_path: Path) -> None:
        manifile = str(tmp_path / 'manifest.js.gz')
        grokmirror.write_manifest(manifile, {'/test/one.git': {'modified': 5}}, mtime=1600000000)
        assert int(os.stat(manifile).st_mtime) == 1600000000

    def test_pretty_manifest_is_still_readable(self, tmp_path: Path) -> None:
        manifile = str(tmp_path / 'manifest.js')
        manifest = {'/test/two.git': {'modified': 2}, '/test/one.git': {'modified': 1}}
        grokmirror.write_manifest(manifile, manifest, pretty=True)
        assert grokmirror.read_manifest(manifile) == manifest
        # Pretty means sorted and indented, which is the point of the option.
        text = Path(manifile).read_text(encoding='utf-8')
        assert text.index('/test/one.git') < text.index('/test/two.git')
        assert '\n ' in text
