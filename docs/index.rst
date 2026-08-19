.. Grokmirror documentation master file

Grokmirror
==========

Grokmirror keeps collections of git repositories in sync between an origin
server and any number of replicas. It was written for kernel.org, where
most of the several hundred repositories are forks of the same kernel
tree.

Overview
--------

The origin server publishes a *manifest*: a small JSON file that lists
every repository it carries, along with a timestamp and a special
"fingerprint" for each one, calculated from the state of all of its refs.
A git hook refreshes the manifest whenever a repository changes, so it
is always current.

Replicas poll that manifest over HTTP. If it has not changed, the origin
answers with a ``304 Not Modified`` and the replica does no work at all.
If it has changed, the replica compares it against its own copy and only
touches the repositories whose fingerprints moved -- everything else is
left alone.

That is the whole idea, and it is what makes grokmirror cheap on both
sides. A replica of a collection that had two repositories pushed to since
its last pass does exactly two fetches, no matter how big the collection
is.

Concepts
--------

The manifest
~~~~~~~~~~~~

The manifest is a JSON dictionary keyed by the repository path relative
to the toplevel, usually served gzip-compressed::

    {
      "/path/to/bare/repository.git": {
        "description": "Repository description",
        "head":        "ref: refs/heads/branchname",
        "reference":   "/path/to/reference/repository.git",
        "forkgroup":   "forkgroup-guid",
        "modified":    timestamp,
        "fingerprint": sha1sum(git show-ref),
        "symlinks": [
            "/location/to/symlink"
        ]
      }
    }

Replicas fetch it with ``If-Modified-Since``, so an unchanged manifest
costs a single conditional request.

Object storage repositories
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Mirroring a few hundred forks of ``linux.git`` naively means storing the
same few gigabytes of objects a few hundred times. Grokmirror avoids that
with *object storage* repositories, or "objstore" for short.

``grok-fsck`` recognizes related repositories by looking at their root
commits. When it finds two or more of them, it creates a shared objstore
repository and fetches every ref from each sibling into it, under
``refs/virtual/<hash-of-sibling>/``. The original repositories are then
pointed at the objstore through ``objects/info/alternates`` and repacked
down to almost nothing.

The objstore repository is repacked with delta islands enabled, so clones
of any individual sibling stay fast.

.. warning::
   Because siblings share an object pool, any object from one sibling can
   in principle be retrieved through another one if the object hash is
   known -- the same behavior you see on GitHub forks. If some of your
   repositories must not leak objects, list them in ``core.private``.
   See :doc:`configuration`.

Features
--------

* Cheap change detection -- one conditional HTTP request when nothing moved
* Shared object storage for forks, with delta-island repacking
* Continuous (daemon) operation with optional push notifications
* Routine repacking, fsck, and corruption reporting via ``grok-fsck``
* Automatic recloning of repositories that turn up corrupt
* Symlink, ``projects.list``, and ``git-daemon-export-ok`` handling
* Hooks at every interesting point: per-repository, after clones, and
  when the work queue drains
* Companion tools for public-inbox archives and for Android's ``repo``

Non-features
------------

* Only bare repositories are supported
* No mirroring of anything that is not a git repository
* No authentication scheme of its own -- use netrc, ssh, or your httpd

The tools
---------

============================  =====================================================
``grok-manifest``             Build and update the manifest (origin side)
``grok-pull``                 Clone and update repositories (replica side)
``grok-fsck``                 Repack, fsck, and manage object storage
``grok-bundle``               Generate ``clone.bundle`` files for Android's ``repo``
``grok-dumb-pull``            Update repositories that grokmirror does not manage
``grok-pi-piper``             Pipe new public-inbox messages into a command
``grok-pi-indexer``           Index mirrored public-inbox repositories
============================  =====================================================

Each has its own man page; this documentation covers how they fit
together.

FAQ
---

Why is it called "grokmirror"?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Because it is developed at kernel.org and "grok" is a mirror of "korg".
Also because it groks git mirroring. It both long predates and has
nothing to do with a certain LLM bot.

Why not just use rsync?
~~~~~~~~~~~~~~~~~~~~~~~

Rsync is a poor fit for git trees, which consist of many small files that
rarely change. Rsync has to checksum every file on every run, which mostly
produces disk thrashing.

Worse, if repositories share objects with each other, rsync will only
produce working repositories when the disk paths are identical on both
ends. Anything else gives you broken repositories.

And it is a bit silly to begin with: git already knows exactly how to
describe what changed between two revisions.

Can a replica also be an origin?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Yes. ``grok-pull`` writes its own manifest as it goes, so a replica can
publish that manifest and serve its own downstream replicas.

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   origin
   replica
   configuration
   maintenance
   contributing

Indices and tables
==================

* :ref:`genindex`
* :ref:`search`
