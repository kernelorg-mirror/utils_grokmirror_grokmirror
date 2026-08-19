=============
Replica Setup
=============

A replica runs ``grok-pull``, which reads a remote manifest and makes the
local tree match it. This guide sets up a replica of kernel.org; adjust
the URLs for your own origin.

Step 1: Write a Configuration File
==================================

Grokmirror uses one config file per mirrored collection. Start from the
heavily commented example shipped with the package -- distribution
packages usually put it in ``/etc/grokmirror/grokmirror.conf``, and it is
also in the source tree as ``grokmirror.conf``.

A minimal working configuration looks like this:

.. code-block:: ini

   [core]
   toplevel = /var/lib/git/mirror
   manifest = ${toplevel}/manifest.js.gz
   log = ${toplevel}/grokmirror.log
   loglevel = info
   objstore = ${toplevel}/objstore

   [remote]
   site = https://git.kernel.org
   manifest = ${site}/manifest.js.gz

   [pull]
   projectslist = ${core:toplevel}/projects.list
   pull_threads = 5
   include = *

Note that ``core.manifest`` is the replica's *own* manifest, which
grok-pull writes as it works, while ``remote.manifest`` is the origin's.
They are different files and must not point at the same path.

The ``${...}`` syntax is variable interpolation: ``${toplevel}`` within
the same section, ``${core:toplevel}`` across sections.

Step 2: Choose What to Mirror
=============================

``pull.include`` and ``pull.exclude`` take shell-style globs, one per
line. ``include = *`` mirrors everything. To take only a few trees:

.. code-block:: ini

   [pull]
   include = /pub/scm/linux/kernel/git/torvalds/linux.git
             /pub/scm/linux/kernel/git/stable/linux.git
             /pub/scm/linux/kernel/git/next/linux-next.git

``exclude`` is applied after ``include``, which makes "everything except"
easy to express:

.. code-block:: ini

   [pull]
   include = *
   exclude = */linux-2.4*

.. note::
   Mirroring all of kernel.org needs a lot of disk. Start with the few
   repositories you actually want and grow the list later -- adding to
   ``include`` just makes the next run clone the newcomers.

Step 3: Run It
==============

.. code-block:: bash

   grok-pull -v -c /etc/grokmirror/grokmirror.conf

The first run clones everything you asked for and will take a long time.
Later runs only fetch repositories whose fingerprint changed in the
remote manifest, and a run where nothing changed at all costs one
conditional HTTP request.

Run it as the user that owns the toplevel, not as root.

Step 4: Keep It Running
=======================

There are two ways to run grok-pull repeatedly. Which one you want
depends on how often you need updates.

**From cron**, for a few times a day or less:

.. code-block:: bash

   */30 * * * * /usr/bin/grok-pull -c /etc/grokmirror/grokmirror.conf

**As a daemon**, for anything more frequent. Set a refresh interval:

.. code-block:: ini

   [pull]
   refresh = 900

and run with ``-o``:

.. code-block:: bash

   grok-pull -o -c /etc/grokmirror/grokmirror.conf

In continuous mode grok-pull stays resident and rechecks the manifest
every ``refresh`` seconds. This is considerably more efficient than cron,
because the process keeps its state, its connections, and its worker
threads between passes.

A systemd unit for exactly this is in ``contrib/grok-pull@.service``.
It is templated on the config name, so:

.. code-block:: bash

   sudo cp contrib/grok-pull@.service /etc/systemd/system/
   sudo systemctl enable --now grok-pull@grokmirror

Step 5: Set Up Log Rotation
===========================

The log file grows indefinitely on its own. An example logrotate config
is in ``contrib/logrotate/``.

Step 6: Schedule grok-fsck
==========================

Mirrored repositories still need repacking and integrity checks, and
object storage only gets set up when ``grok-fsck`` runs. See
:doc:`maintenance` -- do not skip this step.

Push Notifications
==================

If your origin can notify you about pushes, a replica in continuous mode
can react within seconds instead of waiting out the refresh interval.
Enable the listener socket:

.. code-block:: ini

   [pull]
   refresh = 900
   socket = ${core:toplevel}/.updater.socket

Anything written to that socket is treated as a repository path followed
by a newline, matching the names in the local manifest::

   /pub/scm/linux/kernel/git/torvalds/linux.git\n

Names that do not match a known repository are ignored. The refresh
interval still applies as a safety net, so a missed notification only
costs you latency, never correctness. An example pubsub listener is in
``contrib/pubsubv1.py``.

Serving the Replica
===================

Grokmirror does not serve repositories itself -- use gitweb, cgit,
git-daemon, or an httpd as you prefer.

To generate a ``projects.list`` for gitweb or cgit, set
``pull.projectslist``. Symlinks are left out of it by default, on the
assumption that they are legacy paths; set ``projectslist_symlinks = yes``
if you want them listed too.

Because grok-pull writes its own manifest at ``core.manifest``, a replica
can serve that file and act as an origin for further replicas without any
extra setup.

Troubleshooting
===============

**Nothing happens on the first run.** Check that ``remote.manifest`` is
reachable and that ``pull.include`` actually matches something. Run with
``-v`` to see what grok-pull decided.

**Nothing happens on later runs.** Grok-pull skips the download when the
remote manifest's mtime has not moved. Force it with ``-n``.

**A repository is stuck failing.** Grok-pull retries a failing fetch
``pull.retries`` times before marking the repository failed and moving
on. Check the log for the actual git error.

**Repositories are much bigger than on the origin.** Object storage is
set up by ``grok-fsck``, not by grok-pull. Run ``grok-fsck -f`` once and
the forks will collapse into a shared object pool.

Next Steps
==========

* :doc:`configuration` -- the full set of options
* :doc:`maintenance` -- routine upkeep
