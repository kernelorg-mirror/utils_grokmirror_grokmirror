=============
Configuration
=============

Grokmirror uses a single INI-style configuration file per mirrored
collection. All the tools read the same file and pick out the sections
they care about.

The example ``grokmirror.conf`` shipped with the source is heavily
commented, and is the place to start. This page is the reference.

File Location
=============

There is no default path. Every tool that needs a config takes ``-c``
(``--cfgfile`` for ``grok-manifest``):

.. code-block:: bash

   grok-pull -c /etc/grokmirror/grokmirror.conf

The convention used by the distribution packages and the systemd units is
``/etc/grokmirror/<name>.conf``, one file per collection.

Interpolation
=============

Values can refer to other values. ``${varname}`` refers to a value in the
same section, ``${sectname:varname}`` to one in another section:

.. code-block:: ini

   [core]
   toplevel = /var/lib/git/mirror
   manifest = ${toplevel}/manifest.js.gz

   [pull]
   projectslist = ${core:toplevel}/projects.list

Multi-line values
=================

Options that accept lists -- ``include``, ``exclude``, ``ignore``,
``nopurge``, and the hooks -- take one entry per line, with continuation
lines indented:

.. code-block:: ini

   [pull]
   include = /pub/scm/linux/kernel/git/torvalds/linux.git
             /pub/scm/linux/kernel/git/stable/linux.git

Most of them accept shell-style globbing.

[core]
======

Settings shared by all the tools.

``toplevel``
    Directory holding the mirrored repositories. Required.

``manifest``
    Path to *this* installation's manifest. On an origin this is the file
    you publish; on a replica it is the one grok-pull writes as it works.
    A ``.gz`` extension turns on compression. Required.

``log``
    Where to write the log. Rotate it yourself -- grokmirror will not, and
    the file grows without bound. See ``contrib/logrotate``.

``loglevel``
    ``info`` or ``debug``. Debug is extremely verbose.

``objstore``
    Directory for object storage repositories. Usually
    ``${toplevel}/objstore``.

``objstore_uses_plumbing``
    Copy objects into objstore repositories using git plumbing instead of
    ``git fetch``. Considerably faster on a busy mirror, because it skips
    git's cautious haves/wants negotiation. Default: ``no``.

``private``
    Repositories that must never have their objects fetched into an
    objstore, as shell globs. They are still set up with alternates when a
    common root is found, but nothing is copied *out* of them. Use this if
    you have private repositories and a web UI, since an objstore makes
    any object reachable through any of its siblings when the hash is
    known.

[manifest]
==========

Used by ``grok-manifest``. Every option here has a matching command-line
flag, and the flag wins.

``pretty``
    Sort and indent the manifest. Easier to read, but larger and slower to
    generate. Default: ``no``.

``ignore``
    Repository paths to leave out, as shell globs.

``fetch_objstore``
    Fetch objects into objstore repositories from the post-commit hook.
    This can help when somebody pushes the same objects to a sibling, but
    it slows the hook down enough to often be a net loss. With ``no``, the
    objects are picked up by the next ``grok-fsck`` run. Default: ``no``.

``check_export_ok``
    Only include repositories carrying the ``git-daemon-export-ok`` file.
    Default: ``no``.

[remote]
========

Where a replica pulls from. Used by ``grok-pull``.

``site``
    Base URL of the origin, for example ``https://git.kernel.org``. Repository
    URLs are built from this plus the path from the manifest.

``manifest``
    URL of the remote manifest. ``http://`` and ``https://`` are fetched
    with ``If-Modified-Since``; ``file://`` reads it directly, which is
    useful when the manifest lives on shared storage.

    .. note::
       Credentials cannot be embedded in the URL. Use a netrc file.

``manifest_command``
    An alternative to ``manifest``: a command that prints the manifest on
    stdout. The contract is:

    * exit 0 -- valid JSON manifest on stdout
    * exit 1 -- error message on stdout
    * exit 127 -- nothing on stdout, meaning the manifest has not changed

    It must also accept ``--force`` as its single argument, to fetch the
    manifest even when unchanged. This is the usual way to do per-replica
    access control, since the command can emit only the repositories that
    replica may see. Examples for gitolite are in ``contrib/gitolite/``.

``preload_bundle_url``
    Base URL for pre-generated preload bundles, if the origin publishes
    them. Only worth setting when mirroring an entire collection. See
    ``fsck.preload_bundle_outdir`` on the origin side.

[pull]
======

Used by ``grok-pull``.

Selecting repositories
----------------------

``include``
    Repositories to mirror, as shell globs. ``*`` means everything.

``exclude``
    Repositories to skip. Applied after ``include``.

``ffonly``
    Repositories to fetch fast-forward only, as shell globs. Their remote
    is configured with a non-forcing refspec, so a rewritten history
    upstream fails the fetch instead of being replicated over your copy.
    These repositories are also never purged, whatever the manifest says.

Purging
-------

``purge``
    Remove local repositories that are no longer in the remote manifest.
    With ``no``, this can still be requested per-run with ``-p``, which is
    handy for large collections where you do not want to walk the whole
    tree every time. Default: ``yes``.

``nopurge``
    Repositories never to purge, as shell globs. Use it for anything in
    the toplevel that grokmirror does not manage.

``purgeprotect``
    Refuse to purge if the remote manifest has shrunk by more than this
    percentage. This is what stops a truncated or broken manifest from
    wiping out your mirror. Default: ``5``. Override for one run with
    ``--force-purge``.

Performance
-----------

``pull_threads``
    How many repositories to update in parallel. Be considerate to the
    origin, and keep in mind that it may enforce per-IP session limits.
    Default: ``5``.

``retries``
    How many times to retry a failed fetch before marking the repository
    failed. Default: ``3``.

Continuous operation
--------------------

``refresh``
    Seconds between manifest checks when running with ``-o``. Without
    this, ``-o`` does nothing.

``socket``
    Path to a listener socket for push notifications. Each line written to
    it is a repository path as it appears in the local manifest; anything
    unrecognized is ignored. Requires ``refresh``. See
    ``contrib/pubsubv1.py``.

Hooks
-----

Each of these can hold several commands, one per line. See
:doc:`maintenance` for what they are typically used for.

``post_update_hook``
    Runs after each repository is modified, receiving its full path as the
    only argument.

``post_clone_complete_hook``
    Runs after a batch of new clones finishes. Takes no arguments; the
    full paths of the freshly cloned repositories arrive on stdin,
    newline-terminated. Use this for work that should only happen when
    there were new clones and all of them are complete.

``post_work_complete_hook``
    Runs when the work queue drains and grokmirror goes idle. No arguments
    and no stdin.

Web interface support
---------------------

``projectslist``
    Write a ``projects.list`` for gitweb or cgit. Leave blank to skip.

``projectslist_trimtop``
    Start the listing at this subpath instead of the toplevel. Useful for
    generating several gitweb or cgit configurations from one tree.

``projectslist_symlinks``
    Include symlinks in ``projects.list``. They are excluded by default,
    on the assumption that they are legacy paths. Default: ``no``.

``default_owner``
    Owner to report for repositories whose manifest entry does not name
    one.

Miscellaneous
-------------

``remotename``
    Name of the git remote pointing at the origin. Default:
    ``_grokmirror``.

[fsck]
======

Used by ``grok-fsck``. See :doc:`maintenance`.

Scheduling
----------

``frequency``
    Days between checks of any given repository. Each repository gets its
    first check at a random point within the first ``frequency`` days, so
    that they do not all land on the same night. Do not go below 7 unless
    the collection is small. Default: ``30``.

``statusfile``
    Where to record what was checked and when.

Repacking
---------

``repack``
    Repack repositories. You want this on. Default: ``yes``.

``extra_repack_flags``
    Extra flags for every repack. Grokmirror already picks appropriate
    flags depending on whether the repository uses alternates and whether
    the repack is full, and builds bitmaps where they make sense. Use this
    for things like ``--threads`` and ``--window-memory``.

``extra_repack_flags_full``
    Added on top of ``extra_repack_flags`` for full repacks. Add ``-f``
    here if you want full repacks to recompute every delta from scratch
    like grokmirror used to; the marginally better deltas are rarely
    worth the CPU on large repositories. Default:
    ``--window=250 --depth=50``.

``commitgraph``
    Generate commit graphs. Skipped for child repositories that use
    alternates. Note that git older than 2.24 will not use the graphs
    unless ``core.commitgraph`` is set. Default: ``yes``.

``prune``
    Run ``git prune`` to drop obsolete loose objects. Grokmirror makes
    sure this is safe with respect to objstore repositories. Default:
    ``yes``.

``precious``
    Set ``extensions.preciousObjects`` on repositories whose objects
    others depend on through alternates. This protects them from a stray
    ``git gc`` run by hand in the wrong directory. Grokmirror turns the
    flag off temporarily during its own repacks so that redundant packs
    can still be removed. Setting it to ``always`` leaves it on even then,
    which is maximum paranoia at the cost of repositories that only ever
    grow. Default: ``yes``.

Object storage tuning
---------------------

``baselines``
    Repositories likely to hold most of the interesting objects, as shell
    globs. With many forks sharing an objstore, every fetch otherwise
    negotiates thousands of refs. This limits the negotiation using git's
    ``core.alternateRefsPrefixes``.

``islandcores``
    Repositories to prioritize when building delta islands, as shell
    globs. Set this to whatever is cloned most often.

``preload_bundle_outdir``
    Generate preload bundles for objstore repositories into this
    directory. Only useful if you are running a major mirroring hub. See
    ``remote.preload_bundle_url`` on the replica side.

Error handling
--------------

``ignore_errors``
    Substrings of fsck output to treat as benign. The default list covers
    the usual harmless notices.

``reclone_on_errors``
    Substrings that mean the repository is damaged beyond local repair.
    When one matches, grokmirror asks grok-pull to reclone that repository
    on its next run. Repositories without a mirror remote -- the ones on
    an origin server, which has nowhere to reclone from -- are reported as
    needing manual attention instead.

Reporting
---------

``report_to``
    Address for the error report. Default: ``root``.

``report_from``, ``report_subject``, ``report_mailhost``
    The rest of the mail settings. Nothing is sent when a run finds no
    errors.

Public-Inbox Piping
===================

``grok-pi-piper`` uses its own separate configuration file, documented in
``pi-piper.conf`` and in ``grok-pi-piper(1)``.
