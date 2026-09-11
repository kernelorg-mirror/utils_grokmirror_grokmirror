# Copyright (C) 2013-2020 by The Linux Foundation and contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Publish shallow single-branch repositories as tarballs, for CI systems.

A CI node untars one of these, runs "git remote update", and checks out the
sha it wants. The point of shipping it pre-made is that the tarball comes off
a CDN instead of out of git-upload-pack, and that the clone inside it is
single-branch, so the node's subsequent fetch asks the origin about exactly
one ref.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import grokmirror

logger = logging.getLogger(__name__)

SECONDS_IN_DAY = 86400


class Branch(NamedTuple):
    """One branch selected for publication, and the name it publishes under."""

    name: str
    """The branch as git knows it, e.g. "for-next/core"."""

    slug: str
    """The branch as it appears in a filename, e.g. "for-next-core"."""

    tip: str
    """Full object name of the branch tip.

    A depth-1 single-branch tarball is entirely determined by this, which is
    why the abbreviation of it goes in the published filename: the name does
    not merely identify the file, it names the commit inside it.
    """


def slug_branch(name: str) -> str:
    """Turn a branch name into something that is one path component.

    Only "/" needs doing, and that is worth writing down because it looks
    like it should need more. git refuses to create a branch whose name
    contains a space, "~", "^", "?", "*", "[", "\\", an ASCII control
    character, "..", a leading or trailing dot, or a trailing ".lock" -- so
    git-check-ref-format has already rejected everything that would be
    hostile in a filename, and adding our own scrubbing on top would only
    give a later reader the impression that it was load-bearing.

    Three things are legal in a branch name and survive into the filename,
    all of them harmless here:

    - A leading "-" ("-dashlead" is a valid branch). The branch is always the
      middle component of the published name, so the filename still starts
      with the repository and never looks like an option to a CLI tool.
    - Non-ASCII ("uni-café" is valid). Fine on disk; a client fetching it
      over HTTP percent-encodes, which is the client's business.
    - Case. "Master" and "master" are different branches, and different files
      on the filesystems this runs on.

    Slugging is not injective, so whoever holds the resulting list has to
    check it for duplicates -- see check_collisions().
    """
    return name.replace('/', '-')


def check_collisions(branches: list[Branch]) -> list[Branch]:
    """Drop any branches that would publish under the same filename.

    "for-next/core" and a branch literally named "for-next-core" slug to the
    same thing, as do "Master" and "master" if this ever runs on a
    case-insensitive filesystem. Publishing one branch's tree under a name
    another branch also claims is the silently-wrong-content failure this
    whole tool is meant to avoid, so a collision drops *every* branch
    involved rather than letting the last one written win. Dropping one
    arbitrary side would leave a published tarball whose contents depend on
    dictionary ordering, which is the worse outcome: it looks like success.
    """
    by_slug: dict[str, list[Branch]] = {}
    for branch in branches:
        by_slug.setdefault(branch.slug, []).append(branch)

    keep = []
    for slug, found in by_slug.items():
        if len(found) > 1:
            names = ', '.join(sorted(b.name for b in found))
            logger.critical('  collision: %s all publish as "%s", skipping all of them', names, slug)
            continue
        keep.extend(found)
    return keep


def select_branches(
    fullpath: grokmirror.StrPath,
    pattern: str,
    now: int,
    maxrefage: int = 0,
    maxbranches: int = 0,
) -> list[Branch]:
    """Pick the branches of one repository to publish tarballs for.

    The pattern is a shell glob matched against the short branch name, so
    "linux-*.y" picks up the stable branches. A glob rather than a list
    because stable branches are born and retired constantly and a
    hand-maintained list would rot; maxrefage then retires the branches
    nobody maintains any more, since an end-of-life stable branch simply
    stops receiving commits. A maxrefage of 0 keeps every branch, and a
    maxbranches of 0 publishes as many as match.

    The cap is applied before the collision check on purpose: a branch that
    did not make the cut is not being published, so it cannot collide with
    anything.

    Returns the survivors sorted by name, so a run's output does not depend
    on the order git happened to list refs in.
    """
    cutoff = now - maxrefage * SECONDS_IN_DAY if maxrefage > 0 else None
    ecode, out, err = grokmirror.run_git_command(
        fullpath, ['for-each-ref', '--format=%(committerdate:unix) %(objectname) %(refname:short)', 'refs/heads']
    )
    if ecode > 0:
        logger.info('  could not list branches in %s: %s', fullpath, err.strip())
        return []

    matches = grokmirror.compile_globs([pattern])
    dated: list[tuple[int, Branch]] = []
    for line in out.splitlines():
        stamp, _sep, rest = line.partition(' ')
        oid, _sep, name = rest.partition(' ')
        # A branch always points at a commit, so a line without a usable date
        # is something we do not understand and should not be guessing about.
        if not (stamp.isdigit() and oid and name):
            continue
        if not matches.match(name):
            continue
        if cutoff is not None and int(stamp) < cutoff:
            logger.debug('%s: %s is older than %s days, skipping', fullpath, name, maxrefage)
            continue
        dated.append((int(stamp), Branch(name=name, slug=slug_branch(name), tip=oid)))

    if maxbranches > 0 and len(dated) > maxbranches:
        # Newest first, so the cap keeps the branches still being worked on.
        # Ties broken by name so the choice is at least deterministic.
        dated.sort(key=lambda item: (-item[0], item[1].name))
        dropped = [branch.name for _stamp, branch in dated[maxbranches:]]
        logger.info('  capped: %s at %s branches, dropping %s', fullpath, maxbranches, ', '.join(dropped))
        dated = dated[:maxbranches]

    return sorted(check_collisions([branch for _stamp, branch in dated]), key=lambda b: b.name)
