# SPDX-License-Identifier: GPL-3.0-or-later
"""Options set in the config file must actually take effect.

ConfigParser has no schema, so an option the code never reads -- or reads
under a different name, or reads two different ways in two different places --
fails completely silently: the mirror runs, and simply does not do what the
admin wrote down. Both cases below were exactly that, found by enumerating
every config read in the tree and comparing it against grokmirror.conf.
"""

from __future__ import annotations

from support import GrokTree


def test_manifest_honours_core_log(tree: GrokTree) -> None:
    # grok-manifest looked for [core] logfile, which nothing writes and
    # grokmirror.conf has never documented; every other command reads
    # [core] log. Configuring grok-manifest from a config file therefore
    # logged nowhere at all.
    tree.add_repo('test/one.git')
    tree.write_config()

    tree.run('grok-manifest', '--cfgfile', str(tree.cfgfile))

    assert tree.logfile.exists(), 'grok-manifest wrote no log file at all'
    assert 'test/one.git' in tree.log_text()
