# SPDX-License-Identifier: GPL-3.0-or-later
"""Options set in the config file must actually take effect.

ConfigParser has no schema, so an option the code never reads -- or reads
under a different name, or reads two different ways in two different places --
fails completely silently: the mirror runs, and simply does not do what the
admin wrote down. Both cases below were exactly that, found by enumerating
every config read in the tree and comparing it against grokmirror.conf.
"""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize('value', ['yes', 'true', 'on', '1'])
def test_fsck_commitgraph_accepts_every_true_spelling(tree: GrokTree, value: str) -> None:
    # commitgraph was read with .getboolean() in one place and compared
    # against the literal 'yes' in two others, so "commitgraph = true" meant
    # enabled in the first and disabled in the rest: no graph was ever
    # written, and the code that verifies the graph then treated its absence
    # as the correct end state.
    repo = tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'commitgraph': value}})

    # A full repack is what reaches the graph-writing path; a small repo left
    # alone is never repacked and would never write a graph either way.
    tree.run_fsck('-f', '--repack-all-full')

    assert (repo / 'objects' / 'info' / 'commit-graph').exists()


@pytest.mark.parametrize('value', ['no', 'false', 'off', '0'])
def test_fsck_commitgraph_accepts_every_false_spelling(tree: GrokTree, value: str) -> None:
    repo = tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'commitgraph': value}})

    tree.run_fsck('-f', '--repack-all-full')

    assert not (repo / 'objects' / 'info' / 'commit-graph').exists()


@pytest.mark.parametrize('value', ['yes', 'true', 'on', '1'])
def test_fsck_prune_accepts_every_true_spelling(tree: GrokTree, value: str) -> None:
    # Same shape as commitgraph: prune was read as `!= 'yes'`, so every other
    # spelling of true quietly turned pruning off.
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'prune': value}})

    tree.run_fsck('-f', '--repack-all-full')

    assert 'Pruning disabled in config file' not in tree.log_text()


@pytest.mark.parametrize('value', ['no', 'false', 'off', '0'])
def test_fsck_prune_accepts_every_false_spelling(tree: GrokTree, value: str) -> None:
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'prune': value}})

    tree.run_fsck('-f', '--repack-all-full')

    assert 'Pruning disabled in config file' in tree.log_text()


def test_fsck_rejects_an_unparseable_boolean(tree: GrokTree) -> None:
    # An option that is neither true nor false is a typo, and the admin needs
    # to hear about it by name. This used to be silently false for prune, and
    # an uncaught ValueError deep inside the run for commitgraph.
    tree.add_repo('test/one.git')
    tree.run_manifest()
    tree.write_config({'fsck': {'prune': 'sometimes'}})

    res = tree.run_fsck('-f', '--repack-all-full', expect=1)

    out = res.stdout + res.stderr
    assert 'prune' in out
    assert 'sometimes' in out
