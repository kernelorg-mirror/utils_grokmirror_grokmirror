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

import argparse
import logging
import sys
from collections.abc import Collection
from pathlib import Path

import grokmirror

# default basic logger. We override it later.
logger = logging.getLogger(__name__)


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


def generate_bundles(
    config: grokmirror.GrokConfigParser,
    outdir: str,
    gitargs: str,
    revlistargs: str,
    maxsize: int,
    include: Collection[str],
) -> int:
    # uses advisory lock, so its safe even if we die unexpectedly
    # load_config_file() guarantees both of these are set
    manifest = grokmirror.read_manifest(config['core']['manifest'])
    toplevel = Path(config['core']['toplevel']).resolve()
    # An empty string means "no extra arguments", and str.split() already
    # returns an empty list for it -- but only if we don't skip the split.
    git_args = gitargs.split()
    revlist_args = revlistargs.split()
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

        repofpr = grokmirror.get_repo_fingerprint(str(toplevel), bundlename)
        logger.debug('%s fingerprint is %s', bundlename, repofpr)
        if not repofpr:
            # Either the repo is gone or it has no refs at all. Either way there
            # is nothing to bundle, and no fingerprint to record next to it.
            logger.info('  skipped: %s (no refs to bundle)', bundlename)
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

        fullargs = git_args + ['bundle', 'create', str(bfile)] + revlist_args
        logger.debug('Full git args: %s', fullargs)
        logger.info(' generate: %s', bfile)
        ecode, _out, _err = grokmirror.run_git_command(fullpath, fullargs)

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
) -> int:
    config = grokmirror.load_config_file(cfgfile)

    logfile = config['core'].get('log', None)
    loglevel = logging.DEBUG if config['core'].get('loglevel', 'info') == 'debug' else logging.INFO

    grokmirror.init_logger('bundle', logfile, loglevel, verbose)

    return generate_bundles(config, outdir, gitargs, revlistargs, maxsize, include)


def command() -> None:
    opts = parse_args()

    try:
        retval = grok_bundle(
            opts.config, opts.outdir, opts.gitargs, opts.revlistargs, opts.maxsize, opts.include, verbose=opts.verbose
        )
    except grokmirror.GrokError as ex:
        sys.stderr.write(f'ERROR: {ex}\n')
        retval = 1

    sys.exit(retval)


if __name__ == '__main__':
    command()
