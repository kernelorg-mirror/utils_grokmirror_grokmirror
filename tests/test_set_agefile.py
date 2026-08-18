# SPDX-License-Identifier: GPL-3.0-or-later
"""set_agefile(), which writes the two on-disk timestamp records grok-pull
leaves behind after every successful fetch: grokmirror.timestamp (its own
bookkeeping) and info/web/last-modified (cgit's idle-time hint). Neither file
exists until the first pull, and info/web doesn't either, so the directory
has to be created along the way. Had no coverage at any level.
"""

from __future__ import annotations

import time

import grokmirror.pull

from support import GrokTree


def test_writes_timestamp_and_cgit_formatted_agefile(tree: GrokTree) -> None:
    fullpath = tree.add_empty_repo('test/one.git')
    last_modified = 1600000000

    grokmirror.pull.set_agefile(str(tree.toplevel), '/test/one.git', last_modified)

    tsfile = fullpath / 'grokmirror.timestamp'
    agefile = fullpath / 'info' / 'web' / 'last-modified'
    assert tsfile.read_text(encoding='utf-8') == str(last_modified)
    expected_cgit = time.strftime('%F %T', time.localtime(last_modified))
    assert agefile.read_text(encoding='utf-8') == f'{expected_cgit}\n'


def test_second_call_overwrites_both_files_without_error(tree: GrokTree) -> None:
    fullpath = tree.add_empty_repo('test/one.git')

    grokmirror.pull.set_agefile(str(tree.toplevel), '/test/one.git', 1600000000)
    grokmirror.pull.set_agefile(str(tree.toplevel), '/test/one.git', 1700000000)

    tsfile = fullpath / 'grokmirror.timestamp'
    agefile = fullpath / 'info' / 'web' / 'last-modified'
    assert tsfile.read_text(encoding='utf-8') == '1700000000'
    expected_cgit = time.strftime('%F %T', time.localtime(1700000000))
    assert agefile.read_text(encoding='utf-8') == f'{expected_cgit}\n'
