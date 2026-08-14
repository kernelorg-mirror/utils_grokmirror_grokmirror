# SPDX-License-Identifier: GPL-3.0-or-later
"""sync_repo_symlinks(), which keeps a repo's configured symlinks pointed at it.

pull_worker() already exercises three of this function's branches indirectly
(creating a missing symlink, fixing one that points elsewhere, and replacing a
stale real directory), in test_pull_workers.py. What's missing is direct
coverage of the no-op case -- a correct symlink must be left alone, not
unlinked and recreated -- plus missing parent directories and more than one
symlink handled in a single call, which are easiest to check by calling the
function directly instead of through the full pull_worker() machinery.
"""

from __future__ import annotations

import os

import grokmirror.pull

from support import GrokTree


def test_sync_repo_symlinks_does_not_touch_an_already_correct_symlink(tree: GrokTree) -> None:
    fullpath = tree.add_repo('test/one.git')
    linkpath = tree.add_symlink('test/link.git', 'test/one.git')
    inode_before = os.lstat(linkpath).st_ino

    grokmirror.pull.sync_repo_symlinks(str(tree.toplevel), '/test/one.git', fullpath, ['/test/link.git'])

    assert os.lstat(linkpath).st_ino == inode_before
    assert os.path.realpath(linkpath) == os.fspath(fullpath)


def test_sync_repo_symlinks_creates_missing_parent_directories(tree: GrokTree) -> None:
    fullpath = tree.add_repo('test/one.git')
    linkpath = tree.path('deeply/nested/link.git')
    assert not linkpath.parent.exists()

    grokmirror.pull.sync_repo_symlinks(str(tree.toplevel), '/test/one.git', fullpath, ['/deeply/nested/link.git'])

    assert linkpath.is_symlink()
    assert os.path.realpath(linkpath) == os.fspath(fullpath)


def test_sync_repo_symlinks_handles_several_symlinks_independently_in_one_call(tree: GrokTree) -> None:
    fullpath = tree.add_repo('test/one.git')
    other = tree.add_repo('test/other.git')
    correct = tree.add_symlink('test/correct.git', 'test/one.git')
    inode_before = os.lstat(correct).st_ino
    wrong = tree.add_symlink('test/wrong.git', 'test/other.git')
    missing = tree.path('test/missing.git')

    grokmirror.pull.sync_repo_symlinks(
        str(tree.toplevel), '/test/one.git', fullpath, ['/test/correct.git', '/test/wrong.git', '/test/missing.git']
    )

    assert os.lstat(correct).st_ino == inode_before
    assert os.path.realpath(wrong) == os.fspath(fullpath)
    assert os.path.realpath(missing) == os.fspath(fullpath)
    # Untouched sibling, just to be sure fixing 'wrong' didn't drag it along.
    assert other.is_dir() and not other.is_symlink()
