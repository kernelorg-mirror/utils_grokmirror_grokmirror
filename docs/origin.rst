===================
Origin Server Setup
===================

The origin server is the one that publishes a repository collection.
Its only job in grokmirror terms is to keep a manifest up to date and to
serve it over HTTP (or ssh).

.. important::
   Only bare git repositories are supported.

Step 1: Generate the Initial Manifest
=====================================

Run ``grok-manifest`` once over your whole collection:

.. code-block:: bash

   grok-manifest -m /var/www/html/manifest.js.gz \
       -t /var/lib/gitolite3/repositories

``-m`` is where the manifest goes. The git user must be able to write both
to that file and to the directory holding it -- grok-manifest writes a
``manifest.js.gz.randomstring`` file first and then moves it into place, so
that readers never see a half-written manifest.

``-t`` is the toplevel path, which is trimmed off the front of every
repository path. Without it, your replicas would end up mirroring
``/var/lib/gitolite3/repositories/foo.git`` instead of ``/foo.git``.

On a large collection this first run takes a while, because it walks every
repository and reads its refs. Subsequent runs are incremental.

Step 2: Install the Git Hook
============================

Add a ``post-receive`` or ``post-update`` hook to every repository, calling:

.. code-block:: bash

   grok-manifest -m /var/www/html/manifest.js.gz \
       -t /var/lib/gitolite3/repositories -n `pwd`

The trailing ``pwd`` argument puts grok-manifest into per-repository mode:
instead of walking the whole tree, it updates just that one entry.

``-n`` tells it to record the current time rather than parsing the commit
timestamps out of the repository. This is much faster, and in a hook you
already know the change just happened.

.. tip::
   With gitolite, drop the hook into
   ``~/.gitolite/hooks/common/post-receive`` and run ``gitolite setup
   --hooks-only`` to propagate it to every repository.

If a hook misbehaves, ``-l`` will tell you why:

.. code-block:: bash

   grok-manifest -m /var/www/html/manifest.js.gz \
       -t /var/lib/gitolite3/repositories \
       -l /var/log/grokmirror/grok-manifest.log -n `pwd`

Make sure the user running the hook can write to that log path.

Step 3: Handle Deleted Repositories
===================================

A hook fires when a repository changes, but nothing fires when one is
deleted, so removals need to be handled separately. Pick either approach.

Purge from cron, which walks the tree and drops entries whose repository
is gone from disk:

.. code-block:: bash

   grok-manifest -m /var/www/html/manifest.js.gz \
       -t /var/lib/gitolite3/repositories -p

Or remove entries explicitly at deletion time, which is instant and needs
no walk. With gitolite, add this to the ``D`` command:

.. code-block:: bash

   grok-manifest -m /var/www/html/manifest.js.gz \
       -t /var/lib/gitolite3/repositories -x $repo.git

``-x`` takes full paths, the same as the per-repository hook mode.

Step 4: Serve the Manifest
==========================

Point any httpd at the directory containing the manifest. There is nothing
special to configure beyond making sure the server sends a correct
``Last-Modified`` header, which every httpd does for static files. That
header is what lets replicas skip work entirely when nothing has changed.

An httpd is not required, though -- if you would rather not run one, see
`Alternatives to Publishing over HTTP`_ below.

Controlling What Gets Published
===============================

Not everything in the tree necessarily belongs in the manifest.

To publish only repositories explicitly marked as exportable, pass
``--check-export-ok``. Grok-manifest will then honor the
``git-daemon-export-ok`` magic file, the same as ``git-daemon(1)``:

.. code-block:: bash

   grok-manifest -c -m /var/www/html/manifest.js.gz \
       -t /var/lib/gitolite3/repositories

To skip particular paths, use ``-i`` (repeatable, accepts shell globbing):

.. code-block:: bash

   grok-manifest -i '/testing/*' -i '/private/*' \
       -m /var/www/html/manifest.js.gz \
       -t /var/lib/gitolite3/repositories

Both settings, and a few others, can live in a config file instead, which
saves repeating them in every hook. Write a ``grokmirror.conf`` with a
``[manifest]`` section and pass it with ``--cfgfile``; command-line flags
still win over config values. See :doc:`configuration`.

Alternatives to Publishing over HTTP
====================================

If your replicas can reach the manifest another way, they do not have to
fetch it over HTTP:

* A manifest on shared storage can be read directly with a ``file://`` URL.
* A replica can run a ``manifest_command`` and read the manifest from its
  standard output. This is how per-replica access control is usually done,
  since the command can generate a manifest containing only the
  repositories that replica is allowed to see. Examples for gitolite live
  in ``contrib/gitolite/``.

Both are configured on the replica side; see :doc:`configuration`.

Next Steps
==========

* :doc:`replica` -- setting up the other end
* :doc:`maintenance` -- keeping the repositories healthy
