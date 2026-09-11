GROK-MANIFEST
=============
---------------------------------------
Create manifest for use with grokmirror
---------------------------------------

:Author:    mricon@kernel.org
:Date:      2020-08-14
:Copyright: The Linux Foundation and contributors
:License:   GPLv3+
:Version:   2.0.0
:Manual section: 1

SYNOPSIS
--------
    grok-manifest [opts] -m manifest.js[.gz] -t /path [/path/to/bare.git]

DESCRIPTION
-----------
Call grok-manifest from a git post-update or post-receive hook to create
the latest repository manifest. This manifest file is downloaded by
mirroring systems (if manifest is newer than what they already have) and
used to only clone/pull the repositories that have changed since the
grok-pull's last run.

OPTIONS
-------
  --version             show program's version number and exit
  -h, --help            show this help message and exit
  --cfgfile=CFGFILE     Path to grokmirror.conf containing a [manifest] section
  -m MANIFILE, --manifest=MANIFILE
                        Location of manifest.js or manifest.js.gz
  -t TOPLEVEL, --toplevel=TOPLEVEL
                        Top dir where all repositories reside
  -l LOGFILE, --logfile=LOGFILE
                        When specified, will put debug logs in this location
  -c, --check-export-ok
                        Honor the git-daemon-export-ok magic file and
                        do not export repositories not marked as such
  -n, --use-now         Use current timestamp instead of parsing commits
  -p, --purge           Purge deleted git repositories from manifest
  -x, --remove          Remove repositories passed as arguments from
                        the manifest file
  -y, --pretty          Pretty-print the generated manifest (sort repos
                        and add indentation). This is much slower, so
                        should be used with caution on large
                        collections.
  -w, --wait-for-manifest
                        When running with arguments, wait if manifest is not
                        there (can be useful when multiple writers are writing
                        to the manifest file via NFS)
  -i IGNORE, --ignore-paths=IGNORE
                        When finding git dirs, ignore these paths (can be used
                        multiple times, accepts shell-style globbing)
  -o, --fetch-objstore  Fetch updates into objstore repo (if used)
  -v, --verbose         Be verbose and tell us what you are doing
  --config-check        Check the configuration file and exit, reporting
                        every problem found
  --json                With --config-check, write the report as a JSON
                        object instead of text
  --no-network          With --config-check, skip the checks that contact
                        the remote site

You can set some of these options in a config file that you can pass via
``--cfgfile`` option. See example grokmirror.conf file for
documentation. Values passed via cmdline flags will override the
corresponding config file values.

CHECKING THE CONFIGURATION
--------------------------
Running with ``--config-check`` reads the configuration file, reports
every problem it can find, and exits without doing anything else. The
check writes nothing at all: no repository is touched, no manifest is
written, not even a log file is opened.

Grok-manifest answers for the ``[core]`` and ``[manifest]`` sections,
and needs ``--cfgfile`` to say which file to check, since every other
option it takes comes from the command line. Options that belong to
another command are left alone, so if the same file also configures
grok-pull or grok-fsck, run their checks against it too.

None of the options grok-manifest checks name a remote site, so
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

EXAMPLES
--------
The examples assume that the repositories are located in
``/var/lib/gitolite3/repositories``.

Initial manifest generation::

    /usr/bin/grok-manifest -m /var/www/html/manifest.js.gz \
        -t /var/lib/gitolite3/repositories

Inside the git hook::

    /usr/bin/grok-manifest -m /var/www/html/manifest.js.gz \
        -t /var/lib/gitolite3/repositories -n `pwd`

To purge deleted repositories from the manifest, use the ``-p`` flag
when running from cron::

    /usr/bin/grok-manifest -m /var/www/html/manifest.js.gz \
        -t /var/lib/gitolite3/repositories -p

You can also add it to the gitolite's ``D`` command using the ``-x`` flag::

    /usr/bin/grok-manifest -m /var/www/html/manifest.js.gz \
        -t /var/lib/gitolite3/repositories \
        -x $repo.git

To troubleshoot potential problems, you can pass ``-l`` parameter to
grok-manifest, just make sure the user executing the hook command (user
git or gitolite, for example) is able to write to that location::

    /usr/bin/grok-manifest -m /var/www/html/manifest.js.gz \
        -t /var/lib/gitolite3/repositories \
        -l /var/log/grokmirror/grok-manifest.log -n `pwd`

SEE ALSO
--------
* grok-pull(1)
* git(1)

SUPPORT
-------
Email tools@linux.kernel.org.
