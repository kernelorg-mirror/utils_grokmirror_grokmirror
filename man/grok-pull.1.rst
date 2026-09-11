GROK-PULL
=========
--------------------------------------
Clone or update local git repositories
--------------------------------------

:Author:    mricon@kernel.org
:Date:      2020-08-14
:Copyright: The Linux Foundation and contributors
:License:   GPLv3+
:Version:   2.0.0
:Manual section: 1

SYNOPSIS
--------
  grok-pull -c /path/to/grokmirror.conf

DESCRIPTION
-----------
Grok-pull is the main tool for replicating repository updates from the
grokmirror primary server to the mirrors.

Grok-pull has two modes of operation -- onetime and continous
(daemonized). In one-time operation mode, it downloads the latest
manifest and applies any outstanding updates. If there are new
repositories or changes in the existing repositories, grok-pull will
perform the necessary git commands to clone or fetch the required data
from the master. Once all updates are applied, it will write its own
manifest and exit. In this mode, grok-pull can be run manually or from
cron.

In continuous operation mode (when run with -o), grok-pull will continue
running after all updates have been applied and will periodically
re-download the manifest from the server to check for new updates. For
this to work, you must set pull.refresh in grokmirror.conf to the amount
of seconds you would like it to wait between refreshes.

If pull.socket is specified, grok-pull will also listen on a socket for
any push updates (relative repository path as present in the manifest
file, terminated with newlines). This can be used for pubsub
subscriptions (see contrib).

OPTIONS
-------
  --version             show program's version number and exit
  -h, --help            show this help message and exit
  -v, --verbose         Be verbose and tell us what you are doing
  -n, --no-mtime-check  Run without checking manifest mtime.
  -o, --continuous      Run continuously (no effect if refresh is not set)
  -c CONFIG, --config=CONFIG
                        Location of the configuration file
  -p, --purge           Remove any git trees that are no longer in manifest.
  --force-purge         Force purge operation despite significant repo deletions
  --config-check        Check the configuration file and exit, reporting
                        every problem found
  --json                With --config-check, write the report as a JSON
                        object instead of text
  --no-network          With --config-check, skip the checks that contact
                        the remote site

CHECKING THE CONFIGURATION
--------------------------
Running with ``--config-check`` reads the configuration file, reports
every problem it can find, and exits without doing anything else. The
check writes nothing at all: no repository is touched, no manifest is
written, not even a log file is opened.

Grok-pull answers for the ``[core]``, ``[remote]`` and ``[pull]``
sections. Options that belong to another command are left alone, so if
the same file also configures grok-fsck, run ``grok-fsck
--config-check`` against it too.

Some of the checks contact the remote site, to see whether the manifest
URL answers at all. Pass ``--no-network`` to skip those, which is what
you want when checking a configuration on a host that cannot reach the
primary yet.

Problems are reported as either errors or warnings. An error is
something that will stop the command from working, such as a missing
``[core] toplevel``, an option name that is not spelled the way
grokmirror spells it, or a directory that cannot be written to. A
warning is something that looks wrong but may well be deliberate, such
as a glob that matches none of the repositories you are currently
mirroring. The command exits with 1 when there was at least one error
and with 0 otherwise, so warnings never fail the check.

Whether a path is writable is a question about a user, and it is
answered for the user running the check. If the command runs from
cron or from a systemd unit as some other user, run the check as that
user as well, or it will cheerfully tell you a directory is writable
when it is not. Every report about a path names the user it was
checked as, and the summary ends with a line saying who that was.

With ``--json``, the same report is written as a single JSON object
instead of as text, with a ``diagnostics`` list and a ``summary``
giving the number of errors and warnings. The exit code is the same
either way, so scripts can use whichever is easier to read.

EXAMPLES
--------
Use grokmirror.conf and modify it to reflect your needs. The example
configuration file is heavily commented. To invoke, run::

  grok-pull -v -c /path/to/grokmirror.conf

SEE ALSO
--------
* grok-manifest(1)
* grok-fsck(1)
* git(1)

SUPPORT
-------
Please email tools@linux.kernel.org.
