GROK-FSCK
=========
-------------------------------------------------------
Optimize mirrored repositories and check for corruption
-------------------------------------------------------

:Author:    mricon@kernel.org
:Date:      2020-08-14
:Copyright: The Linux Foundation and contributors
:License:   GPLv3+
:Version:   2.0.0
:Manual section: 1

SYNOPSIS
--------
    grok-fsck -c /path/to/grokmirror.conf

DESCRIPTION
-----------
Git repositories should be routinely repacked and checked for
corruption. This utility will perform the necessary optimizations and
report any problems to the email defined via fsck.report_to ('root' by
default). It should run weekly from cron or from the systemd timer (see
contrib).

Please examine the example grokmirror.conf file for various things you
can tweak.

OPTIONS
-------
  --version             show program's version number and exit
  -h, --help            show this help message and exit
  -v, --verbose         Be verbose and tell us what you are doing
  -f, --force           Force immediate run on all repositories.
  -c CONFIG, --config=CONFIG
                        Location of fsck.conf
  --repack-only         Only find and repack repositories that need
                        optimizing (nightly run mode)
  --connectivity        (Assumes --force): Run git fsck on all repos,
                        but only check connectivity
  --repack-all-quick    (Assumes --force): Do a quick repack of all repos
  --repack-all-full     (Assumes --force): Do a full repack of all repos
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

Grok-fsck answers for the ``[core]`` and ``[fsck]`` sections. Options
that belong to another command are left alone, so if the same file also
configures grok-pull, run ``grok-pull --config-check`` against it too.

None of the options grok-fsck checks name a remote site, so
``--no-network`` is accepted here but has nothing to skip. It is
grok-pull that has remote URLs to probe.

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

SEE ALSO
--------
* grok-manifest(1)
* grok-pull(1)
* git(1)

SUPPORT
-------
Email tools@linux.kernel.org.
