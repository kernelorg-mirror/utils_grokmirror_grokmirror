# SPDX-License-Identifier: GPL-3.0-or-later
"""Import and CLI smoke tests.

grokmirror is almost entirely I/O against real git repositories, so these
tests deliberately stay shallow: they only assert that every module imports
and every console script can build its argument parser and print help. That
is enough to catch syntax errors, bad imports and argparse mistakes across
the whole interpreter matrix (see ci-matrix.sh), which is the main thing the
matrix sweep is for.
"""

import importlib
import subprocess
import sys

import pytest

MODULES = [
    'grokmirror',
    'grokmirror.bundle',
    'grokmirror.dumb_pull',
    'grokmirror.fsck',
    'grokmirror.manifest',
    'grokmirror.pi_indexer',
    'grokmirror.pi_piper',
    'grokmirror.pull',
]

SCRIPTS = [
    'grok-bundle',
    'grok-dumb-pull',
    'grok-fsck',
    'grok-manifest',
    'grok-pi-indexer',
    'grok-pi-piper',
    'grok-pull',
]


@pytest.mark.parametrize('modname', MODULES)
def test_module_imports(modname: str) -> None:
    assert importlib.import_module(modname) is not None


@pytest.mark.parametrize('modname', MODULES[1:])
def test_module_has_command_entry_point(modname: str) -> None:
    # Every console script in pyproject.toml points at a `command` callable.
    mod = importlib.import_module(modname)
    assert callable(mod.command)


def test_version_is_exported() -> None:
    import grokmirror

    assert grokmirror.VERSION


@pytest.mark.parametrize('script', SCRIPTS)
def test_script_help(script: str) -> None:
    # Invoke through the installed entry point rather than -m, since that is
    # how users actually reach these.
    res = subprocess.run([script, '--help'], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert 'usage:' in res.stdout


def test_python_version_is_supported() -> None:
    # Mirrors `requires-python` in pyproject.toml.
    assert sys.version_info >= (3, 9)
