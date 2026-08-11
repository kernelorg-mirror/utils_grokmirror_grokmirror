#
# This is a ready-made post_update_hook script for piping messages from
# mirrored public-inbox repositories to arbitrary commands (e.g. procmail).
#

from __future__ import annotations

__author__ = 'Konstantin Ryabitsev <konstantin@linuxfoundation.org>'

import fnmatch
import logging
import os
import shlex
import sys
from pathlib import Path

import grokmirror

# default basic logger. We override it later.
logger = logging.getLogger(__name__)


def git_get_message_from_pi(fullpath: str, commit_id: str) -> bytes:
    logger.debug('Getting %s:m from %s', commit_id, fullpath)
    args = ['show', f'{commit_id}:m']
    ecode, out, err = grokmirror.run_git_command(fullpath, args, decode=False)
    if ecode > 0:
        logger.debug('Could not get the message, error below')
        logger.debug(err.decode())
        raise grokmirror.GrokMissingRevisionsError(f'Could not find {commit_id} in {fullpath}')
    return out


def git_get_new_revs(fullpath: str, pipelast: int | None = None) -> list[tuple[str, str]]:
    if pipelast:
        rev_range = f'-n {pipelast}'
    else:
        latest = Path(fullpath, 'pi-piper.latest').read_text(encoding='utf-8').strip()
        rev_range = f'{latest}..'

    # Terminate each record with a NUL instead of relying on a newline. These
    # subjects are email Subject: headers, so they can carry anything a mail
    # client saw fit to put there -- \v, \f, U+0085 and friends all count as
    # line breaks to str.splitlines(), and git escapes none of them. A NUL is
    # the one byte that cannot appear inside the record.
    args = ['rev-list', '--pretty=format:%H %s%x00', '--reverse', rev_range, 'master']
    ecode, out, _err = grokmirror.run_git_command(fullpath, args)
    if ecode > 0:
        raise grokmirror.GrokMissingRevisionsError(f'Could not iterate {rev_range} in {fullpath}')

    newrevs = []
    for record in out.split('\0'):
        # --pretty=format: puts a "commit <sha>" header line ahead of every
        # record, and --no-commit-header only suppresses it from git 2.33 on,
        # so keep just the last line: the one we asked for.
        entry = record.rsplit('\n', 1)[-1]
        if not entry:
            continue
        commit_id, _, logmsg = entry.partition(' ')
        logger.debug('commit_id=%s, subject=%s', commit_id, logmsg)
        newrevs.append((commit_id, logmsg))

    return newrevs


def reshallow(repo: str, commit_id: str) -> int:
    Path(repo, 'shallow').write_text(f'{commit_id}\n', encoding='utf-8')
    logger.info('   prune: %s ', repo)
    ecode, _out, _err = grokmirror.run_git_command(repo, ['gc', '--prune=now'])
    return ecode


def init_piper_tracking(repo: str, shallow: bool) -> bool:
    logger.info('Initial setup for %s', repo)
    args = ['rev-list', '-n', '1', 'master']
    ecode, out, _err = grokmirror.run_git_command(repo, args)
    if ecode > 0 or not out:
        logger.info('Could not list revs in %s', repo)
        return False
    # Just write latest into the tracking file and return
    latest = out.strip()
    Path(repo, 'pi-piper.latest').write_text(latest, encoding='utf-8')
    if shallow:
        reshallow(repo, latest)
    return True


def run_pi_repo(
    repo: str, pipedef: str, dryrun: bool = False, shallow: bool = False, pipelast: int | None = None
) -> None:
    logger.info('Checking %s', repo)
    args = shlex.split(pipedef)
    if not os.access(args[0], os.X_OK):
        logger.critical('Cannot execute %s', pipedef)
        sys.exit(1)

    statf = Path(repo, 'pi-piper.latest')
    if not statf.exists():
        if dryrun:
            logger.info('Would have set up piper for %s [DRYRUN]', repo)
            return
        if not init_piper_tracking(repo, shallow):
            logger.critical('Unable to set up piper for %s', repo)
        return

    try:
        revlist = git_get_new_revs(repo, pipelast=pipelast)
    except grokmirror.GrokMissingRevisionsError:
        # this could have happened if the public-inbox repository
        # got rebased, e.g. due to GDPR-induced history editing.
        # For now, bluntly handle this by getting rid of our
        # status file and pretending we just started new.
        # XXX: in reality, we could handle this better by keeping track
        #      of the subject line of the latest message we processed, and
        #      then going through history to find the new commit-id of that
        #      message. Unless, of course, that's the exact message that got
        #      deleted in the first place. :/
        #      This also makes it hard with shallow repos, since we'd have
        #      to unshallow them first in order to find that message.
        logger.critical('Assuming the repository got rebased, dropping all history.')
        statf.unlink()
        if not dryrun:
            init_piper_tracking(repo, shallow)
        revlist = git_get_new_revs(repo)

    if not revlist:
        return

    logger.info('Processing %s commits', len(revlist))

    latest_good = None
    ecode = 0
    for commit_id, subject in revlist:
        try:
            msgbytes = git_get_message_from_pi(repo, commit_id)
            if dryrun:
                logger.info('  piping: %s (%s b) [DRYRUN]', commit_id, len(msgbytes))
                logger.debug(' subject: %s', subject)
            else:
                logger.info('  piping: %s (%s b)', commit_id, len(msgbytes))
                logger.debug(' subject: %s', subject)
                ecode, _out, err = grokmirror.run_shell_command(args, stdin=msgbytes)
                if ecode > 0:
                    logger.info('Error running %s', pipedef)
                    logger.info(err)
                    break
                latest_good = commit_id
        except grokmirror.GrokMissingRevisionsError:
            logger.info('Skipping %s', commit_id)

    if latest_good and not dryrun:
        statf.write_text(latest_good, encoding='utf-8')
        logger.info('Wrote %s', statf)
        if ecode == 0 and shallow:
            reshallow(repo, latest_good)

    sys.exit(ecode)


def command() -> None:
    import argparse
    from configparser import ConfigParser, ExtendedInterpolation

    # noinspection PyTypeChecker
    op = argparse.ArgumentParser(
        prog='grok-pi-piper',
        description='Pipe new messages from public-inbox repositories to arbitrary commands',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    op.add_argument(
        '-v', '--verbose', action='store_true', default=False, help='Be verbose and tell us what you are doing'
    )
    op.add_argument(
        '-d',
        '--dry-run',
        dest='dryrun',
        action='store_true',
        default=False,
        help='Do a dry-run and just show what would be done',
    )
    op.add_argument('-c', '--config', required=True, help='Location of the configuration file')
    op.add_argument(
        '-l',
        '--pipe-last',
        dest='pipelast',
        type=int,
        default=None,
        help='Force pipe last NN messages in the list, regardless of tracking',
    )
    op.add_argument('repo', help='Full path to foo/git/N.git public-inbox repository')
    op.add_argument('--version', action='version', version=grokmirror.VERSION)

    opts = op.parse_args()

    # This used to test the expanded path for truthiness, which -c being a
    # required argument already guarantees, so a config file that was not
    # there read as an empty one and the run ended quietly at the "no pipe
    # defined" exit below. Ask the filesystem instead.
    cfgfile = Path(opts.config).expanduser()
    if not cfgfile.exists():
        sys.stderr.write(f'ERROR: File does not exist: {cfgfile}\n')
        sys.exit(1)
    config = ConfigParser(interpolation=ExtendedInterpolation())
    config.read(cfgfile, encoding='utf-8')

    # Find out the section that we want from the config file
    section = 'DEFAULT'
    for sectname in config.sections():
        if fnmatch.fnmatch(opts.repo, f'*/{sectname}/git/*.git'):
            section = sectname

    pipe = config[section].get('pipe')
    if not pipe or pipe == 'None':
        # Quick exit. Also covers a config with no pipe defined at all, which
        # would otherwise have shlex read the pipe command from stdin.
        sys.exit(0)

    logfile = config[section].get('log')
    loglevel = logging.DEBUG if config[section].get('loglevel') == 'debug' else logging.INFO

    shallow = config[section].getboolean('shallow', False)

    # This used to say 'pull', so grok-pi-piper's log entries were labeled
    # as coming from grok-pull.
    grokmirror.init_logger('pi-piper', logfile, loglevel, opts.verbose)

    try:
        run_pi_repo(opts.repo, pipe, dryrun=opts.dryrun, shallow=shallow, pipelast=opts.pipelast)
    except grokmirror.GrokError as ex:
        sys.stderr.write(f'ERROR: {ex}\n')
        sys.exit(1)


if __name__ == '__main__':
    command()
