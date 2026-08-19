============
Contributing
============

Contributions to grokmirror are welcome. This page covers how to get set
up and how to send changes.

Development Setup
=================

Grokmirror uses `uv <https://docs.astral.sh/uv/>`_ for development:

.. code-block:: bash

   git clone https://git.kernel.org/pub/scm/utils/grokmirror/grokmirror.git
   cd grokmirror
   uv sync --all-groups

Verify the checkout:

.. code-block:: bash

   uv run grok-pull --version

Running the Checks
==================

``ci.sh`` runs everything that should be green before a commit -- the
formatter, the linter, three type checkers, and the test suite:

.. code-block:: bash

   ./ci.sh

To run only the tests:

.. code-block:: bash

   uv run pytest

The test suite drives real ``git`` commands against throwaway repository
trees, so nothing about git is mocked. It takes a couple of minutes; the
slowest tests are marked and can be skipped:

.. code-block:: bash

   uv run pytest -m 'not slow'

``ci-matrix.sh`` repeats the run against every supported interpreter, from
Python 3.9 up. It is slow and only needed before a release.

Code Style
==========

* ``ruff format`` and ``ruff check`` decide formatting and style; both run
  in ``ci.sh``. Settings live in ``pyproject.toml``.
* New and modified functions need type annotations. The type checkers are
  ratcheting toward strict mode, so prefer a real annotation over a
  suppression comment.
* Grokmirror supports Python 3.9, which rules out newer syntax. The
  ``from __future__ import annotations`` import at the top of each module
  covers the annotation syntax specifically.

Tests
=====

Tests are expected with behavioral changes. The suite is built around a
throwaway installation fixture that creates real bare repositories with
reproducible history, so a test usually reads as a small setup followed by
running an actual ``grok-*`` command.

Two conventions are worth knowing before writing one:

* Every test runs with the current directory inside an unrelated decoy
  repository. Several past bugs were git commands invoked without a
  repository path, which git happily answers from the current directory
  rather than failing.
* A test fails on any traceback in stderr and on a hang, not just on a bad
  exit code. Failures should be loud.

Submitting Changes
==================

Grokmirror uses an email-based workflow. Send patches to:

   tools@kernel.org

Save yourself trouble and use b4_ to prepare and send them.

.. _b4: https://b4.docs.kernel.org/

Commits need a ``Signed-off-by`` line certifying the Developer Certificate
of Origin -- ``git commit --signoff`` adds it.

Reporting Bugs
==============

Mail tools@kernel.org.

A useful report includes the grokmirror version, the git version, the
relevant part of the config, and the log with ``loglevel = debug``.
