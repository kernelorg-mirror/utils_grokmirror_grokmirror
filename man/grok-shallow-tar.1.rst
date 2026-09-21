GROK-SHALLOW-TAR
================
-------------------------------------------------
Publish shallow single-branch repos as tarballs
-------------------------------------------------

:Author:    mricon@kernel.org
:Date:      2026-09-11
:Copyright: The Linux Foundation and contributors
:License:   GPLv3+
:Version:   2.0.0
:Manual section: 1

SYNOPSIS
--------
    grok-shallow-tar [options] -c grokmirror.conf -o path --clone-url-base url --branches repoglob:branchglob

DESCRIPTION
-----------
A CI system that starts every job with ``git clone --depth=1`` makes the
server do real work for each one: ``git-upload-pack`` has to walk the
history and build a pack, and it does that again for every job in the
swarm. The result is identical every time, so it may as well be made once.

grok-shallow-tar makes it once. For each branch you name, it produces a
shallow, single-branch clone and writes it out as a tarball next to a small
JSON file describing it. The tarballs are ordinary files, so you can put
them behind a CDN and let CI nodes fetch them over HTTP instead of asking
git for a pack.

The clone inside the tarball is not just history-trimmed, it is also
single-branch and tagless: the clone is made with ``--no-tags``, so no tag
ships inside the archive even when one sits on the branch tip, and
``remote.origin.tagOpt = --no-tags`` is set so it stays that way. When a node
untars it and runs ``git remote update``, it asks the origin about exactly
one ref and does not drag in thousands of tags.

Publication is opt-in. A repository that no ``--branches`` pattern names
produces nothing at all -- these artifacts are hundreds of megabytes each,
and a default of "everything in the manifest" would be a surprising way to
fill a disk.

OPTIONS
-------

  -h, --help            show this help message and exit
  -v, --verbose         Be verbose and tell us what you are doing (default: False)
  -c CONFIG, --config CONFIG
                        Location of the configuration file
  -o OUTDIR, --outdir OUTDIR
                        Location where to publish the tarballs
  --branches <REPOGLOB:BRANCHGLOB>
                        Publish these branches of these repositories (repeat
                        for more)
  --clone-url-base URL  Public site the tarballs should fetch from, e.g.
                        https://git.kernel.org
  --strip-prefix PATH   Drop this leading path from the published layout, e.g.
                        /pub/scm/linux/kernel/git
  --depth DEPTH         How many commits of history to put in the tarball
                        (default: 1)
  --max-ref-age DAYS    Publish only branches whose tip is newer than this
                        (0 disables) (default: 0)
  --max-branches NUM    Publish at most this many branches per repository,
                        newest first (0 disables) (default: 0)
  --version             show program's version number and exit

CHOOSING WHAT TO PUBLISH
------------------------
``--branches`` takes two shell globs joined by a colon: which repositories,
and which of their branches. It may be given more than once, and every
pattern that names a repository contributes its branches, so one repository
can be published from two switches. The repository glob is matched against
the manifest key with or without its leading slash, whichever you find
easier to type.

The colon that separates the halves is the *last* one in the argument.
That is not arbitrary: git refuses to create a branch whose name contains a
colon, so the branch glob can never have one, while a repository path
perfectly well can.

Branch names are globbed rather than listed because on a tree like the
kernel's stable repository, branches are born and retired constantly and a
hand-maintained list would rot within a release. ``--max-ref-age`` then
takes care of the retirement end: a stable branch that has reached
end-of-life simply stops receiving commits, so an age limit drops it without
anybody having to notice. ``--max-branches`` is the belt to that suspenders,
capping how many branches a single repository may publish, keeping the ones
whose tips are newest.

A branch name may contain a slash, which a filename may not, so ``/`` becomes
``-`` in the published name. That is not injective: ``for-next/core`` and a
branch literally named ``for-next-core`` want the same filename. When that
happens, *neither* is published and the collision is logged at CRITICAL.
Letting one of them win would publish a tarball whose contents depend on the
order git happened to list refs in, which looks exactly like success.

WHAT GETS PUBLISHED
-------------------
Under ``--outdir``, in a directory tree mirroring the manifest path, each
branch produces four names. For ``/pub/scm/linux/kernel/git/stable/linux.git``
branch ``linux-6.18.y``::

    pub/scm/linux/kernel/git/stable/
        linux.linux-6.18.y.shallow.20260911.a1b2c3d.tar
        linux.linux-6.18.y.shallow.20260911.a1b2c3d.tar.json
        linux.linux-6.18.y.shallow.latest.tar      -> the dated tarball
        linux.linux-6.18.y.shallow.latest.tar.json -> the dated sidecar

The date is UTC, and the seven hex digits are the abbreviated branch tip.
That last part is doing real work: a depth-1 single-branch tarball is
entirely determined by its tip, so the name does not merely identify the
file, it names the commit inside it. It is also how the tool knows whether
there is anything to do -- if a tarball for the current tip is already on
disk, the run generates nothing. There is no state file to lose or corrupt;
the directory listing *is* the state.

``--strip-prefix`` takes a shared leading directory off that layout. Every
kernel.org repository worth publishing this way lives under
``/pub/scm/linux/kernel/git``, and repeating those five levels inside an output
directory that already says what it holds only makes the URL longer. With
``--strip-prefix /pub/scm/linux/kernel/git`` the example above becomes::

    stable/
        linux.linux-6.18.y.shallow.20260911.a1b2c3d.tar
        ...

Only the directory moves; the filename still names the repository, the branch
and the commit, and the sidecar still records the full manifest key, because
that is what answers "which repository is this".

A repository that does not start with the prefix is published at its full path
rather than skipped -- ``--branches`` named it explicitly, and nothing here can
tell a mistyped prefix from a repository that genuinely lives elsewhere. That
does mean a stripped path can land on a literal one: ``/pub/scm/a/linux.git``
stripped of ``/pub/scm`` and a plain ``/a/linux.git`` both want ``a/linux``.
As with the branch-name clash above, *neither* is published and the collision
is logged at CRITICAL. It is settled before the first clone, so a run that hits
one wastes no work.

The ``.json`` sidecar carries the repository, the branch, the full tip, the
creation time, the depth, the size and the SHA-256 of the tarball beside it.

Two dated tarballs are kept per branch, the current one and the one before
it. Deleting the old one in the same run that publishes the new one would
leave a window that CI falls into: a node reads ``latest`` from a frontend
that has synced and then asks for that dated file from one that has not, and
a 404 lands in the middle of somebody's build. One cycle of overlap closes
it.

Every file reaches its final name by ``rename(2)`` from a temporary name in
the same directory, tarballs and symlinks alike, so a reader arriving
mid-write sees either the whole thing or the previous one, never a truncated
tarball.

USING A TARBALL FROM CI
-----------------------
The tarball unpacks into a directory named after the repository, containing
a normal (non-bare) working tree with its ``.git``. Nothing is checked out
yet, which is deliberate: the checkout is the part your job wants to control.

A job that wants a specific commit does::

    curl -sSfL https://cdn.example.org/shallow/pub/scm/linux/kernel/git/stable/linux.linux-6.18.y.shallow.latest.tar | tar -x
    cd linux
    rm -rf .git/hooks .git/config .git/objects/info/alternates
    git init -q
    git remote add -t linux-6.18.y --no-tags origin https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git
    git fsck
    git remote update
    git checkout "$WANTED_SHA"

The three lines after ``cd`` are there because the tree arrived over the
network as an archive with none of the usual git checks applied to it, and
``.git/config`` is a file git reads as instructions rather than as data.
``core.hooksPath`` relocates the hooks a job just deleted, ``core.fsmonitor``
names a program run during ordinary commands -- including during the ``git
fsck`` meant to check the tree -- and ``core.sshCommand`` and
``remote.*.uploadpack`` run one on a fetch. Deleting ``.git/hooks`` alone
does not cover any of that. Deleting the config does, and ``git init``
rebuilds a default one while keeping the objects, the refs and the shallow
boundary; re-add the remote with the branch and ``--no-tags`` so the narrow,
tagless fetch this tool exists for survives the reset.

Give ``git remote add`` the URL your own configuration knows. The origin
baked into the tarball is a convenience for the reader, not a thing to trust:
it is in the archive along with everything else.

The fsck verifies that every object is intact and is what its name says it
is, which takes around ten seconds on a kernel-sized tree. What it does not
do is tell you the history is genuine -- a fabricated commit is a valid
object. That assurance comes from naming the commit you want by its full
object ID, from a source you trust, which is what ``$WANTED_SHA`` above is:
an object ID is checked against the object it names, while a branch or tag
inside the tarball is only whatever the tarball says it is.

``git remote update`` talks to ``--clone-url-base`` plus the repository path,
which is a public URL and not the path on the host that built the tarball.
Because the clone is single-branch and tagless, that fetch asks about one
ref and transfers only the commits made since the tarball was cut -- which is
the whole point, and the reason a slightly stale tarball costs almost
nothing.

If your job needs the tarball to be exactly reproducible, or wants to pin
what it is running against, fetch the dated name rather than ``latest`` and
verify it against the ``sha256`` in the sidecar.

Do not run ``git fetch --unshallow`` on an unpacked tarball, and tell your
users the same. It asks the server to build the entire history as a single
pack, which is heavier than the shallow clones these tarballs were made to
replace -- one job doing it undoes the saving of many. A job that genuinely
needs full history should clone the repository normally instead.

Every unpacked tree carries two files saying so, for whoever finds the
directory later with no memory of having downloaded it:

``.git/description``
  Where the tree came from, which branch and commit it holds, and when it
  was generated, in four lines, plus a pointer at the file below.

``.git/shallow-tar.readme``
  What the tree is, the same reset-and-fsck steps as above, how to bring it
  up to date, and why not to unshallow or deepen it.

DEPLOYMENT NOTES
----------------
Publish to a directory on a single filesystem. The atomic-rename guarantee
above is a same-filesystem property; if ``--outdir`` straddles a mount point
the rename becomes a copy and the guarantee goes with it.

When syncing the output to frontends, do **not** use ``rsync --inplace``.
Inplace transfer rewrites the file readers are currently downloading, which
undoes the whole scheme. Plain rsync writes a temporary and renames, which is
what you want. ``--delete`` is fine and is how pruning reaches the frontends.

Run one grok-shallow-tar at a time against a given output directory. It takes
no locks -- two runs sharing ``-o`` will prune each other's work. Running
alongside grok-pull(1) or grok-fsck(1) is fine: the repositories are only
read, and a clone that loses a race with a repack fails, logs, and is simply
made again on the next run.

Expect the run to need temporary space: each branch is cloned into a
temporary directory under ``--outdir`` before being packed, and that
directory is removed afterwards whether or not the branch succeeded. One
branch failing -- running out of disk, say -- skips that branch and lets the
rest of the run continue, but the exit code is still non-zero, so cron will
tell you.

Those temporary directories are named ``.shallowtar-*``. Killing a run leaves
one behind, since a signal does not give the process a chance to tidy up, and
what it holds can be most of a clone. Each run sweeps up any it finds under
``--outdir`` that are more than a day old, so an interrupted run costs you the
space only until the next one -- but if you need it back sooner, they are safe
to delete by hand once no run is in progress.

EXAMPLES
--------

    grok-shallow-tar -c grokmirror.conf -o /var/www/shallow --clone-url-base https://git.kernel.org --branches '/pub/scm/linux/kernel/git/stable/linux.git:linux-\*.y' --max-ref-age 365 --max-branches 12

    grok-shallow-tar -c grokmirror.conf -o /var/www/shallow --clone-url-base https://git.kernel.org --strip-prefix /pub/scm/linux/kernel/git --branches '/pub/scm/linux/kernel/git/\*/\*.git:master'

    grok-shallow-tar -c grokmirror.conf -o /var/www/shallow --clone-url-base https://git.kernel.org --branches '/pub/scm/linux/kernel/git/torvalds/linux.git:master' --branches '/pub/scm/linux/kernel/git/next/linux-next.git:master' --depth 50

SEE ALSO
--------
* grok-pull(1)
* grok-manifest(1)
* grok-bundle(1)
* git(1)

SUPPORT
-------
Email tools@linux.kernel.org.
