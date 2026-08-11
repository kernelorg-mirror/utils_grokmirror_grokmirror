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

import falcon

# Some sanity defaults
MAX_PROJ_LEN = 32
MAX_REPO_LEN = 1024


# noinspection PyBroadException
class PubsubListener:
    # Signature is dictated by falcon, which passes both positionally.
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:  # noqa: ARG002
        resp.status = falcon.HTTP_200
        resp.text = "We don't serve GETs here\n"

    def on_post(self, req: falcon.Request, resp: falcon.Response) -> None:
        if not req.content_length:
            resp.status = falcon.HTTP_500
            resp.text = 'Payload required\n'
            return

        try:
            # bounded_stream, not stream: reading the raw stream can block
            # waiting for an EOF the client never sends, and calling read() with
            # no size is not something a WSGI server has to support.
            doc = json.load(req.bounded_stream)
        except ValueError:
            # JSONDecodeError and UnicodeDecodeError are both ValueErrors
            resp.status = falcon.HTTP_500
            resp.text = 'Failed to parse payload as json\n'
            return

        try:
            proj = doc['message']['attributes']['proj']
            repo = doc['message']['attributes']['repo']
        except (KeyError, TypeError):
            resp.status = falcon.HTTP_500
            resp.text = 'Not a pubsub v1 payload\n'
            return

        if len(proj) > MAX_PROJ_LEN or len(repo) > MAX_REPO_LEN:
            resp.status = falcon.HTTP_500
            resp.text = 'Repo or project value too long\n'
            return

        # Proj shouldn't contain slashes or whitespace
        if re.search(r'[\s/]', proj):
            resp.status = falcon.HTTP_500
            resp.text = 'Invalid characters in project name\n'
            return

        # Repo shouldn't contain whitespace
        if re.search(r'\s', proj):
            resp.status = falcon.HTTP_500
            resp.text = 'Invalid characters in repo name\n'
            return

        confdir = os.environ.get('GROKMIRROR_CONFIG_DIR', '/etc/grokmirror')
        cfgfile = os.path.join(confdir, f'{proj}.conf')
        if not os.access(cfgfile, os.R_OK):
            resp.status = falcon.HTTP_500
            resp.text = 'Invalid project name\n'
            return
        config = ConfigParser(interpolation=ExtendedInterpolation())
        config.read(cfgfile, encoding='utf-8')
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
                client.send(repo.encode())
        except OSError:
            resp.status = falcon.HTTP_500
            resp.text = 'Unable to communicate with the socket\n'
            return

        resp.status = falcon.HTTP_204


app = falcon.App()
pl = PubsubListener()
app.add_route('/pubsub_v1', pl)
