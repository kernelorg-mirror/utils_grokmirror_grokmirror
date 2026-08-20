===========
Maintenance
===========

Git repositories need routine repacking and integrity checks, whether they
are mirrored or not. ``grok-fsck`` does both, and it is also what sets up
object storage, so it is not optional on a replica.

Running grok-fsck
=================

.. code-block:: bash

   grok-fsck -c /etc/grokmirror/grokmirror.conf

A normal run checks only the repositories that are due, based on
``fsck.frequency``. Each repository is scheduled at a random offset within
the first cycle, so the load spreads out instead of landing on one night.

Run it once a day. It will usually find little or nothing to do, which is
the point: the day it does have work, it is only a fraction of the tree.

The systemd timer in ``contrib/`` does exactly this:

.. code-block:: bash

   sudo cp contrib/grok-fsck@.service contrib/grok-fsck@.timer /etc/systemd/system/
   sudo systemctl enable --now grok-fsck@grokmirror.timer

Or from cron:

.. code-block:: bash

   0 2 * * * /usr/bin/grok-fsck -c /etc/grokmirror/grokmirror.conf

Useful modes
------------

``-f``, ``--force``
    Check every repository right now, ignoring the schedule. Use this
    after the initial clone of a replica to get object storage set up in
    one pass instead of waiting out a full cycle.

``--repack-only``
    Repack what needs repacking and skip the integrity checks. Cheap
    enough for a nightly run alongside the regular schedule.

``--connectivity``
    Run ``git fsck`` on everything, but check connectivity only. Much
    faster than a full fsck across the whole tree.

``--repack-all-quick``, ``--repack-all-full``
    Repack everything, either quickly or thoroughly. A quick repack is
    geometric (with git 2.41 or newer): it only rolls up the small packs
    and never rewrites or drops the bulk of a repository. A full repack
    consolidates everything into a single pack (plus a cruft pack holding
    the unreachable objects), which on a large tree is expensive -- reach
    for it after a bulk import, not routinely.

Object Storage
==============

Object storage is set up by grok-fsck, not by grok-pull. If your mirror
takes far more disk than the origin does, this is why: run ``grok-fsck
-f`` once and the forks will collapse into a shared object pool.

Grok-fsck finds related repositories by comparing root commits. When it
finds two or more, it creates an objstore repository under
``core.objstore``, fetches every ref from each sibling into
``refs/virtual/<hash>/``, points the siblings at it through
``objects/info/alternates``, and repacks them down to metadata.

Repositories that leave a fork group -- because they were repacked
standalone and their alternates removed -- are left alone and reported in
the log. Nothing is silently undone.

Two knobs are worth setting on a large tree:

* ``fsck.baselines`` cuts down ref negotiation when many forks share an
  objstore.
* ``fsck.islandcores`` gives the most-cloned repository priority when
  delta islands are built.

See :doc:`configuration`.

.. warning::
   Siblings sharing an object pool means an object pushed to one of them
   can be fetched through any other one by hash, exactly as GitHub forks
   behave. List anything sensitive in ``core.private``.

Error Reports and Recloning
===========================

When a run finds errors, grok-fsck mails a report to ``fsck.report_to``
(``root`` by default). A clean run mails nothing.

Not everything git reports is worth waking up for. ``fsck.ignore_errors``
holds substrings to treat as benign, and its default covers the usual
harmless notices.

Some things grok-fsck simply fixes. A commit-graph that still lists
commits the repository no longer has -- they became unreachable and were
pruned -- makes ``git fsck`` complain about every one of them. That is
not damage, so the graph is thrown away and rewritten, and nothing is
reported.

At the other end, ``fsck.reclone_on_errors`` lists substrings that mean
the repository is damaged past local repair -- a missing tree, a broken
link. When one matches, grok-fsck marks the repository for recloning and
the next ``grok-pull`` run fetches a fresh copy.

This only works on a replica. A repository with no mirror remote has
nowhere to reclone from and no grok-pull run to do it, so grok-fsck
reports it as needing manual attention and leaves it alone.

Log Rotation
============

Grokmirror never rotates its own log, and ``loglevel = debug`` produces a
great deal of it. Install the example from ``contrib/logrotate``.

Hooks
=====

The ``[pull]`` hooks are how grokmirror drives other work off replication.
Each can hold several commands, one per line.

``post_update_hook`` fires after every repository that changed, receiving
its path. ``post_clone_complete_hook`` fires once a batch of new clones
finishes, with their paths on stdin. ``post_work_complete_hook`` fires
when the queue drains.

The three-stage arrangement matters for anything that has an expensive
"finish the job" step. Indexing a public-inbox mirror is the canonical
example:

.. code-block:: ini

   [pull]
   post_clone_complete_hook = /usr/bin/grok-pi-indexer -c /etc/public-inbox/config -j 4 --no-fsync init
   post_update_hook = /usr/bin/grok-pi-indexer -c /etc/public-inbox/config update
   post_work_complete_hook = /usr/bin/grok-pi-indexer -c /etc/public-inbox/config -j 4 --no-fsync extindex

New archives get initialized once when they finish cloning, each update
gets indexed as it lands, and the expensive cross-archive index is rebuilt
only after everything has settled.

Hooks are not run under a timeout, on purpose: indexing a large archive
can legitimately take days. Remote git operations are, at a generous six
hours, so a worker stuck on a dead connection eventually recovers.

Other Tools
===========

``grok-dumb-pull``
    Updates repositories that grokmirror does not manage -- ones you clone
    from somewhere without a manifest. It is a plain "fetch these
    remotes" tool with grokmirror's locking, so it can share a tree with
    the managed repositories safely.

``grok-bundle``
    Generates ``clone.bundle`` files in the layout Android's ``repo`` tool
    expects, so that clones can be offloaded to a CDN.

``grok-pi-piper``
    Pipes new messages from mirrored public-inbox archives into a command,
    such as procmail. It can also configure the archives as shallow and
    prune messages it has already delivered, which saves a lot of disk.
    Configured separately in ``pi-piper.conf``.

``grok-pi-indexer``
    Runs public-inbox's indexing commands over mirrored archives. Designed
    to be called from the ``[pull]`` hooks, as shown earlier.

Each has a man page with the full set of options.
