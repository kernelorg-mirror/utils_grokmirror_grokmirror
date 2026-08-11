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
import logging
import os
import sys
import time
from pathlib import Path

import grokmirror

logger = logging.getLogger(__name__)


def update_manifest(
    manifest: grokmirror.Manifest, toplevel: str, fullpath: str, usenow: bool, ignorerefs: list[str] | None
) -> None:
    logger.debug('Examining %s', fullpath)
    if not grokmirror.is_bare_git_repo(fullpath):
        logger.critical('Error opening %s.', fullpath)
        logger.critical('Make sure it is a bare git repository.')
        raise grokmirror.GrokError(f'Not a bare git repository: {fullpath}')

    gitdir = grokmirror.fullpath_to_gitdir(toplevel, fullpath)
    repoinfo = grokmirror.get_repo_defs(toplevel, gitdir, usenow=usenow, ignorerefs=ignorerefs)
    # Ignore it if it's an empty git repository
    if not repoinfo.get('fingerprint'):
        logger.info(' manifest: ignored %s (no heads)', gitdir)
        return

    if gitdir not in manifest:
        # In grokmirror-1.x we didn't normalize paths to be always with a leading '/', so
        # check the manifest for both and make sure we only save the path with a leading /
        if gitdir.lstrip('/') in manifest:
            manifest[gitdir] = manifest.pop(gitdir.lstrip('/'))
            logger.info(' manifest: updated %s', gitdir)
        else:
            logger.info(' manifest: added %s', gitdir)
            manifest[gitdir] = {}
    else:
        logger.info(' manifest: updated %s', gitdir)

    altrepo = grokmirror.get_altrepo(fullpath)
    reference = None
    if manifest[gitdir].get('forkgroup', None) != repoinfo.get('forkgroup', None):
        # Use the first remote listed in the forkgroup as our reference, just so
        # grokmirror-1.x clients continue to work without doing full clones.
        # Without alternates there is no forkgroup to look in, and asking git for
        # the remotes of no repo at all just returns whatever is in the cwd.
        remotes = grokmirror.list_repo_remotes(altrepo, withurl=True) if altrepo else []
        if remotes:
            urls = [x[1] for x in remotes]
            urls.sort()
            reference = grokmirror.fullpath_to_gitdir(toplevel, urls[0])
    else:
        reference = manifest[gitdir].get('reference', None)

    if altrepo and not reference and not repoinfo.get('forkgroup'):
        # Not an objstore repo
        reference = grokmirror.fullpath_to_gitdir(toplevel, altrepo)

    manifest[gitdir].update(repoinfo)
    # Always write a reference entry even if it's None, as grok-1.x clients expect it
    manifest[gitdir]['reference'] = reference


def set_symlinks(manifest: grokmirror.Manifest, toplevel: str, symlinks: list[str]) -> None:
    for symlink in symlinks:
        target = Path(symlink).resolve()
        if not target.exists():
            logger.critical(' manifest: symlink %s is broken, ignored', symlink)
            continue
        relative = grokmirror.fullpath_to_gitdir(toplevel, symlink)
        # A path comparison, not a string search: the old containment test
        # accepted any target whose path merely mentioned the toplevel.
        if not target.is_relative_to(toplevel):
            logger.critical(' manifest: symlink %s points outside toplevel, ignored', relative)
            continue
        tgtgitdir = grokmirror.fullpath_to_gitdir(toplevel, target)
        if tgtgitdir not in manifest:
            logger.critical(' manifest: symlink %s points to %s, which we do not recognize', relative, tgtgitdir)
            continue
        known = manifest[tgtgitdir].get('symlinks')
        if known is None:
            manifest[tgtgitdir]['symlinks'] = [relative]
            logger.info(' manifest: symlinked %s->%s', relative, tgtgitdir)
        elif relative not in known:
            logger.info(' manifest: symlinked %s->%s', relative, tgtgitdir)
            known.append(relative)
        else:
            logger.info(' manifest: %s->%s is already in manifest', relative, tgtgitdir)

        # Now go through all repos and fix any references pointing to the
        # symlinked location. We shouldn't need to do anything with forkgroups.
        for gitdir, repoinfo in list(manifest.items()):
            if gitdir == relative:
                logger.info(' manifest: removing %s (replaced by a symlink)', gitdir)
                manifest.pop(gitdir)
                continue
            if repoinfo.get('reference') == relative:
                logger.info(' manifest: symlinked %s->%s', relative, tgtgitdir)
                repoinfo['reference'] = tgtgitdir


def purge_manifest(manifest: grokmirror.Manifest, toplevel: str, gitdirs: list[str]) -> None:
    for oldrepo in list(manifest):
        if str(grokmirror.gitdir_to_fullpath(toplevel, oldrepo)) not in gitdirs:
            logger.info(' manifest: purged %s (gone)', oldrepo)
            manifest.pop(oldrepo)


def parse_args() -> argparse.Namespace:
    # noinspection PyTypeChecker
    op = argparse.ArgumentParser(
        prog='grok-manifest',
        description='Create or update a manifest file',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    op.add_argument(
        '--cfgfile', dest='cfgfile', default=None, help='Path to grokmirror.conf containing at least a [core] section'
    )
    op.add_argument('-m', '--manifest', dest='manifile', help='Location of manifest.js or manifest.js.gz')
    op.add_argument('-t', '--toplevel', dest='toplevel', help='Top dir where all repositories reside')
    op.add_argument(
        '-l', '--logfile', dest='logfile', default=None, help='When specified, will put debug logs in this location'
    )
    op.add_argument(
        '-n',
        '--use-now',
        dest='usenow',
        action='store_true',
        default=False,
        help='Use current timestamp instead of parsing commits',
    )
    op.add_argument(
        '-c',
        '--check-export-ok',
        dest='check_export_ok',
        action='store_true',
        default=False,
        help='Export only repositories marked as git-daemon-export-ok',
    )
    op.add_argument(
        '-p',
        '--purge',
        dest='purge',
        action='store_true',
        default=False,
        help='Purge deleted git repositories from manifest',
    )
    op.add_argument(
        '-x',
        '--remove',
        dest='remove',
        action='store_true',
        default=False,
        help='Remove repositories passed as arguments from manifest',
    )
    op.add_argument(
        '-y',
        '--pretty',
        dest='pretty',
        action='store_true',
        default=False,
        help='Pretty-print manifest (sort keys and add indentation)',
    )
    op.add_argument(
        '-i',
        '--ignore-paths',
        dest='ignore',
        action='append',
        default=None,
        help='When finding git dirs, ignore these paths (accepts shell-style globbing)',
    )
    op.add_argument(
        '-r',
        '--ignore-refs',
        dest='ignore_refs',
        action='append',
        default=None,
        help='Refs to exclude from fingerprint calculation (e.g. refs/meta/*)',
    )
    op.add_argument(
        '-w',
        '--wait-for-manifest',
        dest='wait',
        action='store_true',
        default=False,
        help='When running with arguments, wait if manifest is not there '
        '(can be useful when multiple writers are writing the manifest)',
    )
    op.add_argument(
        '-o',
        '--fetch-objstore',
        dest='fetchobst',
        action='store_true',
        default=False,
        help='Fetch updates into objstore repo (if used)',
    )
    op.add_argument(
        '-v',
        '--verbose',
        dest='verbose',
        action='store_true',
        default=False,
        help='Be verbose and tell us what you are doing',
    )
    op.add_argument('--version', action='version', version=grokmirror.VERSION)
    op.add_argument('paths', nargs='*', help='Full path(s) to process')

    opts = op.parse_args()

    opts.objstore_uses_plumbing = False
    if opts.cfgfile:
        config = grokmirror.load_config_file(opts.cfgfile)
        if not opts.manifile:
            opts.manifile = config['core'].get('manifest')
        if not opts.toplevel:
            # load_config_file() guarantees [core]toplevel is set
            opts.toplevel = os.path.realpath(config['core']['toplevel'])
        if not opts.logfile:
            opts.logfile = config['core'].get('logfile')

        opts.objstore_uses_plumbing = config['core'].getboolean('objstore_uses_plumbing', False)

        if 'manifest' in config:
            if not opts.ignore:
                opts.ignore = [x.strip() for x in config['manifest'].get('ignore', '').splitlines()]
            if not opts.check_export_ok:
                opts.check_export_ok = config['manifest'].getboolean('check_export_ok', False)
            if not opts.pretty:
                opts.pretty = config['manifest'].getboolean('pretty', False)
            if not opts.fetchobst:
                opts.fetchobst = config['manifest'].getboolean('fetch_objstore', False)

    if not opts.manifile:
        op.error('You must provide the path to the manifest file')
    if not opts.toplevel:
        op.error('You must provide the toplevel path')
    if opts.ignore is None:
        opts.ignore = []

    if not opts.paths and opts.wait:
        op.error('--wait option only makes sense when dirs are passed')

    return opts


def grok_manifest(
    manifile: str,
    toplevel: str,
    paths: list[str] | None = None,
    logfile: str | None = None,
    usenow: bool = False,
    check_export_ok: bool = False,
    purge: bool = False,
    remove: bool = False,
    pretty: bool = False,
    ignore: list[str] | None = None,
    wait: bool = False,
    verbose: bool = False,
    fetchobst: bool = False,
    ignorerefs: list[str] | None = None,
    objstore_uses_plumbing: bool = False,
) -> int:
    loglevel = logging.INFO
    grokmirror.init_logger('manifest', logfile, loglevel, verbose)

    # Monotonic, so a clock adjustment mid-run can't produce a silly duration
    startt = time.monotonic()
    if paths is None:
        paths = []
    if ignore is None:
        ignore = []

    ses = grokmirror.GrokSession()

    with grokmirror.locked_manifest(manifile):
        manifest = grokmirror.read_manifest(manifile, wait=wait)

        toplevel = os.path.realpath(toplevel)

        # If manifest is empty, don't use current timestamp
        if not manifest:
            usenow = False

        if remove and paths:
            # Remove the repos as required, write new manfiest and exit
            for fullpath in paths:
                repo = grokmirror.fullpath_to_gitdir(toplevel, fullpath)
                if repo in manifest:
                    manifest.pop(repo)
                    logger.info(' manifest: removed %s', repo)
                else:
                    # Is it in any of the symlinks?
                    found = False
                    for gitdir, repoinfo in manifest.items():
                        known = repoinfo.get('symlinks')
                        if known and repo in known:
                            found = True
                            known.remove(repo)
                            if not known:
                                repoinfo.pop('symlinks')
                            logger.info(' manifest: removed symlink %s->%s', repo, gitdir)
                    if not found:
                        logger.info(' manifest: %s not in manifest', repo)

            # XXX: need to add logic to make sure we don't break the world
            #      by removing a repository used as a reference for others
            grokmirror.write_manifest(manifile, manifest, pretty=pretty)
            return 0

        gitdirs: list[str] = []

        if purge or not paths or not manifest:
            # We automatically purge when we do a full tree walk
            gitdirs.extend(ses.find_all_gitdirs(toplevel, ignore=ignore, exclude_objstore=True))
            purge_manifest(manifest, toplevel, gitdirs)

        if manifest and paths:
            # limit ourselves to passed dirs only when there is something
            # in the manifest. This precaution makes sure we regenerate the
            # whole file when there is nothing in it or it can't be parsed.
            for apath in paths:
                arealpath = os.path.realpath(apath)
                if apath != arealpath and Path(apath).is_symlink():
                    gitdirs.append(apath)
                else:
                    gitdirs.append(arealpath)

        symlinks = []
        tofetch = set()
        for gitdir in gitdirs:
            # check to make sure this gitdir is ok to export
            if check_export_ok and not Path(gitdir, 'git-daemon-export-ok').exists():
                # is it curently in the manifest?
                repo = grokmirror.fullpath_to_gitdir(toplevel, gitdir)
                if repo in list(manifest):
                    logger.info(' manifest: removed %s (no longer exported)', repo)
                    manifest.pop(repo)

                # XXX: need to add logic to make sure we don't break the world
                #      by removing a repository used as a reference for others
                #      also make sure we clean up any dangling symlinks
                continue

            if Path(gitdir).is_symlink():
                symlinks.append(gitdir)
            else:
                update_manifest(manifest, toplevel, gitdir, usenow, ignorerefs)
                if fetchobst:
                    # Do it after we're done with manifest, to avoid keeping it locked
                    tofetch.add(gitdir)

        if symlinks:
            set_symlinks(manifest, toplevel, symlinks)

        grokmirror.write_manifest(manifile, manifest, pretty=pretty)

    fetched = set()
    for gitdir in tofetch:
        altrepo = grokmirror.get_altrepo(gitdir)
        if altrepo in fetched:
            continue
        if altrepo and grokmirror.is_obstrepo(altrepo):
            try:
                with grokmirror.locked_repo(altrepo, nonblocking=True):
                    logger.info(' manifest: objstore %s -> %s', gitdir, Path(altrepo).name)
                    grokmirror.fetch_objstore_repo(altrepo, gitdir, use_plumbing=objstore_uses_plumbing)
                fetched.add(altrepo)
            except (OSError, grokmirror.GrokLockError):
                # grok-fsck will fetch this one, then
                pass

    elapsed = time.monotonic() - startt
    if len(gitdirs) > 1:
        logger.info('Updated %s records in %ds', len(gitdirs), elapsed)
    else:
        logger.info('Done in %0.2fs', elapsed)

    return 0


def command() -> int:
    try:
        opts = parse_args()

        return grok_manifest(
            opts.manifile,
            opts.toplevel,
            paths=opts.paths,
            logfile=opts.logfile,
            usenow=opts.usenow,
            check_export_ok=opts.check_export_ok,
            purge=opts.purge,
            remove=opts.remove,
            pretty=opts.pretty,
            ignore=opts.ignore,
            wait=opts.wait,
            verbose=opts.verbose,
            fetchobst=opts.fetchobst,
            ignorerefs=opts.ignore_refs,
            objstore_uses_plumbing=opts.objstore_uses_plumbing,
        )
    except grokmirror.GrokError as ex:
        sys.stderr.write(f'ERROR: {ex}\n')
        return 1


if __name__ == '__main__':
    command()
