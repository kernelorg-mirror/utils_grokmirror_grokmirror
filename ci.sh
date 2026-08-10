#!/usr/bin/env sh

set -eu

# Fast checks, meant to be run before every commit. The interpreter sweep lives
# in ci-matrix.sh, which is slow and only needed before releases.
#
# Ratchet status: all of these are green and should stay that way. grokmirror
# predates type annotations, so the type checkers are not yet at the `strict`
# used elsewhere: mypy runs with check_untyped_defs (so it checks function
# bodies, not just annotated signatures), pyright at `standard`, and ty at its
# default rules. Tighten in pyproject.toml as modules gain annotations; the goal
# is `mypy --strict` / pyright `strict` / ty `all = "error"`. Prefer real
# annotations over suppressions when tightening: annotating a single parameter
# is what turned up the last two crashes fixed in this tree.

uv run ruff format --check
uv run ruff check
uv run ty check
uv run mypy .
uv run pyright
uv run pytest --durations=0
