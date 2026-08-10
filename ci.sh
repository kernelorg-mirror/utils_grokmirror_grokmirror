#!/usr/bin/env sh

set -eu

# Fast checks, meant to be run before every commit. The interpreter sweep lives
# in ci-matrix.sh, which is slow and only needed before releases.
#
# Ratchet status: grokmirror predates type annotations, so mypy and pyright run
# at their default strictness rather than the `strict` used elsewhere, and
# `[tool.ty.rules]` is empty. Tighten these in pyproject.toml as modules gain
# annotations; the goal is `mypy --strict` / pyright `strict` / ty `all = "error"`.

uv run ruff format --check
uv run ruff check
uv run ty check
uv run mypy .
uv run pyright
uv run pytest --durations=0
