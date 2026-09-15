"""Schema migrations for the on-disk state.

A schema version describes the SHAPE OF THE STATE, not the build that wrote it.
It moves only when existing data has to be rewritten, so most releases leave it
alone — which is why it is a separate number from `__version__`.

Migrations are numbered, one per schema change, and append-only. Once a step can
have been applied anywhere, editing it is undetectable and unfixable: someone's
state is stamped with a number that no longer means what it meant when they ran
it. Corrections arrive as a new step, never as an edit to an old one.

Which steps run is decided at two ends that neither derives from the other:

    source  <- the state's own marker (`migrations/version`); what has been applied here
    target  <- CURRENT_VERSION, compiled into this binary; what this build knows

The release version cannot stand in for the source. It says what code is
running, never what shape the state is in — and after a retried or interrupted
update those differ.
"""
import contextlib
import logging
import os
import shutil
from pathlib import Path

from .store import MigrationStore, StateError, restrict
from . import m0001_id_keyed_grants

logger = logging.getLogger("steerholm.migrations")

# What this build knows how to produce.
CURRENT_VERSION = 1

# State with no version marker predates versioning. Both releases that could
# have written one (0.1.0, 0.1.1) wrote the same shape, so the baseline is a
# constant rather than a lookup. Bootstrap only: every state from here on
# carries the marker.
UNVERSIONED_BASELINE = 0

# The three working directories, all INSIDE the state directory.
#
# Each holds a copy of `config.json`, so each holds every `--env` secret the real
# one does. As siblings they sat outside the 0700 that protects the state
# directory and had to be hardened separately — and a user auditing or tightening
# `~/.steerholm` would not have seen them at all. Inside, they inherit that
# protection, they travel with the state when it is moved or backed up, and the
# names they create live in a directory `_swap` already fsyncs.
#
# Nothing stages or enumerates them: `_stage` copies only the MIGRATED names, and
# the audit log matches `events-<digits>.jsonl` exactly.
#
# There is one backup and its name carries no version — which schema it holds is
# recorded inside it, in `migrations/version`, exactly as for live state.
BACKUP_DIR = ".pre-migration"
STAGING_DIR = ".migrating-v{step}"
SCRATCH_DIR = ".displaced-v{step}"

# Each step takes a MigrationStore and rewrites it in place. A step only ever
# sees the shape of the version BEFORE it, and it does not have to be idempotent
# — the engine guarantees it is never handed its own output.
#
# That guarantee needs explaining, because it is not free. `_swap` renames the
# migrated names in one at a time and commits the marker last, atomically
# (marker first would leave the marker ahead of the data, which nothing re-runs,
# and a single atomic whole-directory swap is impossible: POSIX rename will not
# overwrite a non-empty directory). So a crash mid-swap leaves converted data
# live with the version still reading the old number. What stops the retry from re-running the
# step against that data is `_resumable`: the staging copy survives the crash,
# the names still in it are exactly the ones that never got swapped, and the
# retry finishes the swap instead of redoing the work.
MIGRATIONS = {
    1: m0001_id_keyed_grants.migrate,
}

MARKER_DIR, MARKER_FILE = "migrations", "version"

# What a step may rewrite, and therefore what gets staged and swapped. The
# marker is NOT here: `_commit_marker` writes it directly, so staging it would
# copy a file that is unlinked before the step runs and replaced after it. The
# audit log is absent too — it is rotated, can be tens of megabytes, and no step
# touches it. The keyring is outside the state directory entirely.
MIGRATED = ("config.json", "policies")


class MigrationError(Exception):
    """A migration could not be applied. The state is unchanged."""


def resolve_version(store: MigrationStore) -> int:
    """What schema a state is in, marker or not.

    The store reports whether a marker exists. Turning "no marker" into a number
    is a statement about which releases wrote which shape, so it lives here with
    the migrations rather than in the store, which should outlast any particular
    era of the format.
    """
    marked = store.version
    return UNVERSIONED_BASELINE if marked is None else marked


def _claim(lock: Path) -> int:
    return os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)


def _busy(root: Path, lock: Path) -> "MigrationError":
    return MigrationError(
        f"Another migration is running against {root} ({lock} exists). "
        "If nothing is running, remove that file and retry."
    )


def _owner_is_gone(lock: Path) -> bool:
    """Whether the process that wrote the lock has exited.

    The PID is written into the lock for exactly this. Conservative in every
    uncertain case — an unreadable lock, a PID that is not ours to signal, or
    Windows, where `os.kill` cannot ask the question — because wrongly clearing
    a live lock lets two migrations share one staging directory.
    """
    if os.name == "nt":
        return False
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        return False
    # Not a self-check as well: `os.kill(our own pid, 0)` succeeds and falls
    # through to the same answer. Zero and negatives DO need refusing — they
    # address process groups rather than a process.
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False        # alive, or not ours to signal
    return False


@contextlib.contextmanager
def _exclusive(root: Path):
    """Hold the state directory for the duration of a migration.

    `_derived_id` is deterministic so two runs would agree on the ids they
    produce, but they share the staging, backup and scratch directories —
    one would
    rmtree the other's staging mid-copy, or swap the other's already-migrated
    files into what it labels the previous state.
    """
    root.mkdir(parents=True, exist_ok=True)
    restrict(root, 0o700)   # it is about to receive config.json and every policy
    lock = root / ".migrating.lock"
    try:
        fd = _claim(lock)
    except FileExistsError:
        if _owner_is_gone(lock):
            # The owning process died — a SIGKILL, or the power going out
            # mid-migration. Without this the leftover file refuses every future
            # run, which means every future install: both installers abort when
            # `holm migrate` fails.
            logger.warning("Clearing a lock left by a process that is gone: %s", lock)
            lock.unlink(missing_ok=True)
            try:
                fd = _claim(lock)
            except FileExistsError:
                raise _busy(root, lock) from None
        else:
            raise _busy(root, lock) from None
    try:
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            # Closed even if the write raises: on Windows an open handle makes
            # the unlink below fail, producing exactly the stale lock this all
            # exists to avoid.
            os.close(fd)
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def run(root: Path) -> int:
    """Bring the state directory up to CURRENT_VERSION. Returns steps applied.

    Each step runs against a staged copy and is swapped in only if it succeeds,
    so a step that fails partway leaves the live directory untouched. The
    version marker is bumped inside the staged copy, so the data and the number
    describing it arrive together — a step cannot end with the marker claiming a
    shape the files are not in.
    """
    with _exclusive(root):
        return _run_locked(root)


def _run_locked(root: Path) -> int:
    store = MigrationStore(root)
    try:
        current = resolve_version(store)
    except StateError as e:
        raise MigrationError(str(e)) from e
    if current > CURRENT_VERSION:
        raise MigrationError(
            f"{root} is schema v{current}; this build understands "
            f"v{CURRENT_VERSION}. There are no reverse migrations — upgrade "
            "Steerholm instead of downgrading it."
        )

    steps = range(current + 1, CURRENT_VERSION + 1)
    missing = [n for n in steps if n not in MIGRATIONS]
    if missing:
        # Checked before the first one runs: applying part of the range and then
        # stopping would leave the state at an intermediate version while the
        # error still reported the version it started at.
        raise MigrationError(
            f"{root} is schema v{current} and this build is missing the "
            f"migration{'s' if len(missing) > 1 else ''} for "
            f"v{', v'.join(str(n) for n in missing)}. This is a packaging fault, "
            "not a problem with your state."
        )

    if steps:
        _refuse_if_stranded(root, steps)

    applied = 0
    for step in steps:
        # Only the FIRST step's displaced files are the state the user was
        # running. Later steps displace intermediates that no build wants.
        _apply(root, step, MIGRATIONS[step], first=applied == 0)
        applied += 1
        logger.info("Applied schema migration %d.", step)

    _discard_spent_work(root, resolve_version(MigrationStore(root)))
    return applied


def _discard_spent_work(root: Path, current: int) -> None:
    """Remove staging and scratch directories for steps that are already in.

    `_apply` clears its own, but only when it gets that far. A crash between the
    final rename and that cleanup leaves staging behind; a swap that RAISES
    skips the scratch cleanup entirely, and nothing else ever removes one. Both
    hold a copy of `config.json`, secrets included.

    Bounded to steps at or below the version now live, so a scratch still
    holding files a rollback could not restore is left for `_refuse_if_stranded`
    to report rather than quietly deleted.
    """
    for pattern in (".migrating-v*", ".displaced-v*"):
        for path in root.glob(pattern):
            try:
                step = int(path.name.rsplit("-v", 1)[1])
            except (IndexError, ValueError):
                continue
            if step <= current:
                logger.info("Removing spent migration working state: %s", path)
                shutil.rmtree(path, ignore_errors=True)


def _refuse_if_stranded(root: Path, steps) -> None:
    """Stop if a previous run left part of the state only in a backup.

    A rollback that could not put a directory back leaves, say, `policies/`
    present only in the backup. Migrating from there would convert a state
    with no policies at all and stamp it current — turning "the grants are in
    the backup" into "the grants are gone".

    Only a PARTIAL state counts. A directory holding none of it was cleared,
    not stranded.
    """
    if not any((root / n).exists() for n in MIGRATED):
        # Nothing of the state is live. That is not a half-finished rollback —
        # it is a directory the user cleared, or one this run just created. A
        # backup outlives the migration that made it, so refusing here would
        # fail every later run against a state that was reset on purpose, and
        # both installers abort the whole install when `holm migrate` fails.
        # There is also nothing to lose: migrating an empty state writes
        # nothing the backup holds.
        return

    # A name a resumable staging copy still holds is not stranded, it is
    # pending: `_apply` will swap it in. Refusing on it would block exactly the
    # crash this engine can recover from — a swap interrupted partway, with the
    # originals already moved into the backup and the replacements waiting.
    pending = set()
    for step in steps:
        staging = root / STAGING_DIR.format(step=step)
        if _resumable(staging, step):
            pending |= {n for n in MIGRATED if (staging / n).exists()}

    backup = backup_path(root)
    missing = [n for n in MIGRATED
               if (backup / n).exists() and not (root / n).exists()
               and n not in pending]
    if missing:
        raise MigrationError(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
            f"missing from {root} but present in {backup}. A previous "
            "migration could not finish restoring it. Move it back before "
            "migrating, or the grants it holds will be lost."
        )


def _resumable(staging: Path, step: int) -> bool:
    """Whether a leftover staging directory can be swapped in as it stands.

    `_apply` writes the staged marker LAST, once the step has finished
    rewriting the copy. So a staging directory still carrying it is one whose
    step SUCCEEDED and whose swap was interrupted partway through, and the names
    left in it are exactly the ones that never made it across — `_swap` renames
    them out one at a time, so what remains is the work remaining. A staging
    directory without the marker was abandoned mid-step and says nothing about
    what is correct; it gets discarded and the step redone from live.

    This is why a separate progress file would buy nothing. A flag has to be
    written AFTER the rename it records, so a crash in between leaves it lying.
    The renames maintain this record themselves.
    """
    try:
        return MigrationStore(staging).version == step
    except StateError:
        return False


def _apply(root: Path, step: int, migrate, first: bool) -> None:
    """Run one step and swap it in.

    The live files the swap displaces have to go somewhere — that is also how a
    failed swap puts them back. For the FIRST step of a run they are the state
    the user was running, so that somewhere is the backup. For any later step
    they are an intermediate schema no build wants, so it is scratch, deleted
    once the swap succeeds.

    Whether the existing backup is stale is not a question about versions, it is
    a question about whether the previous run finished — and `_resumable`
    answers that directly, from the staging copy it left behind.
    """
    staging = root / STAGING_DIR.format(step=step)
    scratch = root / SCRATCH_DIR.format(step=step)
    resuming = _resumable(staging, step)

    # `first` describes this run's position, and a retry's first pending step
    # may have been a LATER step of the run that was interrupted — a v0->v1->v2
    # run that died in step 2 retries as a run whose only step is 2. Following
    # `first` there would displace into a backup already holding step 1's
    # originals, and `os.replace` cannot put a directory over a non-empty one:
    # every retry fails identically, forever. A resume continues filling
    # whatever the interrupted run was filling, and the scratch it left behind
    # is the evidence of which that was.
    if resuming and scratch.exists():
        displaced, keep = scratch, False
    else:
        displaced, keep = (backup_path(root), True) if first else (scratch, False)

    if resuming:
        # Finish the interrupted swap rather than redoing the step. The step
        # already ran to completion against this copy; re-running it would hand
        # it the data it produced, which no step should have to expect. The
        # backup keeps filling where that run left off — `_swap` skips the names
        # staging no longer holds, so what is left to displace is exactly what
        # the interrupted run never reached, still untouched originals.
        logger.info("Resuming an interrupted swap for schema migration %d.", step)
    else:
        _rebuild_staging(root, staging, step, migrate)
        # Whatever is there belongs to a run that finished; this one supersedes
        # it. Not cleared before the step: dropping it and then failing would
        # leave the user with neither.
        shutil.rmtree(displaced, ignore_errors=True)

    _swap(root, staging, displaced, step)
    if not keep:
        shutil.rmtree(displaced, ignore_errors=True)


def _rebuild_staging(root: Path, staging: Path, step: int, migrate) -> None:
    """Make a fresh copy and run the step against it."""
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _stage(root, staging)
        staged = MigrationStore(staging)
        # Staging carries no marker until the step succeeds, so a step reading
        # `store.version` sees None rather than its predecessor's value.
        migrate(staged)
        staged.version = step
    except StateError as e:
        # Already says what is wrong with the state and what to do about it;
        # wrapping it in "failed; nothing was changed" would bury that. The
        # staging path is rewritten to the live one: the copy is deleted on the
        # next line, so naming it would send the user to a directory that no
        # longer exists.
        shutil.rmtree(staging, ignore_errors=True)
        raise MigrationError(str(e).replace(str(staging), str(root))) from e
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise MigrationError(f"Migration {step} failed; nothing was changed: {e}") from e


def backup_path(root: Path) -> Path:
    """Where the pre-migration state is kept."""
    return root / BACKUP_DIR



def _stage(root: Path, staging: Path) -> None:
    """Copy what a step may rewrite into the staging directory."""
    staging.mkdir(parents=True)
    restrict(staging, 0o700)
    for name in MIGRATED:
        src = root / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, staging / name, symlinks=True)
            restrict(staging / name, 0o700)
        else:
            shutil.copy2(src, staging / name)


def _swap(root: Path, staging: Path, backup: Path, step: int) -> None:
    """Move the migrated files into place, keeping the originals.

    Per-file rather than swapping whole directories: the state directory also
    holds the audit log, which is deliberately not staged and must not be moved
    aside with everything else.

    The marker is committed LAST, by `_commit_marker`, and as a single atomic
    file replacement rather than as one of the renames below. A crash before it
    therefore leaves a state that still reads as the old version — the one
    ordering that cannot leave the marker ahead of the data, and the reason the
    marker is never moved aside: a state with NO marker reads as pre-versioning
    and would send every retry back through steps that are already in.
    Re-running resumes from the staging copy, which is why the failure path
    below keeps it when nothing was rolled back.
    """
    moved = []
    ordered = MIGRATED
    try:
        # Inside the try: a read-only or full home fails here, and leaving it
        # outside raised a bare OSError past `main.migrate`'s handler — printing
        # a traceback under the installer's "fix the cause above" — while the
        # staging copy, config secrets and all, stayed on disk.
        # exist_ok: on a resume the backup is already there and half filled by
        # the interrupted run, and the names this swap displaces belong beside
        # the ones it already holds.
        backup.mkdir(parents=True, exist_ok=True)
        restrict(backup, 0o700)   # holds a copy of the state, secrets included
        for name in ordered:
            live, staged = root / name, staging / name
            if not staged.exists():
                continue
            if live.exists():
                os.replace(live, backup / name)
                # Recorded BEFORE the second rename, not after: if that one
                # fails, this name has already left the live directory and the
                # rollback is the only thing that can put it back.
                moved.append(name)
            os.replace(staged, live)

        # Everything else is in place. Flush it before the marker follows, or
        # the device is free to order the marker ahead of the data — leaving a
        # state that reads as migrated while `policies/` still holds the old
        # shape, which nothing would ever re-run.
        _fsync_dir(root)
        _commit_marker(root, staging, backup)
    except OSError as e:
        stranded = []
        for name in reversed(moved):          # put back what we moved
            with_backup = backup / name
            if not with_backup.exists():
                continue
            try:
                # Where the swap already succeeded, the staged copy is sitting
                # at the live path. `os.replace` cannot put a directory over a
                # non-empty directory, so clear it first — a rollback is all or
                # nothing.
                live = root / name
                if live.is_dir() and not live.is_symlink():
                    shutil.rmtree(live)
                os.replace(with_backup, live)
            except OSError:
                # Best-effort: a rollback that fails must not replace the real
                # cause with its own, but the operator has to be told which
                # pieces are only in the backup now.
                stranded.append(name)
        detail = ""
        if stranded:
            detail = (f" Could not restore {', '.join(stranded)} — "
                      f"{'it is' if len(stranded) == 1 else 'they are'} only at {backup}.")
        if moved:
            # The rollback put live back the way it was, which makes staging an
            # INVALID resume source: `_swap` consumes a name out of staging as
            # it swaps it in, and `_resumable` reads "not in staging" as
            # "already live". After a rollback that is false — the name is live
            # again, but as the ORIGINAL. Resuming would skip it and leave it
            # unmigrated, or worse, move only the marker and stamp the new
            # version over old data. Redo the step from live instead.
            shutil.rmtree(staging, ignore_errors=True)
        # Otherwise staging is kept: nothing was displaced, so it still matches
        # live exactly, and the retry can finish the swap rather than redo the
        # step against data this one may already have converted.
        raise MigrationError(
            f"Migration {step} could not be swapped in: {e}. The previous state "
            f"is at {backup}.{detail}"
        ) from e
    # Both. `backup` carries the originals just renamed into it; `root`
    # carries the swapped-in names AND `backup`'s own name, which
    # `backup.mkdir` created — so one flush of `root` covers what previously
    # needed the parent too, now that the working directories live inside.
    _fsync_dir(backup)
    _fsync_dir(root)
    shutil.rmtree(staging, ignore_errors=True)


def _commit_marker(root: Path, staging: Path, backup: Path) -> None:
    """Replace the version marker. This is the step's commit point.

    The marker is swapped as a FILE, not by moving its directory aside and
    renaming the new one in. Two renames leave a window where the state carries
    NO marker, which reads as pre-versioning — and a crash there is unrecoverable
    by retrying: the state holds vN data while reporting v0, so every retry
    re-runs steps that are already in, against data they produced, and jams on
    a backup it cannot overwrite. `os.replace` over a file is atomic, so the
    marker is only ever the old value or the new one.

    The old marker is COPIED into the backup rather than moved, for the same
    reason. It is a dozen bytes.
    """
    # `staged` always exists here: a resumed swap needed it to be recognised as
    # resumable, and a fresh one has just written it.
    staged = staging / MARKER_DIR / MARKER_FILE
    live_dir = root / MARKER_DIR
    live_dir.mkdir(parents=True, exist_ok=True)
    restrict(live_dir, 0o700)
    live = live_dir / MARKER_FILE
    if live.exists():
        kept = backup / MARKER_DIR
        kept.mkdir(parents=True, exist_ok=True)
        restrict(kept, 0o700)
        shutil.copy2(live, kept / MARKER_FILE)
    os.replace(staged, live)


def _fsync_dir(path: Path) -> None:
    """Flush a directory's entries.

    The ordering `_swap` relies on — data first, marker last — is only a
    guarantee once the renames are durable. Without this a crash can make the
    marker visible and the data not, which is the inversion it exists to stop.
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
