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

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

import grokmirror

logger = logging.getLogger(__name__)

SECONDS_IN_DAY = 86400
# Long enough to stay unambiguous in a repository the size of linux.git, short
# enough to stay readable in a filename. This is what git itself abbreviates
# to by default in a tree that big.
TIP_ABBREV = 7
# The current tarball plus the previous one. See prune_tarballs().
KEEP_TARBALLS = 2
# Scratch clones are made under this prefix, inside the output directory. The
# leading dot keeps them out of a directory listing and out of an rsync that
# excludes dotfiles, which is also why they need sweeping up -- see
# sweep_stale_workdirs().
WORKDIR_PREFIX = '.shallowtar-'
# How long a scratch directory has to have been sitting there before a later
# run will clear it away.
STALE_WORKDIR_AGE = SECONDS_IN_DAY
# Inside .git/ of the published clone, beside git's own description. It goes
# there rather than at the top of the tree because the top of the tree is the
# repository's own content, and a file we invented appearing in "git status"
# as untracked would be our noise in somebody else's working tree.
README_NAME = 'shallow-tar.readme'


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
    pattern: str | Sequence[str],
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

    Several patterns may be passed, for a repository that more than one
    --branches switch names. They are matched as one set rather than one at a
    time, so the cap and the collision check both see every branch the
    repository is going to publish.

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

    matches = grokmirror.compile_globs([pattern] if isinstance(pattern, str) else pattern)
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


def describe_clone(branch: Branch, cloneurl: str, now: int, depth: int) -> str:
    """The .git/description git would otherwise fill with a placeholder.

    Generation data only, and a pointer at the longer file. A description is
    a description -- gitweb shows its first line as a one-liner, and a reader
    who opens it wants to know what the tree is, not to be taught how to use
    it. The advice lives in README_NAME, which is free to be as long as it
    needs to be because nothing else has designs on it.
    """
    created = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(now))
    name = cloneurl.rstrip('/').rpartition('/')[2]
    commits = 'one commit' if depth == 1 else f'{depth} commits'
    # A URL and a full object name are both long enough to wrap on their own,
    # so they get their own lines rather than being written into prose.
    return f"""Shallow single-branch clone of {name}, branch {branch.name}, {commits} deep.
Origin: {cloneurl}
Tip:    {branch.tip}
Generated {created} by grok-shallow-tar {grokmirror.VERSION}.

Read .git/{README_NAME} before running git commands in this tree.
"""


def clone_readme() -> str:
    """The long-form file, for somebody who has found this tree and forgotten it.

    The reader to picture is not a CI runner -- that one never reads anything.
    It is a person who downloaded a tarball weeks ago, unpacked it, poked at
    it, forgot, and has just found the directory again. Their questions are
    "what is this", "is it safe", and "how do I use it", and every one of
    those has an answer we know at generation time and they cannot easily work
    out later.

    The safety section is the part that earns the file. This tree came off the
    network as a tarball, and a tarball can carry anything. grok-shallow-tar
    ships nothing dangerous, but the reader cannot tell our tarball from one
    tampered with in transit or on a mirror.

    The advice is to discard .git/config rather than to clear .git/hooks,
    because clearing the hooks is not sufficient and reads as though it were.
    git treats that file as instructions: core.hooksPath relocates the hooks
    the reader just deleted, core.fsmonitor names a program run during
    ordinary commands -- including during the "git fsck" meant to check the
    tree -- and core.sshCommand and remote.*.uploadpack run one on a fetch.
    All three were confirmed to fire on an unpacked tarball under the earlier
    "rm -f .git/hooks/*" advice. Deleting the whole file and letting git
    rebuild a default one closes the entire class, and keeps the objects, the
    refs and the shallow boundary.

    What none of it establishes is that the history is genuine, so the last
    paragraph is the one that matters: a full object ID verifies itself, and a
    ref that came in the tarball does not.

    Nothing here is interpolated. Everything specific to this particular
    tarball -- origin, branch, tip, depth, when it was made -- lives in
    describe_clone(), which is why this file can point at "description"
    instead of repeating it.
    """
    return """What this is
------------
A shallow, single-branch clone, published as a tarball so that CI jobs can
untar it directly and fetch any new commits. See "description" for details
about how it was generated.

Before you run any git commands
-------------------------------
This tree arrived over the network as a tarball. None of the usual git safety
checks ran on it, so you should not blindly trust it to be safe.

Most of the risk is in .git/config, which git reads as instructions rather
than as data: core.hooksPath, core.fsmonitor, core.sshCommand and others name
programs that ordinary commands will run. Clearing .git/hooks does not cover
it. Throw the config away instead and let git write a clean one:

  rm -rf .git/hooks .git/config .git/objects/info/alternates
  git init
  git remote add -t [branch] --no-tags origin [the URL you trust]
  git fsck

"git init" keeps the objects, the refs and the shallow boundary. Use a URL
your own setup knows rather than the one that arrived in the tarball. The
fsck then checks that every object is intact and is what its name says it is,
which on a kernel-sized tree takes about ten seconds.

Using it
--------
You should fetch any new commits:

  git remote update

Then, check out the commit you want:

  git checkout [commit-id]

Name that commit by its full object ID, from somewhere you trust. An object
ID is checked against the object it names, so it is the one thing here a
tampered tarball cannot answer wrongly; a branch or tag in the tarball is
only whatever the tarball says it is.

Do NOT run --deepen or --unshallow
----------------------------------
If you need the full history, clone a fresh copy from the origin in
"description". Do not run "git fetch --unshallow" in this repository: it asks
the server to build the whole history as a single pack, which is very heavy on
the server side. A clean clone gets you a better repository anyway.
"""


def prepare_clone(gitdir: Path, branch: Branch, cloneurl: str, now: int, depth: int = 1) -> bool:
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

    # Two files rather than one: git's own description, which stays short
    # because other things display it, and a readme beside it that can take
    # the space the explanation needs. See describe_clone() and clone_readme().
    try:
        (gitdir / 'description').write_text(describe_clone(branch, cloneurl, now, depth))
        (gitdir / README_NAME).write_text(clone_readme())
    except OSError as ex:
        logger.info('  failed: could not write the description (%s)', ex)
        return False
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
    workdir = Path(tempfile.mkdtemp(prefix=WORKDIR_PREFIX, dir=tarpath.parent))
    try:
        clonedir = workdir / dirname
        if not clone_shallow(fullpath, branch, clonedir, depth):
            return False
        gitdir = clonedir / '.git'
        if not verify_shallow(gitdir, branch):
            return False
        if not prepare_clone(gitdir, branch, cloneurl, now, depth=depth):
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


def sweep_stale_workdirs(outdir: Path, now: int, maxage: int = STALE_WORKDIR_AGE) -> None:
    """Remove scratch directories an earlier run was killed before cleaning up.

    generate_tarball() removes its own scratch directory in a "finally", which
    covers every way the code itself can fail -- but not a signal. A SIGTERM
    from an impatient init script, or the OOM killer arriving mid-clone, ends
    the process without ever running it, and what is left behind is most of a
    clone of linux.git under a dot-name nothing ever looks at. A signal handler
    would not close this either, since SIGKILL cannot be caught at all, so the
    only complete answer is for the next run to clear up after the last one.

    The age check is what makes doing that unconditionally safe: a scratch
    directory is only litter once nothing can still be writing to it. Two runs
    overlapping is a deployment mistake rather than something supported, but
    deleting a live clone out from under the other one would turn a slow cron
    job into a mysterious one.
    """
    if not outdir.is_dir():
        return
    for dirpath, dirnames, _filenames in os.walk(outdir):
        stale = [name for name in dirnames if name.startswith(WORKDIR_PREFIX)]
        # In place, because that is how os.walk is told not to descend: a
        # scratch directory holds an entire clone, and walking into one would
        # cost more than everything else this sweep does put together. This
        # matters most for the ones being *kept* -- a directory that gets
        # removed below has already gone by the time walk would open it.
        dirnames[:] = [name for name in dirnames if not name.startswith(WORKDIR_PREFIX)]
        for name in stale:
            path = Path(dirpath, name)
            try:
                age = now - int(path.stat().st_mtime)
            except OSError as ex:
                # Another run finishing normally will have just removed it,
                # which is the outcome we wanted anyway.
                logger.debug('could not stat %s: %s', path, ex)
                continue
            if age < maxage:
                logger.debug('%s is only %s seconds old, leaving it alone', path, age)
                continue
            logger.info('  cleanup: %s, left behind by an earlier run', path)
            shutil.rmtree(path, ignore_errors=True)


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


class Pattern(NamedTuple):
    """One --branches switch: which repositories, and which of their branches."""

    repoglob: str
    branchglob: str


def parse_pattern(value: str) -> Pattern:
    """Split a REPOGLOB:BRANCHGLOB argument.

    The separator is the *last* colon, because the two halves are not equally
    restricted: git-check-ref-format rejects a colon in a refname, so the
    branch glob can never contain one, while a repository path perfectly well
    can. Splitting on the first colon instead would cut such a path in half.
    """
    # rpartition puts the whole string in the tail when there is no colon at
    # all, so an argument missing the separator arrives here as an empty
    # repository glob and is caught by the same check as an empty half.
    repoglob, _sep, branchglob = value.rpartition(':')
    if not repoglob or not branchglob:
        raise argparse.ArgumentTypeError(f'expected REPOGLOB:BRANCHGLOB, got "{value}"')
    return Pattern(repoglob=repoglob, branchglob=branchglob)


def match_patterns(repo: str, patterns: Sequence[Pattern]) -> list[str]:
    """The branch globs that apply to one repository, in the order given.

    Manifest keys are absolute ("/pub/scm/linux.git"), and a repository glob
    is documented as accepting either spelling, so the leading slash comes off
    both sides before matching rather than being required to agree.
    """
    globs = []
    for pattern in patterns:
        matcher = grokmirror.compile_globs([pattern.repoglob.lstrip('/')])
        if matcher.match(repo.lstrip('/')):
            globs.append(pattern.branchglob)
    return globs


def generate_tarballs(
    config: grokmirror.GrokConfigParser,
    outdir: str,
    patterns: Sequence[Pattern],
    cloneurlbase: str,
    depth: int = 1,
    maxrefage: int = 0,
    maxbranches: int = 0,
) -> int:
    """Publish a tarball for every branch every --branches switch asked for.

    Repositories are opt-in: one that no pattern names publishes nothing.
    These artifacts are hundreds of megabytes each, so a default of "all of
    them" would be a surprising way to fill a disk.
    """
    # Nothing here takes the repository lock, the same way grok-bundle does
    # not: the repositories are only read, and a clone that loses a race with
    # a repack fails and is simply made again on the next run.

    # load_config_file() guarantees both of these are set
    manifest = grokmirror.read_manifest(config['core']['manifest'])
    toplevel = Path(config['core']['toplevel']).resolve()
    outpath = Path(outdir)
    now = int(time.time())
    retval = 0

    # Before anything else, so a run that goes on to fill the disk has already
    # given back whatever a killed run was holding.
    sweep_stale_workdirs(outpath, now)

    for repo in manifest:
        globs = match_patterns(repo, patterns)
        if not globs:
            # Skipping here is an optimisation rather than the opt-in itself:
            # an empty glob list already matches no branch. It saves a git
            # invocation per repository, and on a full kernel.org manifest
            # that is over a thousand of them.
            logger.debug('%s matches no --branches pattern, skipping', repo)
            continue

        fullpath = grokmirror.gitdir_to_fullpath(toplevel, repo)
        branches = select_branches(fullpath, globs, now, maxrefage=maxrefage, maxbranches=maxbranches)
        if not branches:
            logger.info('  skipped: %s (no branch matches)', repo)
            continue

        # The manifest key is a path under the site, so it appends cleanly.
        cloneurl = cloneurlbase.rstrip('/') + repo
        for branch in branches:
            if not publish_branch(fullpath, outpath, repo, branch, cloneurl, now, depth=depth):
                # Keep going: one repository out of room or mid-repack should
                # not cost the rest of the run, but the exit code should still
                # say the run was not clean, because cron reads that.
                retval = 1

    return retval


def parse_args() -> argparse.Namespace:
    # noinspection PyTypeChecker
    op = argparse.ArgumentParser(
        prog='grok-shallow-tar',
        description='Publish shallow single-branch repositories as tarballs, for CI systems',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    op.add_argument(
        '-v', '--verbose', action='store_true', default=False, help='Be verbose and tell us what you are doing'
    )
    op.add_argument('-c', '--config', required=True, help='Location of the configuration file')
    op.add_argument('-o', '--outdir', required=True, help='Location where to publish the tarballs')
    op.add_argument(
        '--branches',
        action='append',
        type=parse_pattern,
        required=True,
        metavar='REPOGLOB:BRANCHGLOB',
        help='Publish these branches of these repositories (repeat for more)',
    )
    op.add_argument(
        '--clone-url-base',
        required=True,
        metavar='URL',
        help='Public site the tarballs should fetch from, e.g. https://git.kernel.org',
    )
    op.add_argument('--depth', type=int, default=1, help='How many commits of history to put in the tarball')
    op.add_argument(
        '--max-ref-age',
        type=int,
        default=0,
        metavar='DAYS',
        help='Publish only branches whose tip is newer than this (0 disables)',
    )
    op.add_argument(
        '--max-branches',
        type=int,
        default=0,
        metavar='NUM',
        help='Publish at most this many branches per repository, newest first (0 disables)',
    )
    op.add_argument('--version', action='version', version=grokmirror.VERSION)

    return op.parse_args()


def grok_shallow_tar(
    cfgfile: str,
    outdir: str,
    patterns: Sequence[Pattern],
    cloneurlbase: str,
    verbose: bool = False,
    depth: int = 1,
    maxrefage: int = 0,
    maxbranches: int = 0,
) -> int:
    config = grokmirror.load_config_file(cfgfile)

    logfile = config['core'].get('log', None)
    loglevel = logging.DEBUG if config['core'].get('loglevel', 'info') == 'debug' else logging.INFO

    grokmirror.init_logger('shallow-tar', logfile, loglevel, verbose)

    return generate_tarballs(
        config,
        outdir,
        patterns,
        cloneurlbase,
        depth=depth,
        maxrefage=maxrefage,
        maxbranches=maxbranches,
    )


def command() -> None:
    opts = parse_args()

    try:
        retval = grok_shallow_tar(
            opts.config,
            opts.outdir,
            opts.branches,
            opts.clone_url_base,
            verbose=opts.verbose,
            depth=opts.depth,
            maxrefage=opts.max_ref_age,
            maxbranches=opts.max_branches,
        )
    except grokmirror.GrokError as ex:
        sys.stderr.write(f'ERROR: {ex}\n')
        retval = 1

    sys.exit(retval)


if __name__ == '__main__':
    command()
