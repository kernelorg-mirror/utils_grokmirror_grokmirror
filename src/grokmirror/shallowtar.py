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

import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

import grokmirror

logger = logging.getLogger(__name__)

SECONDS_IN_DAY = 86400
# Long enough to stay unambiguous in a repository the size of linux.git, short
# enough to stay readable in a filename. This is what git itself abbreviates
# to by default in a tree that big.
TIP_ABBREV = 7
# The current tarball plus the previous one. See prune_tarballs().
KEEP_TARBALLS = 2


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


def clone_shallow(fullpath: Path, branch: Branch, destdir: Path, depth: int) -> bool:
    """Make the shallow single-branch clone that goes into the tarball.

    The URL is a file:// one, and that is not decoration. Given a plain path,
    "git clone --depth" warns and *silently ignores the depth*, taking the
    local-hardlink route instead -- so the mistake produces a working clone
    of the entire history, which is the worst kind of bug to have here: every
    functional test still passes and the mirror just quietly starts serving
    gigabytes. file:// forces a real transfer, which is also what makes this
    safe against grokmirror's objstore layout: upload-pack reads through
    objects/info/alternates, so the clone comes out self-contained with no
    alternates file of its own.

    --no-tags has to be on the clone itself, not only in the config afterwards.
    Tag auto-following happens during this fetch, so a tag sitting on the tip
    of the branch comes across with it and ships inside the tarball, and by the
    time prepare_clone() sets remote.origin.tagOpt the tag is already there.
    """
    ecode, _out, err = grokmirror.run_git_command(
        None,
        [
            'clone',
            '--quiet',
            f'--depth={depth}',
            '--single-branch',
            '--no-tags',
            f'--branch={branch.name}',
            # CI checks out the sha it wants, so a working tree here would
            # double the download for something thrown away on arrival.
            '--no-checkout',
            fullpath.resolve().as_uri(),
            str(destdir),
        ],
    )
    if ecode > 0:
        logger.info('  failed: %s %s (%s)', fullpath, branch.name, err.strip())
        return False
    return True


def verify_shallow(gitdir: Path, branch: Branch) -> bool:
    """Refuse to publish a clone that is not actually shallow.

    Belt to clone_shallow()'s braces, and worth the few lines: the failure it
    guards against does not announce itself. A full clone works perfectly,
    serves the right commit, and passes every test that asks whether the
    tarball is usable -- it is just enormous. The only way to notice is to
    ask directly.
    """
    if not (gitdir / 'shallow').exists():
        logger.critical('  refusing %s: the clone is not shallow, so --depth did not take', branch.name)
        return False
    ecode, out, _err = grokmirror.run_git_command(gitdir, ['symbolic-ref', '--quiet', 'HEAD'])
    if ecode > 0 or out.strip() != f'refs/heads/{branch.name}':
        # Without this a bare "git checkout" in CI lands on whatever the
        # client's init.defaultBranch happens to be, which is a confusing
        # failure a long way from here.
        logger.critical('  refusing %s: HEAD is not pointing at the branch', branch.name)
        return False
    return True


def prepare_clone(gitdir: Path, branch: Branch, cloneurl: str, now: int) -> bool:
    """Turn a fresh local clone into something ready to hand to a CI system."""
    settings = [
        # The file:// URL it was cloned from is a path on the generating
        # host. What goes out has to be the public URL CI will fetch from.
        ('remote.origin.url', cloneurl),
        # Set explicitly rather than left to "git clone --no-tags", which
        # happens to write the same key: this is the setting CI inherits, and
        # it is what keeps the *node's* first "git remote update" from
        # dragging in thousands of tags -- one of them pointing outside the
        # shallow boundary pulls its history across, which is exactly the
        # server load this tool exists to remove.
        ('remote.origin.tagOpt', '--no-tags'),
        # Stamped where a human can find it with one command, because a
        # tarball kept for months is the failure mode we cannot prevent, only
        # make diagnosable.
        ('grokmirror.shallowCreated', str(now)),
        ('grokmirror.shallowBranch', branch.name),
    ]
    for option, value in settings:
        ecode, _out, err = grokmirror.run_git_command(gitdir, ['config', option, value])
        if ecode > 0:
            logger.info('  failed: could not set %s (%s)', option, err.strip())
            return False

    # Every clone gets a copy of git's sample hooks. They are inert, but they
    # are also a couple of dozen files of noise in an artifact whose whole
    # point is to be the smallest useful thing.
    for sample in (gitdir / 'hooks').glob('*.sample'):
        sample.unlink()
    return True


def write_tarball(clonedir: Path, tarpath: Path, dirname: str) -> None:
    """Tar the prepared clone, reproducibly enough to be worth diffing.

    Entries are added in sorted order with ownership zeroed, so two runs over
    the same tip differ only in the mtimes git wrote. Uncompressed on
    purpose: with no working tree the payload is a packfile that is already
    deflated, so compressing it again is CPU spent on both ends for nothing.
    """

    def normalize(entry: tarfile.TarInfo) -> tarfile.TarInfo:
        entry.uid = entry.gid = 0
        entry.uname = entry.gname = 'root'
        return entry

    def add(path: Path, arcname: str) -> None:
        info = tar.gettarinfo(str(path), arcname)
        if path.is_dir():
            tar.addfile(normalize(info))
            for child in sorted(path.iterdir()):
                add(child, f'{arcname}/{child.name}')
        elif path.is_file():
            with path.open('rb') as fh:
                tar.addfile(normalize(info), fh)
        else:
            # Symlinks and anything else git left behind go in as-is.
            tar.addfile(normalize(info))

    tmppath = tarpath.with_name(f'.{tarpath.name}.tmp')
    try:
        with tarfile.open(tmppath, 'w', format=tarfile.PAX_FORMAT) as tar:
            add(clonedir, dirname)
    except BaseException:
        # A half-written tarball under a dot-name is invisible litter that
        # nothing ever comes back for, and these are hundreds of megabytes.
        tmppath.unlink(missing_ok=True)
        raise
    # Same directory, so the rename is atomic and a reader either sees the
    # whole tarball or no tarball. Publishing under the final name directly
    # would hand CI a truncated tarball for as long as the write takes.
    tmppath.replace(tarpath)


def generate_tarball(
    fullpath: Path,
    branch: Branch,
    cloneurl: str,
    tarpath: Path,
    dirname: str,
    now: int,
    depth: int = 1,
) -> bool:
    """Build one branch's tarball, leaving nothing behind if anything fails.

    The scratch clone is made inside the output directory rather than in
    /tmp, so the finished tarball can be renamed into place instead of copied
    across a filesystem boundary -- an atomic publish is the whole reason the
    fixed "latest" name is safe.
    """
    tarpath.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix='.shallowtar-', dir=tarpath.parent))
    try:
        clonedir = workdir / dirname
        if not clone_shallow(fullpath, branch, clonedir, depth):
            return False
        gitdir = clonedir / '.git'
        if not verify_shallow(gitdir, branch):
            return False
        if not prepare_clone(gitdir, branch, cloneurl, now):
            return False
        logger.info(' generate: %s', tarpath)
        try:
            write_tarball(clonedir, tarpath, dirname)
        except OSError as ex:
            # Usually a full disk, and these artifacts are large enough that
            # it is the failure to expect. One branch running out of room
            # should not take the rest of the run down with it.
            logger.critical('  refusing %s: could not write the tarball (%s)', branch.name, ex)
            return False
        return True
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class Artifact(NamedTuple):
    """Everything one (repository, branch) pair publishes, by name.

    Names are built once, here, so that the generator, the sidecar writer, the
    symlink mover and the pruner cannot drift apart in how they spell things.
    """

    directory: Path
    """The directory the files live in, mirroring the manifest key."""
    stem: str
    """Shared prefix of every file for this pair, e.g. "linux.linux-6.18.y.shallow"."""
    dated: str
    """The immutable tarball name, carrying the date and the tip."""
    latest: str
    """The fixed name, a symlink onto whichever dated tarball is current."""
    dirname: str
    """The single top-level directory inside the tarball."""


def plan_artifact(outdir: Path, repo: str, branch: Branch, now: int) -> Artifact:
    """Work out what this (repository, branch) pair publishes as.

    The tarball sits beside the repository's own name rather than in a
    directory of its own -- "stable/linux.linux-6.18.y.shallow.<date>.<tip>.tar"
    -- because a stable tree publishes a dozen of these and one flat directory
    of siblings reads better than a dozen directories holding one file each.

    The date is UTC and the tip is abbreviated. Putting the date first means a
    directory listing sorts by age; putting the tip in at all means the
    filename *names the commit inside the file*, which is a tautology worth
    having at 3am and is what lets the whole tool work without a state file.
    """
    # The manifest key is absolute, and Path() would throw away outdir if that
    # leading slash were joined on.
    relative = repo.lstrip('/').removesuffix('.git')
    parent, _sep, name = relative.rpartition('/')
    stem = f'{name}.{branch.slug}.shallow'
    # time.gmtime, not localtime: the generating host's timezone is nobody
    # else's business, and a DST shift must not rename an artifact.
    datestamp = time.strftime('%Y%m%d', time.gmtime(now))
    return Artifact(
        directory=outdir / parent if parent else outdir,
        stem=stem,
        dated=f'{stem}.{datestamp}.{branch.tip[:TIP_ABBREV]}.tar',
        latest=f'{stem}.latest.tar',
        dirname=name,
    )


def existing_tarball(artifact: Artifact, branch: Branch) -> Path | None:
    """Find an already-published tarball holding this exact tip, if any.

    This is the whole skip check, and the reason there is no state file: a
    depth-1 single-branch tarball is entirely determined by its tip, so a file
    already named after that tip is already the file we were about to build.
    An unchanged branch therefore keeps its original date, which is the honest
    answer -- the date says how old the content is, not when cron last ran.

    Globbing the stem is safe: git-check-ref-format rejects "*", "?", "[" and
    "\\" in a refname, so nothing in the stem can be a glob metacharacter.
    """
    for candidate in artifact.directory.glob(f'{artifact.stem}.*.{branch.tip[:TIP_ABBREV]}.tar'):
        if not candidate.is_symlink():
            return candidate
    return None


def file_sha256(path: Path) -> str:
    """Checksum without pulling a quarter of a gigabyte into memory."""
    digest = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_sidecar(artifact: Artifact, repo: str, branch: Branch, now: int, depth: int) -> None:
    """Describe the dated tarball in a few hundred bytes of JSON.

    This is what a CI node should actually fetch first. Reading it gives the
    node an immutable filename to ask for from then on, which removes the
    "the symlink moved while I was downloading" race outright, keeps ranged
    and resumed downloads valid, and lets a node skip the download entirely
    when it recognises the tip.
    """
    tarpath = artifact.directory / artifact.dated
    sidecar = {
        'tarball': artifact.dated,
        'repo': repo,
        'branch': branch.name,
        'tip': branch.tip,
        'created': now,
        'depth': depth,
        'size': tarpath.stat().st_size,
        'sha256': file_sha256(tarpath),
    }
    target = tarpath.with_name(f'{artifact.dated}.json')
    tmppath = target.with_name(f'.{target.name}.tmp')
    tmppath.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + '\n')
    tmppath.replace(target)


def point_at(link: Path, target: str) -> None:
    """Move one symlink onto a new target without it ever being absent."""
    if link.is_symlink() and str(link.readlink()) == target:
        return
    tmplink = link.with_name(f'.{link.name}.tmp')
    tmplink.unlink(missing_ok=True)
    tmplink.symlink_to(target)
    tmplink.replace(link)


def update_latest_links(artifact: Artifact) -> None:
    """Repoint "latest" at the dated tarball and its sidecar.

    Relative targets, so the tree survives being rsynced to a frontend that
    mounts it somewhere else entirely.
    """
    point_at(artifact.directory / artifact.latest, artifact.dated)
    point_at(artifact.directory / f'{artifact.latest}.json', f'{artifact.dated}.json')


def prune_tarballs(artifact: Artifact, keep: int = KEEP_TARBALLS) -> None:
    """Keep the current tarball and the one before it, and no more.

    Deleting the old tarball in the same run that publishes the new one leaves
    a window a CI node can fall into: it reads "latest" from a frontend that
    has synced, then asks for that dated file from one that has not -- or the
    deletion reaches a frontend before the addition does. Either way a 404
    lands in the middle of somebody's build. One cycle of overlap closes it,
    and a second cycle would just be unbounded retention with extra steps.

    Age comes from the mtime rather than the name, because two tarballs cut on
    the same day sort by tip, and a tip is not a clock.
    """
    published = [p for p in artifact.directory.glob(f'{artifact.stem}.*.tar') if not p.is_symlink()]
    for stale in sorted(published, key=lambda p: p.stat().st_mtime, reverse=True)[keep:]:
        stale.unlink(missing_ok=True)
        stale.with_name(f'{stale.name}.json').unlink(missing_ok=True)
        logger.info('  pruned: %s', stale.name)


def publish_branch(
    fullpath: Path,
    outdir: Path,
    repo: str,
    branch: Branch,
    cloneurl: str,
    now: int,
    depth: int = 1,
) -> bool:
    """Publish one branch, and tidy up after the ones published before it."""
    artifact = plan_artifact(outdir, repo, branch, now)
    artifact.directory.mkdir(parents=True, exist_ok=True)

    already = existing_tarball(artifact, branch)
    if already is not None:
        # Still worth finishing the run: the links and the pruning are what
        # keep a directory that stopped changing from drifting out of shape.
        logger.info('  current: %s', already.name)
        artifact = artifact._replace(dated=already.name)
    else:
        tarpath = artifact.directory / artifact.dated
        if not generate_tarball(fullpath, branch, cloneurl, tarpath, artifact.dirname, now, depth=depth):
            return False
        write_sidecar(artifact, repo, branch, now, depth)

    update_latest_links(artifact)
    prune_tarballs(artifact)
    return True
