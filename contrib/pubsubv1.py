#!/usr/bin/env python3
# Implements a Google pubsub v1 push listener, see:
# https://cloud.google.com/pubsub/docs/push
#
# In order to work, grok-pull must be running as a daemon service with
# the "socket" option enabled in the configuration.
#
# The pubsub message should contain two attributes:
# {
#   "message": {
#     "attributes": {
#       "proj": "projname",
#       "repo": "/path/to/repo.git"
#     }
#   }
# }
#
# "proj" value should map to a "$proj.conf" file in /etc/grokmirror
#        (you can override that default via the GROKMIRROR_CONFIG_DIR env var).
# "repo" value should match a repo defined in the manifest file as understood
#        by the running grok-pull daemon (it will ignore anything else)
#
# Any other attributes or the "data" field are ignored.

import json
import os
import re
import socket
from configparser import ConfigParser, ExtendedInterpolation
from configparser import Error as ConfigParserError
from pathlib import Path

import falcon

# Some sanity defaults
MAX_PROJ_LEN = 32
MAX_REPO_LEN = 1024

# Proj becomes part of a filename we open, so allow it a known-good set of
# characters instead of trying to name every bad one. This rules out slashes,
# whitespace and null bytes in one go -- the last of these makes os.access()
# raise, and is not whitespace, so a "no whitespace" check lets it through.
PROJ_PATT = re.compile(r'^[\w.-]+$')


class PubsubListener:
    # There is deliberately no on_get(). Falcon answers anything we do not
    # implement with a 405 and a correct Allow header, which is a better
    # answer than a 200 whose body says no.
    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.content_length:
            resp.status = falcon.HTTP_400
            resp.text = 'Payload required\n'
            return

        try:
            # bounded_stream, not stream: reading the raw stream can block
            # waiting for an EOF the client never sends, and calling read() with
            # no size is not something a WSGI server has to support.
            doc = json.load(req.bounded_stream)
        except ValueError:
            # JSONDecodeError and UnicodeDecodeError are both ValueErrors
            resp.status = falcon.HTTP_400
            resp.text = 'Failed to parse payload as json\n'
            return

        try:
            proj = doc['message']['attributes']['proj']
            repo = doc['message']['attributes']['repo']
        except (KeyError, TypeError):
            resp.status = falcon.HTTP_400
            resp.text = 'Not a pubsub v1 payload\n'
            return

        # Json gives us whatever type was on the wire, and len() on a number
        # raises rather than returning an unhelpful answer
        if not isinstance(proj, str) or not isinstance(repo, str):
            resp.status = falcon.HTTP_400
            resp.text = 'Repo and project must be strings\n'
            return

        if len(proj) > MAX_PROJ_LEN or len(repo) > MAX_REPO_LEN:
            resp.status = falcon.HTTP_400
            resp.text = 'Repo or project value too long\n'
            return

        if not PROJ_PATT.search(proj):
            resp.status = falcon.HTTP_400
            resp.text = 'Invalid characters in project name\n'
            return

        # Repo shouldn't contain whitespace. A newline in particular would let
        # a single message queue several repos, since the daemon reads its
        # socket a line at a time.
        if re.search(r'\s', repo):
            resp.status = falcon.HTTP_400
            resp.text = 'Invalid characters in repo name\n'
            return

        confdir = os.environ.get('GROKMIRROR_CONFIG_DIR', '/etc/grokmirror')
        cfgfile = Path(confdir, f'{proj}.conf')
        if not os.access(cfgfile, os.R_OK):
            resp.status = falcon.HTTP_400
            resp.text = 'Invalid project name\n'
            return
        config = ConfigParser(interpolation=ExtendedInterpolation())
        try:
            config.read(cfgfile, encoding='utf-8')
        except (ConfigParserError, UnicodeDecodeError):
            # Ours to fix, not the sender's, so this one really is a 500
            resp.status = falcon.HTTP_500
            resp.text = 'Invalid project configuration (cannot be parsed)\n'
            return
        sockfile = config['pull'].get('socket') if 'pull' in config else None
        if not sockfile:
            resp.status = falcon.HTTP_500
            resp.text = 'Invalid project configuration (no socket defined)\n'
            return
        if not os.access(sockfile, os.W_OK):
            resp.status = falcon.HTTP_500
            resp.text = 'Invalid project configuration (socket does not exist or is not writable)\n'
            return

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(sockfile)
                # The daemon reads with readline(), so terminate the repo name
                # rather than relying on the close to end it. sendall(), because
                # send() is free to write only part of what we handed it.
                client.sendall(f'{repo}\n'.encode())
        except OSError:
            resp.status = falcon.HTTP_500
            resp.text = 'Unable to communicate with the socket\n'
            return

        resp.status = falcon.HTTP_204


app = falcon.App()
pl = PubsubListener()
app.add_route('/pubsub_v1', pl)
