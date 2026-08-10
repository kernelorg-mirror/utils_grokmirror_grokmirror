#!/usr/bin/env sh

set -eu

# Fast checks, meant to be run before every commit. The interpreter sweep lives
# in ci-matrix.sh, which is slow and only needed before releases.
#
# Ratchet status: grokmirror predates type annotations, so the type checkers are
# not yet at the `strict` used elsewhere. mypy is clean with check_untyped_defs
# (so it checks function bodies, not just annotated signatures) and ty is clean
# at its default rules; both should stay that way. pyright still runs at
# `standard` and is the remaining backlog. Tighten in pyproject.toml as modules
# gain annotations; the goal is `mypy --strict` / pyright `strict` /
# ty `all = "error"`.

uv run ruff format --check
uv run ruff check
uv run ty check
uv run mypy .
uv run pyright
uv run pytest --durations=0
