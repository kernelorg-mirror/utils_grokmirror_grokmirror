#!/usr/bin/env sh

set -eu

# Fast checks, meant to be run before every commit. The interpreter sweep lives
# in ci-matrix.sh, which is slow and only needed before releases.
#
# Ratchet status: all of these are green and should stay that way. grokmirror
# predates type annotations, so the type checkers are not yet at the `strict`
# used elsewhere: mypy now requires annotations on every function
# (disallow_untyped_defs) but not yet the rest of `strict`, pyright runs at
# `standard`, and ty at its default rules. Keep tightening in pyproject.toml;
# the goal is `mypy --strict` / pyright `strict` / ty `all = "error"`. Prefer
# real annotations over suppressions when tightening: annotating a single
# parameter is what turned up several of the crashes fixed in this tree.

# --locked fails rather than re-resolving, so a pyproject.toml edit that was
# not followed by `uv lock` is caught here instead of silently giving this run
# a different set of tool versions than everyone else's. When it complains,
# run `uv lock` (and `uv export --no-dev --no-emit-project -o requirements.txt`
# if a runtime dependency changed) and commit both.
uv sync --all-groups --locked

uv run ruff format --check
uv run ruff check
uv run ty check
uv run mypy .
uv run pyright
uv run pytest --durations=0
