GROK-BUNDLE
===========
-------------------------------------------------
Create clone.bundle files for use with "repo"
-------------------------------------------------

:Author:    mricon@kernel.org
:Date:      2020-09-04
:Copyright: The Linux Foundation and contributors
:License:   GPLv3+
:Version:   2.0.0
:Manual section: 1

SYNOPSIS
--------
    grok-bundle [options] -c grokmirror.conf -o path

DESCRIPTION
-----------
Android's "repo" tool will check for the presence of clone.bundle files
before performing a fresh git clone. This is done in order to offload
most of the git traffic to a CDN and reduce the load on git servers
themselves.

This command will generate clone.bundle files in a hierarchy expected by
repo. You can then sync the output directory to a CDN service.

OPTIONS
-------

  -h, --help            show this help message and exit
  -v, --verbose         Be verbose and tell us what you are doing (default: False)
  -c CONFIG, --config CONFIG
                        Location of the configuration file
  -o OUTDIR, --outdir OUTDIR
                        Location where to store bundle files
  -g GITARGS, --gitargs GITARGS
                        extra args to pass to git (default: -c core.compression=9)
  -r REVLISTARGS, --revlistargs REVLISTARGS
                        Rev-list args to use (default: --branches HEAD)
  -s MAXSIZE, --maxsize MAXSIZE
                        Maximum size of git repositories to bundle (in GiB) (default: 2)
  -i, --include INCLUDE
                        List repositories to bundle (accepts shell globbing) (default: \*)
  --max-ref-age DAYS
                        Bundle only branches whose tip is newer than this, plus
                        the tags on them (0 disables) (default: 0)

REF AGE FILTERING
-----------------
By default every branch goes into the bundle, which on a long-lived
repository means paying for history nobody clones any more. ``--max-ref-age``
keeps only the branches whose tip commit is younger than the given number of
days.

Tags are handled separately, and the difference matters. A tag is included
when it is reachable from one of the surviving branches, not when the tag
itself looks recent: a tag object created yesterday can point at ancient
history on a branch the age limit just dropped, and including it would pull
that entire branch back into the bundle. Reachability costs one walk per
surviving branch, so it scales with how many branches are kept rather than
with how many tags the repository has.

On the kernel stable tree, a 365-day limit keeps 12 of 116 branches and 2764
of 5751 tags, and takes the bundle from 6.14 GiB to 4.81 GiB. Filtering the
tags by age instead of by reachability would have given back the entire
saving.

``HEAD`` is carried into the bundle whenever it points at a branch that
survived the filter, so ``git clone`` on the bundle file still checks
something out. Because the ref set is chosen explicitly, ``--revlistargs``
does not apply once ``--max-ref-age`` is in use.

EXAMPLES
--------

    grok-bundle -c grokmirror.conf -o /var/www/bundles -i /pub/scm/linux/kernel/git/torvalds/linux.git /pub/scm/linux/kernel/git/stable/linux.git /pub/scm/linux/kernel/git/next/linux-next.git

SEE ALSO
--------
* grok-pull(1)
* grok-manifest(1)
* grok-fsck(1)
* git(1)

SUPPORT
-------
Email tools@linux.kernel.org.
