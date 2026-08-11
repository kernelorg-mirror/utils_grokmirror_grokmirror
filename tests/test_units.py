# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the parts of grokmirror that need no repository.

These are all cheap, so they cover the awkward inputs too: the sizes nobody has
on disk yet, the relative paths nobody passes on purpose, the empty strings that
turn up when a config option is present but blank. Several of the crashes fixed
in this tree were exactly that kind of input.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
from pathlib import Path

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
                'except BlockingIOError:\n    sys.exit(7)\n'
                'sys.exit(0)\n'
            )
            res = subprocess.run(['python3', '-c', code], capture_output=True, text=True, check=False)
            assert res.returncode == 7, res.stderr
        finally:
            grokmirror.unlock_repo(target)


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
