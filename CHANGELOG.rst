v3.0 (TBD)
----------
- Require Python 3.9 or newer
- Switch to a pyproject.toml-based build (setup.py is gone; downstream
  packaging should use the PEP 517 interface)
- Add new hook post_work_complete_hook that fires after all work is
  complete and grokmirror goes idle
- Add new command grok-pi-indexer for indexing public-inbox mirrored
  repositories
- Read and write all text files (configs, logs, descriptions, manifests,
  state files) as UTF-8 regardless of the system locale, so behavior no
  longer changes under e.g. LC_ALL=C cron environments
- Fix grok-manifest traceback when purging repositories that no longer
  exist on disk (broken since v2.0.0)
- Fix grok-manifest not removing a repository from the manifest when it
  was replaced by a symlink; it remained listed as a real repository in
  addition to showing up in the target's symlinks
- Fix grok-manifest recording a "reference" harvested from the current
  working directory when a repository listed a forkgroup but no longer
  had alternates
- Fix grok-manifest traceback when the manifest path has no directory
  component (e.g. "-m manifest.js.gz")
- Fix grok-manifest failing on every repository with recent versions of
  git on Python older than 3.11: git renders a UTC committer date with a
  trailing "Z", which datetime.fromisoformat() did not accept until 3.11
- Fix grok-manifest reporting a nonsensical runtime if the system clock
  was adjusted mid-run
- Fix grok-fsck traceback when reporting repository sizes of 1 TiB and
  above
- Fix grok-fsck traceback on repositories excluded from object storage
  with the grokmirror.do-not-objstore setting
- Fix grok-fsck traceback when merging siblings and none of them had
  usable root commits
- Fix grok-fsck writing the literal string "None" as the fingerprint of a
  repository with no refs, which every reader then accepted as valid
- Fix grok-fsck dying with a traceback, and losing the error report
  entirely, when the mail host could not be reached; the report is now
  written to the log instead
- Fix grok-bundle traceback when passed an empty --gitargs or
  --revlistargs
- Fix grok-pull error message on remote manifest command failure, which
  itself raised an exception while reporting the error
- grok-pull now reports a clear error when the [remote] section does not
  define "site", or defines neither "manifest" nor "manifest_command",
  instead of failing with a traceback inside a worker
- Fix grok-pull traceback when the config file has no [pull] section; all
  of its settings are optional, so leaving it out now works
- Fix grok-pull burying remote manifest failures (missing manifest,
  failing or unparseable manifest_command) under a traceback instead of
  just reporting them and exiting non-zero
- Fix grok-pi-piper hanging instead of exiting when no pipe was
  configured
- Fix grok-pi-piper not actually checking that the configured pipe
  command is executable, reporting a confusing error when it was not
- Report a clear error instead of a traceback when the configuration file
  has no "toplevel" defined in the [core] section
- Interrupting any of the commands with Ctrl-C is now reliable; several
  code paths used bare except clauses that swallowed KeyboardInterrupt
- The contrib pubsub v1 listener works again, and now requires falcon
  3.0 or newer; it also reads the request body in a way that does not
  depend on WSGI server internals
- Development: the tree is checked with ruff, mypy, ty and pyright; run
  ./ci.sh before committing and ./ci-matrix.sh before releases

v2.0.9 (2021-07-13)
-------------------
- Add initial support for post_clone_complete_hook that fires only after
  all new clones have been completed.
- Fix grok-manifest traceback due to unicode errors in the repo
  description file.
- Minor code cleanups.

v2.0.8 (2021-03-11)
-------------------
- Fixes around symlink handling in manifest files. Adding and deleting
  symlinks should properly work again.
- Don't require [fsck] section in the config file (though you'd almost
  always want it there).

v2.0.7 (2021-01-19)
-------------------
- A slew of small fixes improving performance on very large repository
  collections (CAF internally is 32,500).

v2.0.6 (2021-01-07)
-------------------
- Use fsck.extra_repack_flags when doing quick post-clone repacks
- Store objects in objstore after grok-dumb-pull call on a repo that uses
  objstore repositories

v2.0.5 (2020-11-25)
-------------------
- Prioritize baseline repositories when finding related objstore repos.
- Minor fixes.

v2.0.4 (2020-11-06)
-------------------
- Add support to use git plumbing for objstore operations, via enabling
  core.objstore_uses_plumbing. This allows to significantly speed up
  fetching objects into objstore during pull operations. Fsck operations
  will continue to use porcelain "git fetch", since speed is less important
  in those cases and it's best to opt for maximum safety. As a benchmark,
  with remote.preload_bundle_url and core.objstore_uses_plumbing settings
  enabled, cloning a full replica of git.kernel.org takes less than an hour
  as opposed to over a day.

v2.0.3 (2020-11-04)
-------------------
- Refuse to delete ffonly repos
- Add new experimental bundle_preload feature for generating objstore
  repo bundles and using them to preload objstores on the mirrors

v2.0.2 (2020-10-06)
-------------------
- Provide pi-piper utility for piping new messages from public-inbox
  repositories. It can be specified as post_update_hook:
  post_update_hook = /usr/bin/grok-pi-piper -c ~/.config/pi-piper.conf
- Add -r option to grok-manifest to ignore specific refs when calculating
  repository fingerprint. This is mostly useful for mirroring from gerrit.

v2.0.1 (2020-09-30)
-------------------
- fix potential corruption when migrating repositories with existing
  alternates to new object storage format
- improve grok-fsck console output to be less misleading for large repo
  collections (was misreporting obstrepo/total repo numbers)
- use a faster repo search algorithm that doesn't needlessly recurse
  into git repos themselves, once found


v2.0.0 (2020-09-21)
-------------------
Major rewrite to improve shared object storage and replication for VERY
LARGE repository collections (codeaurora.org is ~30,000 repositories,
which are mostly various forks of Android).

See UPGRADING.rst for the upgrade strategy.

Below are some major highlights.

- Drop support for python < 3.6
- Introduce "object storage" repositories that benefit from git-pack
  delta islands and improve overall disk storage footprint (depending on
  the number of forks).
- Drop dependency on GitPython, use git calls directly for all operations
- Remove progress bars to slim down dependencies (drops enlighten)
- Make grok-pull operate in daemon mode (with -o) (see contrib for
  systemd unit files). This is more efficient than the cron mode when
  run very frequently.
- Provide a socket listener for pubsub push updates (see contrib for
  Google pubsubv1.py).
- Merge fsck.conf and repos.conf into a single config file. This
  requires creating a new configuration file after the upgrade. See
  UPGRADING.rst for details.
- Record and propagate HEAD position using the manifest file.
- Add grok-bundle command to create clone.bundle files for CDN-offloaded
  cloning (mostly used by Android's repo command).
- Add SELinux policy for EL7 (see contrib).


v1.2.2 (2019-10-23)
-------------------
- Small bugfixes
- Generate commit-graph file if the version of git is new
  enough to support it. This is done during grok-fsck any time we
  decide that the repository needs to be repacked. You can force
  this off by setting commitgraph=never in config.


v1.2.1 (2019-03-11)
-------------------
- Minor feature improvement changing how precious=yes works.
  Grokmirror will now turn preciousObjects off for the duration
  of the repack. We still protect shared repositories against
  inadvertent object pruning by outside processes, but this
  allows us to clean up loose objects and obsolete packs.
  To have the 1.2.0 behaviour back, set precious=always, but it
  is only really useful in very rare cases.


v1.2.0 (2019-02-14)
-------------------
- Make sure to set gc.auto=0 on repositories to avoid pruning repos
  that are acting as alternates to others. We run our own prune
  during fsck, so there is no need to auto-gc, ever (unless you
  didn't set up grok-fsck, in which case you're not doing it right).
- Rework the repack code to be more clever -- instead of repacking
  based purely on dates, we now track the number of loose objects
  and the number of generated packs. Many of the settings are
  hardcoded for the moment while testing, but will probably end up
  settable via global and per-repository config settings.
- The following fsck.conf settings have no further effect:
    - repack_flags (replaced with extra_repack_flags)
    - full_repack_flags (replaced with extra_repack_flags_full)
    - full_repack_every (we now figure it out ourselves)
- Move git command invocation routines into a central function to
  reduce the amount of code duplication. You can also set the path
  to the git binary using the GITBIN env variable or by simply
  adding it to your path.
- Add "reclone_on_errors" setting in fsck.conf. If fsck/repack/prune
  comes across a matching error, it will mark the repository for
  recloning and it will be cloned anew from the master the next time
  grok-pull runs. This is useful for auto-correcting corruption on the
  mirrors. You can also manually request a reclone by creating a
  "grokmirror.reclone" file in a repository.
- Set extensions.preciousObjects for repositories used with git
  alternates if precious=yes is set in fsck.conf. This helps further
  protect shared repos from erroneous pruning (e.g. done manually by
  an administrator).


v1.1.1 (2018-07-25)
-------------------
- Quickfix a bug that was causing repositories to never be repacked
  due to miscalculated fingerprints.


v1.1.0 (2018-04-24)
-------------------
- Make Python3 compatible (thanks to QuLogic for most of the work)
- Rework grok-fsck to improve functionality:

  - run repack and prune before fsck, for optimal safety
  - add --connectivity flag to run fsck with --connectivity-only
  - add --repack-all-quick to trigger a quick repack of all repos
  - add --repack-all-full to trigger a full repack of all repositories
    using the defined full_repack_flags from fsck.conf
  - always run fsck with --no-dangling, because mirror admins are not
    responsible for cleaning those up anyway
  - no longer locking repos when running repack/prune/fsck, because
    these operations are safe as long as they are done by git itself

- fix grok-pull so it no longer purges repos that are providing
  alternates to others
- fix grok-fsck so it's more paranoid when pruning repos providing
  alternates to others (checks all repos on disk, not just manifest)
- in verbose mode, most commands will draw progress bars (handy with
  very large connections of repositories)
