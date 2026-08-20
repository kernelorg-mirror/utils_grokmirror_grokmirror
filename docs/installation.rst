============
Installation
============

Prerequisites
=============

* Python 3.9 or newer
* Git 2.20 or newer (grok-fsck refuses to run with anything older, since
  objstore repositories depend on delta islands); 2.41 or newer is
  recommended, as it enables geometric repacking and cruft packs, which
  dramatically reduce the repacking load on large trees
* An httpd on the origin server, to publish the manifest, though it can
  also be configured to work over ssh

Grokmirror depends on ``packaging``, ``requests``, and ``urllib3``, all of
which are packaged by every distribution.

Distribution Packages
=====================

Check your distribution first -- grokmirror is packaged for Fedora, EPEL,
Debian, and Ubuntu, among others:

.. code-block:: bash

   # Fedora / EPEL
   sudo dnf install python3-grokmirror

   # Debian / Ubuntu
   sudo apt install grokmirror

Distribution packages install the ``grok-*`` commands into ``/usr/bin``,
the man pages, and an example ``grokmirror.conf`` (usually under
``/etc/grokmirror/``).

Installing from PyPI
====================

.. code-block:: bash

   pipx install grokmirror

Using pipx keeps grokmirror and its dependencies out of your system
Python. If you would rather install into a virtualenv you manage
yourself, plain ``pip install grokmirror`` works the same way.

Installing from Source
======================

.. code-block:: bash

   git clone https://git.kernel.org/pub/scm/utils/grokmirror/grokmirror.git
   cd grokmirror
   pipx install .

The build is PEP 517 based (there is no ``setup.py``), so downstream
packaging should invoke the build backend rather than calling a script.

Development Installation
========================

For hacking on grokmirror itself, use `uv <https://docs.astral.sh/uv/>`_:

.. code-block:: bash

   git clone https://git.kernel.org/pub/scm/utils/grokmirror/grokmirror.git
   cd grokmirror
   uv sync

This creates a ``.venv`` with the development dependencies -- pytest,
mypy, pyright, and ruff. See :doc:`contributing` for how to run the test
suite.

Creating the Mirror User
========================

Grokmirror does not need root. On a replica, the usual arrangement is a
dedicated unprivileged user that owns the mirrored tree:

.. code-block:: bash

   sudo useradd -r -m -d /var/lib/git -s /bin/bash mirror
   sudo mkdir -p /var/lib/git/mirror
   sudo chown mirror: /var/lib/git/mirror

Make sure that user can write to the toplevel directory and to wherever
you point the log file.

Verifying Installation
======================

.. code-block:: bash

   grok-pull --version

This should print the version number.

Next Steps
==========

* To publish a repository collection, see :doc:`origin`
* To mirror another site's collection, see :doc:`replica`
