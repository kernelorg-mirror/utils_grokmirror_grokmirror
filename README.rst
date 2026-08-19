GROKMIRROR
==========
--------------------------------------------
Framework to smartly mirror git repositories
--------------------------------------------

:Author:    konstantin@linuxfoundation.org
:Copyright: The Linux Foundation and contributors
:License:   GPLv3+

DESCRIPTION
-----------
Grokmirror keeps collections of git repositories in sync between an origin
server and any number of replicas. It was written for kernel.org, where
most of the several hundred repositories are forks of one enormous tree.

The origin server publishes a *manifest*: a small JSON file listing every
repository it carries, along with a timestamp and a fingerprint for each
one. A git hook keeps it current. Replicas poll that manifest over HTTP,
and only fetch the repositories whose fingerprint moved -- so a pass where
nothing changed costs a single conditional request, no matter how large
the collection is.

Grokmirror also collapses forks into shared "object storage" repositories,
so that a hundred forks of the same tree do not cost a hundred copies of
its objects on disk.

TOOLS
-----
============================  =====================================================
``grok-manifest``             Build and update the manifest (origin side)
``grok-pull``                 Clone and update repositories (replica side)
``grok-fsck``                 Repack, fsck, and manage object storage
``grok-bundle``               Generate ``clone.bundle`` files for Android's ``repo``
``grok-dumb-pull``            Update repositories that grokmirror does not manage
``grok-pi-piper``             Pipe new public-inbox messages into a command
``grok-pi-indexer``           Index mirrored public-inbox repositories
============================  =====================================================

Each has a man page in ``man/``.

DOCUMENTATION
-------------
See the ``docs/`` directory, which covers installing grokmirror, setting
up an origin server, setting up a replica, every option in
``grokmirror.conf``, and routine maintenance.

To build it locally::

    cd docs
    pip install -r requirements.txt
    make html

REQUIREMENTS
------------
Python 3.9 or newer, and git. Only bare git repositories are supported.

FAQ
---
Why is it called "grokmirror"?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Because it is developed at kernel.org and "grok" is a mirror of "korg".
Also because it groks git mirroring.

Why not just use rsync?
~~~~~~~~~~~~~~~~~~~~~~~
Rsync is a poor fit for git trees, which consist of many small files that
rarely change, and it has to checksum every one of them on every run.

Worse, if repositories share objects with each other, rsync only produces
working repositories when the disk paths are identical on both ends.

And it is a bit silly to begin with: git already knows exactly how to
describe what changed between two revisions.

CONTRIBUTING
------------
Send patches to tools@kernel.org, ideally prepared with b4_. See
``docs/contributing.rst``.

.. _b4: https://b4.docs.kernel.org/

SUPPORT
-------
Send email to tools@kernel.org.
