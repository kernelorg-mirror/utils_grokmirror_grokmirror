#!/bin/bash
# This is executed by grok-pull if manifest_command is defined.
# You should install the other file as one of your commands in local-code
# and enable it in .gitolite.rc
#
# NOTE: this assumes the replica and the primary agree on the time. The
# timestamp we record is from this host's clock, but the primary compares
# it against the manifest's mtime on its own filesystem, so a replica
# running ahead makes every check look unchanged and mirroring quietly
# stalls. Both ends being on NTP is expected and not verified here.

PRIMARY=$(gitolite mirror list master gitolite-admin)
if [[ -z $PRIMARY ]]; then
    echo "Could not work out the primary from 'gitolite mirror list'"
    exit 1
fi

ADMIN_BASE=$(gitolite query-rc GL_ADMIN_BASE)
if [[ -z $ADMIN_BASE ]]; then
    echo "Could not work out GL_ADMIN_BASE from 'gitolite query-rc'"
    exit 1
fi

STATEFILE="${ADMIN_BASE}/.${PRIMARY}.manifest.lastupd"
GL_COMMAND=get-grok-manifest

LASTUPD=0
if [[ -s $STATEFILE ]] && [[ $1 != '--force' ]]; then
    LASTUPD=$(cat "$STATEFILE")
fi
# Taken before the ssh, so that anything published while we are fetching is
# picked up next time round. Assumes clocks are in sync -- see the note above.
NOWSTAMP=$(date +'%s')

ERRFILE=$(mktemp) || exit 1
trap 'rm -f "$ERRFILE"' EXIT

# LASTUPD is meant to expand here, on the replica, before ssh sends it
# shellcheck disable=SC2029
ssh "$PRIMARY" "$GL_COMMAND" "$LASTUPD" 2>"$ERRFILE"
ECODE=$?

# Exit 127 tells grok-pull that the manifest is unchanged, but it is also
# what ssh returns when the remote command does not exist. Telling the two
# apart matters, because the second one would otherwise stall mirroring
# indefinitely without ever reporting an error.
if [[ $ECODE -eq 127 ]] && [[ -s $ERRFILE ]]; then
    echo "Running ${GL_COMMAND} on ${PRIMARY} failed:"
    cat "$ERRFILE"
    exit 1
fi

if [[ $ECODE -ne 0 ]]; then
    # grok-pull wants to see diagnostics on stdout, ssh writes them to stderr
    cat "$ERRFILE"
    exit $ECODE
fi

if ! echo "$NOWSTAMP" > "$STATEFILE"; then
    echo "Could not record the timestamp in ${STATEFILE}"
    exit 1
fi

exit 0
