#!/bin/bash
# This is a command to install in gitolite's local-code.
# Don't forget to enable it via .gitolite.rc
#
# It is invoked over ssh by grok-get-gl-manifest.sh on the replica, and
# follows grokmirror's manifest_command contract:
#   exit 0   -- the manifest is on stdout
#   exit 1   -- an error message is on stdout (on purpose, not on stderr)
#   exit 127 -- nothing on stdout, the manifest has not changed
#
# Change this to where grok-manifest is writing manifest.js
MANIFILE="/var/www/html/grokmirror/manifest.js.gz"

if [[ -z "$GL_USER" ]]; then
    echo "ERROR: GL_USER is unset. Run me via ssh, please."
    exit 1
fi

# Make sure we only accept credential replication from the mirrors.
# GL_USER has to be cleared, or 'gitolite mirror list' refuses to run and
# prints its usage message instead. Mirrors authenticate as server-<host>,
# which is the naming gitolite's mirroring trigger requires.
AOK=""
for MIRROR in $(GL_USER='' gitolite mirror list copies gitolite-admin); do
    if [[ $GL_USER == "server-${MIRROR}" ]]; then
        AOK="yes"
        break
    fi
done

if [[ -z "$AOK" ]]; then
    echo "You are not allowed to do this"
    exit 1
fi

if [[ ! -s $MANIFILE ]]; then
    echo "Manifest file not found"
    exit 1
fi

# The argument comes from the replica, and [[ -le ]] evaluates its operands
# as arithmetic, where an unvalidated string can assign to shell variables
# (and, if it ever reaches us without gitolite-shell's character filter in
# front, run commands via an array subscript). Insist on a plain integer.
R_LASTMOD=$1
if [[ ! $R_LASTMOD =~ ^[0-9]+$ ]]; then
    R_LASTMOD=0
fi

L_LASTMOD=$(stat --printf='%Y' "$MANIFILE")
if [[ ! $L_LASTMOD =~ ^[0-9]+$ ]]; then
    echo "Could not get the modification time of the manifest file"
    exit 1
fi

if [[ $L_LASTMOD -le $R_LASTMOD ]]; then
    exit 127
fi

# Hand over to the decompressor, so a failure part-way through becomes our
# exit code instead of being masked by a successful exit
if [[ $MANIFILE == *.gz ]]; then
    exec zcat "$MANIFILE"
fi
exec cat "$MANIFILE"
