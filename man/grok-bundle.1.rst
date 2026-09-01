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
  --incremental         Publish a bundle-uri bundle list, adding incremental
                        bundles instead of rewriting one big one (default: False)
  --publish-delay SECONDS
                        How long a new bundle must exist before the list may
                        name it (default: 7200)
  --prune-delay SECONDS
                        How long a bundle must stay on disk after the list
                        stops naming it (default: 86400)
  --max-bundles NUM     Start over with a full bundle once the list grows to
                        this many (default: 30)
  --no-clone-bundle     Do not maintain a clone.bundle symlink for "repo"

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

INCREMENTAL BUNDLES
-------------------
With ``--incremental``, grok-bundle stops rewriting one large bundle every
run and instead maintains a git *bundle list*: a full bundle, plus a small
bundle per run carrying only what is new since the last one. Clients that
speak the bundle-uri protocol download the whole set; the server is told
about the list with::

    git config uploadpack.advertiseBundleURIs true
    git config bundle.mode all
    git config bundle.list.uri https://cdn.example.org/bundles/repo/bundle-list

That is only half of it: a client acts on the advertisement only when it
has opted in with ``transfer.bundleURI = true``, which is off by default.
Without it ``git clone`` ignores the bundles entirely and fetches
everything from the server as before, so do not read a normal-looking clone
as the setup being broken. ``git clone --bundle-uri=<url>`` needs no client
configuration at all, which also makes it the quick way to check a list by
hand.

The output directory gains three things next to the bundles: ``bundle-list``,
which is the file clients fetch; ``clone.bundle``, a symlink to the newest
published full bundle, so "repo" and anything else using the old name keeps
working; and ``.bundlestate``, grok-bundle's own bookkeeping. The URIs in the
list are relative, so git resolves them against wherever the list itself was
fetched from and nothing in the file needs to know your CDN's hostname.

Because ``bundle.mode`` is ``all``, a client needs *every* bundle the list
names, and that shapes the timing. Bundles are usually generated on one host
and served from several others, an rsync hop or two away, so nothing is ever
listed by the run that created it: a new bundle waits out ``--publish-delay``
first. The same reasoning applies at the other end. When a fresh full bundle
is published it retires everything older, but those files stay on disk for
``--prune-delay`` afterwards, because a client may still be working through a
list it fetched a moment ago, and a CDN can keep handing that list out for
longer still. Set both to comfortably more than it takes a change to reach
every mirror.

Bundles cannot be merged, so the list is kept from growing without limit by
starting over: once it reaches ``--max-bundles`` entries, the next run cuts a
new full bundle and lets the rest age out.

``--revlistargs`` does not apply in this mode. The ref set is always explicit
-- every branch and tag, or what ``--max-ref-age`` leaves of them -- because
each incremental bundle has to be built against the exact branch tips the
previous one ended at.

EXAMPLES
--------

    grok-bundle -c grokmirror.conf -o /var/www/bundles -i /pub/scm/linux/kernel/git/torvalds/linux.git /pub/scm/linux/kernel/git/stable/linux.git /pub/scm/linux/kernel/git/next/linux-next.git

    grok-bundle -c grokmirror.conf -o /var/www/bundles -s 20 --incremental --max-ref-age 365 -i /pub/scm/linux/kernel/git/stable/linux.git

SEE ALSO
--------
* grok-pull(1)
* grok-manifest(1)
* grok-fsck(1)
* git(1)

SUPPORT
-------
Email tools@linux.kernel.org.
