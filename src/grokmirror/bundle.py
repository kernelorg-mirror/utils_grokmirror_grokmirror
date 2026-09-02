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

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Collection
from pathlib import Path
from typing import NamedTuple, TypedDict, cast

import grokmirror

# default basic logger. We override it later.
logger = logging.getLogger(__name__)

SECONDS_IN_DAY = 86400

# What grok-bundle writes alongside the bundles. The list is the only one of
# these that git clients read; the state file is our own bookkeeping and the
# leading dot keeps it out of a directory index.
STATE_NAME = '.bundlestate'
LIST_NAME = 'bundle-list'
CLONE_NAME = 'clone.bundle'

# Bumped when the state layout changes in a way this version cannot read. A
# file claiming any other version is left strictly alone.
STATE_VERSION = 1


class BundleEntry(TypedDict):
    """One generated bundle, and where it is in its publication life cycle."""

    name: str
    token: int
    created: int
    listed: bool
    unlisted: int | None
    full: bool


class BundleState(TypedDict):
    """Everything grok-bundle needs to remember about one repository."""

    version: int
    fingerprint: str
    tips: list[str]
    bundles: list[BundleEntry]


class IncrementalOpts(NamedTuple):
    """The knobs that only mean anything in incremental mode."""

    enabled: bool = False
    publishdelay: int = 7200
    prunedelay: int = 86400
    maxbundles: int = 30
    clonebundle: bool = True


# The default everywhere: incremental mode is strictly opt-in, so an existing
# invocation keeps writing the single clone.bundle it always has.
NO_INCREMENTAL = IncrementalOpts()


class BundleRefs(NamedTuple):
    """The refs going into a bundle, and the branch tips among them."""

    refs: list[str]
    tips: list[str]


def get_repo_size(fullpath: grokmirror.StrPath) -> int:
    reposize = 0
    obj_info = grokmirror.get_repo_obj_info(fullpath)
    if 'alternate' in obj_info:
        altpath = grokmirror.get_altrepo(fullpath)
        if altpath:
            reposize = get_repo_size(altpath)
    reposize += int(obj_info['size'])
    reposize += int(obj_info['size-pack'])

    logger.debug('%s size: %s', fullpath, reposize)
    return reposize


def select_refs(fullpath: grokmirror.StrPath, maxrefage: int, now: int) -> BundleRefs:
    """Pick the refs to bundle, leaving out branches nobody maintains any more.

    A branch counts as maintained when its tip commit is younger than maxrefage
    days; a maxrefage of 0 keeps every branch. Tags are then chosen by
    reachability from the surviving branches rather than by their own age, and
    that distinction is the whole point: a tag pointing into a retired branch
    drags that branch's entire history back into the bundle, which is exactly
    what the age limit exists to prevent.

    HEAD comes along when it points at a branch that survived, because it is
    what "git clone" checks out: a bundle without it clones into an empty
    working tree unless the client's init.defaultBranch happens to match.

    Returns empty lists when nothing qualifies, which the caller treats the
    same way as a repository with no refs at all.
    """
    cutoff = now - maxrefage * SECONDS_IN_DAY if maxrefage > 0 else None
    ecode, out, err = grokmirror.run_git_command(
        fullpath, ['for-each-ref', '--format=%(committerdate:unix) %(objectname) %(refname)', 'refs/heads']
    )
    if ecode > 0:
        logger.info('  could not list branches in %s: %s', fullpath, err.strip())
        return BundleRefs([], [])

    branches = []
    tips = []
    for line in out.splitlines():
        stamp, _sep, rest = line.partition(' ')
        oid, _sep, refname = rest.partition(' ')
        # A branch always points at a commit, so a line without a usable date
        # is something we do not understand and should not be guessing about.
        if not (stamp.isdigit() and oid and refname):
            continue
        if cutoff is not None and int(stamp) < cutoff:
            continue
        branches.append(refname)
        tips.append(oid)

    if not branches:
        return BundleRefs([], [])

    if cutoff is None:
        # Nothing was dropped, so no tag can drag a retired branch back in and
        # the walks below would only be confirming that. Worth skipping: they
        # cost one traversal per branch, and this is the 116-branch case.
        ecode, out, err = grokmirror.run_git_command(fullpath, ['for-each-ref', '--format=%(refname)', 'refs/tags'])
        if ecode > 0:
            logger.info('  could not list tags in %s: %s', fullpath, err.strip())
            return BundleRefs([], [])
        tags = set(out.split())
    else:
        # One reachability walk per surviving branch. That is the entire cost
        # of the filter: it scales with the number of branches kept, not with
        # the number of tags the repository has.
        tags = set()
        for branch in branches:
            ecode, out, err = grokmirror.run_git_command(
                fullpath, ['for-each-ref', f'--merged={branch}', '--format=%(refname)', 'refs/tags']
            )
            if ecode > 0:
                logger.info('  could not list tags merged into %s: %s', branch, err.strip())
                return BundleRefs([], [])
            tags.update(out.split())

    # A detached HEAD has nothing to name here, and a HEAD pointing at a branch
    # the age filter dropped has to stay out -- naming it would pull that whole
    # branch back in through the back door.
    ecode, out, _err = grokmirror.run_git_command(fullpath, ['symbolic-ref', '--quiet', 'HEAD'])
    head = ['HEAD'] if ecode == 0 and out in branches else []

    return BundleRefs([*branches, *head, *sorted(tags)], tips)


def revlist_stdin(refs: list[str], exclude: list[str]) -> bytes:
    """Feed git a rev-list on stdin: the kernel's tag count outgrows argv."""
    lines = [*refs, *[f'^{oid}' for oid in exclude]]
    return ('\n'.join(lines) + '\n').encode()


def new_state() -> BundleState:
    return {'version': STATE_VERSION, 'fingerprint': '', 'tips': [], 'bundles': []}


def valid_entry(entry: object) -> bool:
    """Check one bundle record has every field the code below reads."""
    if not isinstance(entry, dict):
        return False
    if not isinstance(entry.get('name'), str):
        return False
    if not all(isinstance(entry.get(key), int) for key in ('token', 'created')):
        return False
    if not all(isinstance(entry.get(key), bool) for key in ('listed', 'full')):
        return False
    unlisted = entry.get('unlisted')
    return unlisted is None or isinstance(unlisted, int)


def valid_state(data: dict[str, object]) -> bool:
    """Check the shape we are about to trust, field by field.

    The version number says the layout is one we know; it says nothing about a
    file that was truncated or hand-edited. Everything below is read without a
    default further on, so one missing key is a traceback that takes the whole
    run down with it and leaves every later repository in the manifest
    unprocessed.
    """
    if not isinstance(data.get('fingerprint'), str):
        return False
    tips = data.get('tips')
    if not isinstance(tips, list) or not all(isinstance(tip, str) for tip in tips):
        return False
    bundles = data.get('bundles')
    return isinstance(bundles, list) and all(valid_entry(entry) for entry in bundles)


def read_state(bundledir: Path) -> BundleState | None:
    """Load a directory's bundle state, or None if it must be left alone."""
    statefile = bundledir / STATE_NAME
    if not statefile.exists():
        # Either brand new, or the single-bundle layout. Both start from
        # scratch; an old clone.bundle keeps serving until the first full
        # bundle of the new layout is published and the link moves onto it.
        return new_state()
    try:
        data = json.loads(statefile.read_text(encoding='utf-8'))
    except (OSError, ValueError) as ex:
        # Starting over here is the one thing we must not do. With no record of
        # which bundles are published, the run would write an empty list --
        # which means deleting the list clients are fetching, with none of the
        # publication delay that normally protects them -- and strand every
        # bundle already on disk, since nothing left would ever prune them.
        logger.info('  skipped: %s (cannot read %s: %s)', bundledir, STATE_NAME, ex)
        return None
    if not isinstance(data, dict) or data.get('version') != STATE_VERSION:
        # Written by a version that knows something we do not. Rebuilding from
        # scratch would unpublish bundles clients are in the middle of using,
        # so do nothing at all with this repository.
        logger.info('  skipped: %s (unknown state version)', bundledir)
        return None
    if not valid_state(data):
        logger.info('  skipped: %s (malformed %s)', bundledir, STATE_NAME)
        return None
    return cast('BundleState', data)


def write_state(bundledir: Path, state: BundleState) -> None:
    tmpfile = bundledir / f'{STATE_NAME}.tmp'
    # Flushed and fsynced before the rename, because read_state() now refuses
    # to touch a directory whose state it cannot parse. That is the right call
    # -- the alternative unpublishes live bundles -- but it does mean a state
    # file that lands truncated after a crash needs a human, so do not let the
    # rename beat the data to disk.
    with tmpfile.open('w', encoding='utf-8') as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write('\n')
        fh.flush()
        os.fsync(fh.fileno())
    tmpfile.replace(bundledir / STATE_NAME)


def write_bundle_list(bundledir: Path, state: BundleState) -> None:
    """Write the bundle list clients read, or remove it if nothing is public."""
    listed = sorted((e for e in state['bundles'] if e['listed']), key=lambda e: e['token'])
    listfile = bundledir / LIST_NAME
    if not listed:
        # No list is the right answer here, and better than an empty one: git
        # reads that as a valid list offering nothing and stops looking.
        listfile.unlink(missing_ok=True)
        return

    lines = ['[bundle]', '\tversion = 1', '\tmode = all', '\theuristic = creationToken', '']
    for entry in listed:
        # git resolves these against the URI the list itself was fetched from,
        # so nothing written here needs to know the CDN hostname.
        lines.extend(
            [
                f'[bundle "{entry["token"]:010d}"]',
                f'\turi = {entry["name"]}',
                f'\tcreationToken = {entry["token"]}',
                '',
            ]
        )
    tmpfile = bundledir / f'.{LIST_NAME}.tmp'
    tmpfile.write_text('\n'.join(lines), encoding='utf-8')
    tmpfile.replace(listfile)


def promote_bundles(state: BundleState, now: int, publishdelay: int) -> None:
    """Put bundles on the list once they have had time to reach the mirrors.

    Nothing is ever listed by the run that created it. The hosts generating
    bundles are not the hosts serving them -- on kernel.org they are two rsync
    hops and five independent frontends apart -- and bundle.mode=all means a
    client needs every bundle the list names. A list naming a bundle that has
    not arrived yet fails the clone outright, so the file has to be in place
    everywhere before anything points at it.
    """
    for entry in sorted(state['bundles'], key=lambda e: e['token']):
        if entry['listed'] or entry['unlisted'] is not None:
            continue
        if now - entry['created'] < publishdelay:
            # Stop, rather than judging each bundle on its own: an increment
            # is built against the tips the bundle before it ended at, so
            # listing one whose predecessor is still held back publishes a set
            # no client can unbundle. Tokens are kept monotonic by
            # next_token(), but 'created' is raw wall clock, and a clock that
            # steps backwards between two runs is all it takes.
            break
        if entry['full']:
            # A full bundle supersedes everything before it, but only now, as
            # it joins the list. Retiring the old ones any earlier would leave
            # a window with no complete set of bundles to offer at all.
            for older in state['bundles']:
                if older['token'] < entry['token'] and older['unlisted'] is None:
                    older['listed'] = False
                    older['unlisted'] = now
        entry['listed'] = True
        logger.info('  published: %s', entry['name'])


def prune_bundles(bundledir: Path, state: BundleState, now: int, prunedelay: int) -> None:
    """Delete bundles that have been off the list long enough.

    A client that fetched the list just before an entry was dropped is still
    working its way through it, and a CDN can hand out that old list for a
    good while longer, so a bundle has to outlive its last mention.
    """
    keep: list[BundleEntry] = []
    for entry in state['bundles']:
        unlisted = entry['unlisted']
        if unlisted is not None and now - unlisted >= prunedelay:
            (bundledir / entry['name']).unlink(missing_ok=True)
            logger.info('  pruned: %s', entry['name'])
            continue
        keep.append(entry)
    state['bundles'] = keep


def update_clone_bundle(bundledir: Path, state: BundleState) -> None:
    """Keep clone.bundle pointing at the newest published full bundle.

    "repo" and anything else that hardcodes the old name goes on working. The
    link only ever moves onto a bundle that is already published, so it cannot
    end up pointing at a file the mirrors have not received yet.
    """
    published = [e for e in state['bundles'] if e['full'] and e['listed']]
    if not published:
        return
    target = max(published, key=lambda e: e['token'])['name']
    link = bundledir / CLONE_NAME
    if link.is_symlink() and str(link.readlink()) == target:
        return
    tmplink = bundledir / f'.{CLONE_NAME}.tmp'
    tmplink.unlink(missing_ok=True)
    tmplink.symlink_to(target)
    # Replaces the plain file left behind by the single-bundle layout, too.
    tmplink.replace(link)


def needs_full_bundle(state: BundleState, maxbundles: int) -> bool:
    """Decide whether to cut a new full bundle instead of an increment.

    Bundles cannot be merged, so "compact the old increments" means building a
    new full bundle and letting the rest age out.
    """
    live = [e for e in state['bundles'] if e['unlisted'] is None]
    if any(e['full'] and not e['listed'] for e in live):
        # One is already made and waiting out its publication delay.
        return False
    if not any(e['full'] for e in live):
        return True
    return len(live) >= maxbundles


def next_token(state: BundleState, now: int) -> int:
    """Creation tokens must increase, including for two runs in one second."""
    tokens = [e['token'] for e in state['bundles']]
    if not tokens:
        return now
    return max(now, max(tokens) + 1)


def add_bundle(
    fullpath: Path,
    bundledir: Path,
    bundlename: str,
    repofpr: str,
    state: BundleState,
    git_args: list[str],
    maxrefage: int,
    maxsize: int,
    maxbundles: int,
    now: int,
) -> None:
    """Generate one bundle -- full or incremental -- and record it as pending."""
    selected = select_refs(fullpath, maxrefage, now)
    if not selected.refs:
        logger.info('  skipped: %s (no refs to bundle)', bundlename)
        return

    full = needs_full_bundle(state, maxbundles)
    exclude = [] if full else state['tips']

    if not full:
        ecode, out, _err = grokmirror.run_git_command(
            fullpath, ['rev-list', '--count', '--stdin'], stdin=revlist_stdin(selected.refs, exclude)
        )
        if ecode > 0:
            # The recorded tips are not in the repository any more, so there is
            # nothing to make an increment against.
            logger.info('  full rebuild: %s (previous tips are gone)', bundlename)
            full = True
            exclude = []
        elif out.strip() == '0':
            # A fingerprint also changes when a ref is deleted or moved back,
            # neither of which adds a commit. Record where we are and move on,
            # rather than asking git for an empty bundle every run from now on.
            logger.info('  no new commits: %s', bundlename)
            state['fingerprint'] = repofpr
            state['tips'] = selected.tips
            return

    if full:
        total_size = get_repo_size(fullpath) / 1024 / 1024
        if total_size > maxsize:
            logger.info('  skipped: %s (%s > %s)', bundlename, total_size, maxsize)
            return

    token = next_token(state, now)
    name = f'{token:010d}.bundle'
    bfile = bundledir / name
    logger.info(' generate: %s', bfile)
    ecode, _out, err = grokmirror.run_git_command(
        fullpath,
        [*git_args, 'bundle', 'create', str(bfile), '--stdin'],
        stdin=revlist_stdin(selected.refs, exclude),
    )
    if ecode > 0:
        logger.info('  failed: %s (%s)', bundlename, err.strip())
        bfile.unlink(missing_ok=True)
        return

    state['bundles'].append(
        {
            'name': name,
            'token': token,
            'created': now,
            'listed': False,
            'unlisted': None,
            'full': full,
        }
    )
    state['fingerprint'] = repofpr
    state['tips'] = selected.tips


def generate_bundles(
    config: grokmirror.GrokConfigParser,
    outdir: str,
    gitargs: str,
    revlistargs: str,
    maxsize: int,
    include: Collection[str],
    maxrefage: int = 0,
    incremental: IncrementalOpts = NO_INCREMENTAL,
) -> int:
    # Nothing here takes the repository lock, on purpose: the repositories are
    # only ever read, and a bundle that loses a race with a concurrent repack
    # fails and is simply made again on the next run. The bundle directory is
    # the part that is not safe. read_state() and write_state() bracket
    # everything one repository does, so two grok-bundle runs sharing an
    # output directory lose each other's bookkeeping and leave bundles on disk
    # with no record to prune them by. Run one at a time.

    # load_config_file() guarantees both of these are set
    manifest = grokmirror.read_manifest(config['core']['manifest'])
    toplevel = Path(config['core']['toplevel']).resolve()
    ignorerefs = grokmirror.get_ignorerefs(config)
    # An empty string means "no extra arguments", and str.split() already
    # returns an empty list for it -- but only if we don't skip the split.
    git_args = gitargs.split()
    revlist_args = revlistargs.split()
    now = int(time.time())
    # Manifest keys are absolute ('/test/one.git'), but -i is documented as
    # accepting both spellings, so match each pattern with and without its
    # leading slash.
    includematch = grokmirror.compile_globs([p for x in include for p in (x, x.lstrip('/'))])

    for repo in manifest:
        logger.debug('Checking %s', repo)
        # Does it match our globbing pattern?
        if not includematch.match(repo):
            logger.debug('%s does not match include list, skipping', repo)
            continue

        fullpath = grokmirror.gitdir_to_fullpath(toplevel, repo)
        # The bundle directory mirrors the manifest key relative to outdir, so
        # this name keeps its own lstrip: it is not a path under toplevel, and
        # Path() would discard outdir if it were joined on with its slash.
        bundlename = repo.lstrip('/')

        bundledir = Path(outdir, bundlename.removesuffix('.git'))
        bundledir.mkdir(parents=True, exist_ok=True)

        repofpr = grokmirror.get_repo_fingerprint(str(toplevel), bundlename, ignorerefs=ignorerefs)
        logger.debug('%s fingerprint is %s', bundlename, repofpr)
        if not repofpr:
            # Either the repo is gone or it has no refs at all. Either way there
            # is nothing to bundle, and no fingerprint to record next to it.
            logger.info('  skipped: %s (no refs to bundle)', bundlename)
            continue

        if incremental.enabled:
            state = read_state(bundledir)
            if state is None:
                continue
            if state['fingerprint'] == repofpr:
                logger.info('  unchanged: %s', bundlename)
            else:
                add_bundle(
                    fullpath,
                    bundledir,
                    bundlename,
                    repofpr,
                    state,
                    git_args,
                    maxrefage,
                    maxsize,
                    incremental.maxbundles,
                    now,
                )
            # These run whether or not anything was generated: a bundle made
            # last week is only reaching the list now, and one dropped from the
            # list last week is only being deleted now.
            promote_bundles(state, now, incremental.publishdelay)
            prune_bundles(bundledir, state, now, incremental.prunedelay)
            write_bundle_list(bundledir, state)
            if incremental.clonebundle:
                update_clone_bundle(bundledir, state)
            write_state(bundledir, state)
            continue

        # Do we have a bundle file already?
        bfile = bundledir / 'clone.bundle'
        bfprfile = bundledir / '.fingerprint'
        logger.debug('Looking for %s', bfile)
        if bfile.exists():
            # Do we have a bundle fingerprint?
            logger.debug('Found existing bundle in %s', bfile)
            if bfprfile.exists():
                bfpr = bfprfile.read_text(encoding='utf-8').strip()
                logger.debug('Read bundle fingerprint from %s: %s', bfprfile, bfpr)
                if bfpr == repofpr:
                    logger.info('  skipped: %s (unchanged)', bundlename)
                    continue

        logger.debug('checking size of %s', bundlename)
        total_size = get_repo_size(fullpath) / 1024 / 1024

        if total_size > maxsize:
            logger.info('  skipped: %s (%s > %s)', bundlename, total_size, maxsize)
            continue

        # Only pay for the reachability walks once we know we are generating.
        bundle_stdin = None
        if maxrefage > 0:
            bundle_refs = select_refs(fullpath, maxrefage, now).refs
            if not bundle_refs:
                logger.info('  skipped: %s (no branch newer than %s days)', bundlename, maxrefage)
                continue
            # Fed on stdin rather than argv: the kernel stable tree alone
            # contributes a few thousand tag refs, and argv has a limit.
            bundle_stdin = ('\n'.join(bundle_refs) + '\n').encode()
            fullargs = [*git_args, 'bundle', 'create', str(bfile), '--stdin']
        else:
            fullargs = [*git_args, 'bundle', 'create', str(bfile), *revlist_args]

        logger.debug('Full git args: %s', fullargs)
        logger.info(' generate: %s', bfile)
        ecode, _out, _err = grokmirror.run_git_command(fullpath, fullargs, stdin=bundle_stdin)

        if ecode == 0:
            bfprfile.write_text(repofpr, encoding='utf-8')
            logger.debug('Wrote %s into %s', repofpr, bfprfile)

    return 0


def parse_args() -> argparse.Namespace:

    # noinspection PyTypeChecker
    op = argparse.ArgumentParser(
        prog='grok-bundle',
        description='Generate clone.bundle files for use with "repo"',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    op.add_argument(
        '-v', '--verbose', action='store_true', default=False, help='Be verbose and tell us what you are doing'
    )
    op.add_argument('-c', '--config', required=True, help='Location of the configuration file')
    op.add_argument('-o', '--outdir', required=True, help='Location where to store bundle files')
    op.add_argument('-g', '--gitargs', default='-c core.compression=9', help='extra args to pass to git')
    op.add_argument('-r', '--revlistargs', default='--branches HEAD', help='Rev-list args to use')
    op.add_argument('-s', '--maxsize', type=int, default=2, help='Maximum size of git repositories to bundle (in GiB)')
    op.add_argument(
        '-i', '--include', nargs='*', default='*', help='List repositories to bundle (accepts shell globbing)'
    )
    op.add_argument(
        '--max-ref-age',
        type=int,
        default=0,
        metavar='DAYS',
        help='Bundle only branches whose tip is newer than this, plus the tags on them (0 disables)',
    )
    op.add_argument(
        '--incremental',
        action='store_true',
        default=False,
        help='Publish a bundle-uri bundle list, adding incremental bundles instead of rewriting one big one',
    )
    op.add_argument(
        '--publish-delay',
        type=int,
        default=7200,
        metavar='SECONDS',
        help='How long a new bundle must exist before the list may name it (incremental mode)',
    )
    op.add_argument(
        '--prune-delay',
        type=int,
        default=86400,
        metavar='SECONDS',
        help='How long a bundle must stay on disk after the list stops naming it (incremental mode)',
    )
    op.add_argument(
        '--max-bundles',
        type=int,
        default=30,
        metavar='NUM',
        help='Start over with a full bundle once the list grows to this many (incremental mode)',
    )
    op.add_argument(
        '--no-clone-bundle',
        action='store_false',
        dest='clone_bundle',
        default=True,
        help='Do not maintain a clone.bundle symlink for "repo" (incremental mode)',
    )
    op.add_argument('--version', action='version', version=grokmirror.VERSION)

    opts = op.parse_args()

    return opts


def grok_bundle(
    cfgfile: str,
    outdir: str,
    gitargs: str,
    revlistargs: str,
    maxsize: int,
    include: Collection[str],
    verbose: bool = False,
    maxrefage: int = 0,
    incremental: IncrementalOpts = NO_INCREMENTAL,
) -> int:
    config = grokmirror.load_config_file(cfgfile)

    logfile = config['core'].get('log', None)
    loglevel = logging.DEBUG if config['core'].get('loglevel', 'info') == 'debug' else logging.INFO

    grokmirror.init_logger('bundle', logfile, loglevel, verbose)

    return generate_bundles(config, outdir, gitargs, revlistargs, maxsize, include, maxrefage, incremental)


def command() -> None:
    opts = parse_args()

    try:
        retval = grok_bundle(
            opts.config,
            opts.outdir,
            opts.gitargs,
            opts.revlistargs,
            opts.maxsize,
            opts.include,
            verbose=opts.verbose,
            maxrefage=opts.max_ref_age,
            incremental=IncrementalOpts(
                enabled=opts.incremental,
                publishdelay=opts.publish_delay,
                prunedelay=opts.prune_delay,
                maxbundles=opts.max_bundles,
                clonebundle=opts.clone_bundle,
            ),
        )
    except grokmirror.GrokError as ex:
        sys.stderr.write(f'ERROR: {ex}\n')
        retval = 1

    sys.exit(retval)


if __name__ == '__main__':
    command()
