# SPDX-License-Identifier: GPL-3.0-or-later
"""purge_stale_repos(), grok-pull's purge-protection gate.

test_integration.py already exercises purge-disabled-by-default, the ffonly
and nopurge protections, and purgeprotect refusing a large deletion. What's
missing is the malformed-config branch: an out-of-range purgeprotect value
must fall back to the documented default of 5 rather than being used as-is,
since a value like '150' would otherwise silently defeat the protection.
"""

from __future__ import annotations

import queue

import pytest

import grokmirror
import grokmirror.pull

from support import GrokTree


def test_an_invalid_purgeprotect_value_falls_back_to_the_default(
    tree: GrokTree, caplog: pytest.LogCaptureFixture
) -> None:
    for n in range(5):
        tree.add_repo(f'test/repo{n}.git')
    # Only 4 of the 5 are still in the remote manifest, so one is up for
    # purging -- 20%, comfortably past the documented default of 5%. A raw,
    # unsanitized '150' would never refuse anything, since no percentage can
    # reach 150.
    r_culled: grokmirror.Manifest = {f'/test/repo{n}.git': {} for n in range(4)}
    config = grokmirror.load_config_file(
        str(tree.write_config(sections={'pull': {'purge': 'yes', 'purgeprotect': '150'}}))
    )
    ses = grokmirror.GrokSession()
    q_mani: queue.Queue[grokmirror.pull.ManiItem] = queue.Queue()

    with caplog.at_level('CRITICAL'):
        grokmirror.pull.purge_stale_repos(ses, config, str(tree.toplevel), r_culled, set(), False, q_mani)

    assert 'not valid for purgeprotect' in caplog.text
    assert 'Defaulting to purgeprotect=5' in caplog.text
    assert 'Refusing to purge' in caplog.text
    assert q_mani.empty()
