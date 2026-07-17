from __future__ import annotations

import csv
import io
import json
import os
import threading
from pathlib import Path
from typing import NamedTuple, Optional

FIELDNAMES = ["date", "tag", "session_name", "minutes"]


class SessionRow(NamedTuple):
    date: str          # YYYY-MM-DD
    tag: str
    session_name: str
    minutes: int


# ------------------------------------------------------------------
# Path resolution
# ------------------------------------------------------------------

def get_data_dir() -> Path:
    """Return the directory used for sessions.csv and config.json.

    Priority:
    1. TRANQLI_DATA_DIR env var (used in tests / portable installs),
       or its legacy alias TRAENKY_DATA_DIR for one-release backward
       compat with prior installs.
    2. %APPDATA%\\Tranqli\\ on Windows
    3. ~/.tranqli/ fallback (Linux / WSL dev)

    A one-time migration runs at module import via
    `_migrate_legacy_data_if_needed()` to move pre-rename data
    (under the old "Traenky" name) into this new location.
    """
    override = (os.environ.get("TRANQLI_DATA_DIR")
                or os.environ.get("TRAENKY_DATA_DIR"))
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Tranqli"
    return Path.home() / ".tranqli"


def _legacy_data_dir() -> Optional[Path]:
    """Return the previous app-name's data directory if it exists on
    disk, else None. Used by the migration helper below.

    Checks the same priority order as get_data_dir() but for the
    pre-rename "Traenky" name. Does NOT honour the env-var override —
    if the user set TRANQLI_DATA_DIR explicitly, they're pointing at a
    deliberate location and we don't want to clobber whatever's there
    with a side-channel migration.
    """
    appdata = os.environ.get("APPDATA")
    candidates = []
    if appdata:
        candidates.append(Path(appdata) / "Traenky")
    candidates.append(Path.home() / ".traenky")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def migrate_legacy_data_if_needed() -> None:
    """One-time migration from the pre-rename Traenky data directory
    to the current Tranqli location.

    Runs on every startup but is a no-op in the steady state — only
    acts when the new directory doesn't yet exist AND the old one
    does. Implemented as a directory rename, so file mtimes are
    preserved and we don't briefly hold two copies of the user's
    session history.

    Safe on all platforms — `_legacy_data_dir()` returns None on
    systems where the old layout was never installed.

    Idempotent: if the user has already migrated, or has a fresh
    Tranqli install on top of a leftover Traenky directory, the new
    dir exists and we leave the old one alone (the user can delete
    it manually if they want).
    """
    new_dir = get_data_dir()
    if new_dir.exists():
        return
    old_dir = _legacy_data_dir()
    if old_dir is None:
        return
    try:
        new_dir.parent.mkdir(parents=True, exist_ok=True)
        old_dir.rename(new_dir)
    except OSError:
        # If the rename fails (cross-volume, permission, etc.) fall
        # back to a defensive copy + leave the old dir in place. The
        # user's data stays safe; on next launch we'll find the new
        # dir and skip migration entirely.
        import shutil
        try:
            shutil.copytree(old_dir, new_dir)
        except OSError:
            pass  # Best-effort — worst case is a "new user" experience.


def get_csv_path() -> Path:
    return get_data_dir() / "sessions.csv"


def get_config_path() -> Path:
    return get_data_dir() / "config.json"


def get_active_snapshot_path() -> Path:
    """Path for the crash-safety snapshot of the live session.
    Refreshed every ~3 min while tracking; deleted on save / discard /
    recovery. Lives alongside sessions.csv and config.json."""
    return get_data_dir() / "active_session.json"


def _ensure_dir() -> None:
    get_data_dir().mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------
# CSV read / write
# ------------------------------------------------------------------

def load_sessions() -> list[SessionRow]:
    path = get_csv_path()
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [
            SessionRow(
                date=row["date"],
                tag=row["tag"],
                session_name=row["session_name"],
                minutes=int(row["minutes"]),
            )
            for row in reader
        ]


# Serialises CSV mutation. Held across a whole read-modify-write — load
# the rows, apply the change, save — not merely the file write, because
# the damaging race is wider than the write itself: two threads that both
# load, then both save, each write a version derived from the same
# starting rows and the later save silently drops the earlier one's
# change. Narrower locking also leaves concurrent writers sharing one
# derived tmp path, where the first rename moves it away and the second
# dies on a file that no longer exists.
#
# Reachable in practice: the web editor saves on Flask's thread while the
# widget saves on Qt's. Covers same-process writers only — the app is
# single-instance, and two processes over one data dir would race on far
# more than this.
#
# Reentrant because the mutators hold it and then call save_sessions,
# which acquires it again; a plain Lock would deadlock on the nesting.
# Always taken before _undo_lock where both are held, so the ordering
# cannot invert into a deadlock.
_csv_lock = threading.RLock()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` crash-safely (brief §10).

    Writes a sibling .tmp then os.replace()s it onto the target. The
    rename is atomic within a filesystem, and the tmp is a sibling so
    the two are always on the same one. A kill mid-write leaves the
    original wholly intact rather than truncated — which a plain
    open("w") cannot promise, since it zeroes the file on open and any
    interruption before the rows are flushed takes the history with it.
    """
    _ensure_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _csv_lock:
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            # Don't leave a half-written .tmp behind to confuse the next
            # write (or the user looking at the data dir).
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


def _serialize_sessions(sessions: list[SessionRow]) -> bytes:
    """Render rows as the CSV file's exact on-disk bytes."""
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=FIELDNAMES)
    writer.writeheader()
    for row in sessions:
        writer.writerow(row._asdict())
    return buf.getvalue().encode("utf-8")


def _write_sessions(sessions: list[SessionRow]) -> None:
    """Persist rows without touching the undo stack.

    Split from save_sessions so undo() can restore a snapshot through
    the same crash-safe path without its own restore being recorded as
    a fresh undoable mutation.
    """
    _atomic_write_bytes(get_csv_path(), _serialize_sessions(sessions))


def save_sessions(sessions: list[SessionRow]) -> None:
    """Persist rows, recording the pre-write state for undo (spec §5).

    Callers that derived `sessions` from a prior load should hold
    _csv_lock across both, so the rows they modified are still the rows
    on disk. The mutators below do; this is also safe to call bare with
    a complete row set, as the web editor does.
    """
    with _csv_lock:
        snapshot = _capture_undo_snapshot()
        _write_sessions(sessions)
        # Recorded only once the write has actually landed. The snapshot
        # is of the pre-write state either way, so this preserves the
        # spec's "snapshot before the write" semantics while keeping a
        # write that raised — leaving the file untouched — from pushing
        # a no-op entry.
        _push_undo_snapshot(snapshot)


# ------------------------------------------------------------------
# Undo stack (spec §5)
# ------------------------------------------------------------------

# LIFO stack of whole-CSV snapshots, oldest first, capped at
# UNDO_STACK_DEPTH. In-memory only and deliberately not persisted: undo
# is a within-session convenience, and a stack on disk would be a second
# crash-safety surface to keep consistent with the CSV it describes.
UNDO_STACK_DEPTH = 8

# A snapshot is the CSV's raw bytes, or None meaning "no CSV existed
# yet". None is not the same as empty bytes: undoing the first-ever save
# must remove the file, not leave a header-only one behind.
_undo_stack: list[Optional[bytes]] = []

# save_sessions is reachable from both the Qt main thread and the Flask
# thread the web editor runs on (main.py injects _write_rows_for_web as
# webserver's write_rows callable), so the stack is genuinely shared
# state. The lock guards the stack only — the pre-existing
# load-modify-save race between those two writers is unchanged by undo.
_undo_lock = threading.Lock()


def _capture_undo_snapshot() -> Optional[bytes]:
    """Read the CSV's current bytes for the undo stack.

    Best-effort: an unreadable CSV yields None, which undo treats as
    "restore to no file". Failing to snapshot must never block the save
    the user actually asked for.
    """
    try:
        return get_csv_path().read_bytes()
    except OSError:
        return None


def _push_undo_snapshot(snapshot: Optional[bytes]) -> None:
    with _undo_lock:
        _undo_stack.append(snapshot)
        # Evict oldest beyond the cap. Slice-delete is a no-op while the
        # stack is under depth.
        del _undo_stack[:-UNDO_STACK_DEPTH]


def undo_depth() -> int:
    """How many mutations can currently be undone."""
    with _undo_lock:
        return len(_undo_stack)


def can_undo() -> bool:
    """Whether an undo is available — drives the greyed-out UI states."""
    with _undo_lock:
        return bool(_undo_stack)


def clear_undo_stack() -> None:
    """Drop all snapshots. Exists for tests: the stack is module state,
    so it would otherwise leak across them."""
    with _undo_lock:
        _undo_stack.clear()


def undo() -> bool:
    """Restore the most recent snapshot. Returns False if nothing to undo.

    Restores through the same crash-safe path as a normal write, so an
    interrupted undo can't destroy the file it's repairing. Deliberately
    records nothing itself — there is no redo (spec §5), and a restore
    that pushed its own snapshot would make undo un-undoable and burn a
    stack slot per press.

    Holds _csv_lock across pop-and-restore: popping a snapshot and then
    writing it is itself a read-modify-write, and a mutation landing in
    between would be silently reverted by the restore that follows it.
    _csv_lock is taken before _undo_lock here, matching save_sessions, so
    the two orderings can't invert.
    """
    with _csv_lock:
        with _undo_lock:
            if not _undo_stack:
                return False
            snapshot = _undo_stack.pop()

        path = get_csv_path()
        if snapshot is None:
            # The pre-write state had no CSV at all.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return True
        _atomic_write_bytes(path, snapshot)
        return True


# ------------------------------------------------------------------
# Merge logic
# ------------------------------------------------------------------

def merge_into(sessions: list[SessionRow], new_row: SessionRow) -> list[SessionRow]:
    """Add new_row's minutes to an existing (tag, date) row, or append it."""
    result: list[SessionRow] = []
    merged = False
    for row in sessions:
        if row.tag == new_row.tag and row.date == new_row.date:
            result.append(row._replace(minutes=row.minutes + new_row.minutes))
            merged = True
        else:
            result.append(row)
    if not merged:
        result.append(new_row)
    return result


def commit_session(
    tag: str,
    date_str: str,
    minutes: int,
    session_name: str | None = None,
) -> None:
    """Persist a session, merging into an existing (tag, date) row if present."""
    if session_name is None:
        session_name = f"{tag}-{date_str}"
    new_row = SessionRow(date=date_str, tag=tag, session_name=session_name, minutes=minutes)
    with _csv_lock:
        sessions = load_sessions()
        sessions = merge_into(sessions, new_row)
        save_sessions(sessions)


def set_minutes_for_tag_date(
    tag: str,
    date_str: str,
    minutes: int,
    session_name: str | None = None,
) -> None:
    """REPLACE (not add) the minutes for the (tag, date_str) row.

    Counterpart to commit_session, which adds. Drives the Retime
    flow in the menu and archive — the user is asserting an exact
    total for that day's tag, not contributing to a running sum.

    - minutes > 0, row exists: replace the row's minutes; keep its
      existing session_name (renaming is a separate concern).
    - minutes > 0, no row: create a new row using session_name or
      the default "tag-date_str" if not provided.
    - minutes == 0, row exists: drop the row. Matches the brief's
      "no row for an empty day" invariant.
    - minutes == 0, no row: no-op.
    """
    with _csv_lock:
        sessions = load_sessions()
        for i, row in enumerate(sessions):
            if row.tag == tag and row.date == date_str:
                if minutes == 0:
                    sessions.pop(i)
                else:
                    sessions[i] = row._replace(minutes=minutes)
                save_sessions(sessions)
                return
        if minutes > 0:
            if session_name is None:
                session_name = f"{tag}-{date_str}"
            sessions.append(SessionRow(
                date=date_str, tag=tag,
                session_name=session_name, minutes=minutes,
            ))
            save_sessions(sessions)


# ------------------------------------------------------------------
# Tag totals
# ------------------------------------------------------------------

def format_tag_total(minutes: int) -> str:
    """Format a minute count as 'Dd Hh' (brief §4 tag-total format).

    Days and hours are both zero-padded to 2; the days field grows past
    two digits naturally for multi-month aggregates. Sub-hour remainders
    are truncated, not rounded — a tag total is a coarse lifetime
    readout, not an exact duration.

    Examples:
        0      -> "00d 00h"
        90     -> "00d 01h"
        3000   -> "02d 02h"
        144000 -> "100d 00h"

    Deliberately NOT the cascading 'Xd Xh Xm' used by main.py's
    _format_dhm and the web editor's formatDhm. Those render individual
    session durations, where minutes matter; this renders per-tag
    lifetimes, which the brief specifies as days + hours only.
    """
    m = max(0, int(minutes))
    days = m // 1440
    hours = (m % 1440) // 60
    return f"{days:02d}d {hours:02d}h"


def tag_totals(sessions: list[SessionRow] | None = None) -> dict[str, str]:
    """Return {tag: 'DDd HHh'} summed across all rows for each tag."""
    if sessions is None:
        sessions = load_sessions()
    raw: dict[str, int] = {}
    for row in sessions:
        raw[row.tag] = raw.get(row.tag, 0) + row.minutes
    return {t: format_tag_total(m) for t, m in raw.items()}


# ------------------------------------------------------------------
# Session management
# ------------------------------------------------------------------

def rename_session(old_name: str, new_name: str) -> bool:
    """Rename a session by session_name. Returns True if found and renamed."""
    with _csv_lock:
        sessions = load_sessions()
        updated = [
            row._replace(session_name=new_name) if row.session_name == old_name else row
            for row in sessions
        ]
        if updated == list(sessions):
            return False
        save_sessions(updated)
        return True


def delete_session(session_name: str) -> bool:
    """Delete a session by session_name. Returns True if found and deleted."""
    with _csv_lock:
        sessions = load_sessions()
        kept = [row for row in sessions if row.session_name != session_name]
        if len(kept) == len(sessions):
            return False
        save_sessions(kept)
        return True


def retag_session(session_name: str, new_tag: str) -> bool:
    """Change a session's tag. Returns True if the session was found and
    updated.

    Handles the (tag, date) collision case: if the new tag combined with
    the session's date matches an existing different row, the two rows'
    minutes are summed into the existing one and the renamed row is
    dropped. This preserves the "one row per (tag, date)" invariant.
    """
    with _csv_lock:
        sessions = load_sessions()
        target_idx = None
        for i, row in enumerate(sessions):
            if row.session_name == session_name:
                target_idx = i
                break
        if target_idx is None:
            return False

        target = sessions[target_idx]
        if target.tag == new_tag:
            return True  # no-op, nothing to do

        # Look for a collision with another (tag, date) row.
        for i, row in enumerate(sessions):
            if i == target_idx:
                continue
            if row.tag == new_tag and row.date == target.date:
                # Merge target's minutes into the existing row, drop target.
                sessions[i] = row._replace(minutes=row.minutes + target.minutes)
                sessions.pop(target_idx)
                save_sessions(sessions)
                return True

        # No collision — straight tag swap.
        sessions[target_idx] = target._replace(tag=new_tag)
        save_sessions(sessions)
        return True


def rename_tag(old_tag: str, new_tag: str) -> bool:
    """Rename a tag across ALL stored sessions. Returns True if at
    least one row was affected (renamed or merged).

    Per-date collision handling: when an old_tag row and a new_tag
    row share the same date, the old row's minutes are folded into
    the new row and the old row is dropped — preserving the "one
    row per (tag, date)" invariant. Without a collision the row is
    simply renamed in place.

    session_name is NOT auto-updated. session_names are arbitrary
    user-mutable strings (the auto-generated `tag-date` form is just
    the default at first save). A user who renamed a session before
    renaming its tag has a reason for the name they chose; we don't
    second-guess it.

    No-ops cleanly on empty / whitespace / same-name input, returning
    False without touching storage.
    """
    old_tag = old_tag.strip()
    new_tag = new_tag.strip()
    if not old_tag or not new_tag or old_tag == new_tag:
        return False

    with _csv_lock:
        sessions = load_sessions()

        # Index existing new_tag rows by date — these are potential merge
        # targets when we encounter an old_tag row on the same date.
        new_by_date: dict[str, int] = {
            row.date: i for i, row in enumerate(sessions) if row.tag == new_tag
        }

        affected = False
        drop_indices: list[int] = []
        for i, row in enumerate(sessions):
            if row.tag != old_tag:
                continue
            affected = True
            if row.date in new_by_date:
                # Collision — merge into the existing new_tag row.
                target_idx = new_by_date[row.date]
                target = sessions[target_idx]
                sessions[target_idx] = target._replace(
                    minutes=target.minutes + row.minutes,
                )
                drop_indices.append(i)
            else:
                # No collision — rename in place. Register the row as a
                # merge target for any subsequent old_tag rows on the same
                # date (shouldn't happen given the storage invariant, but
                # keeps the loop self-consistent).
                sessions[i] = row._replace(tag=new_tag)
                new_by_date[row.date] = i

        if not affected:
            return False

        # Drop merged rows in reverse so earlier indices stay valid.
        for i in sorted(drop_indices, reverse=True):
            sessions.pop(i)
        save_sessions(sessions)
        return True


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

# Bumped whenever the config schema needs migration. Pre-rename configs
# have no version field at all (treated as v1).
CURRENT_CONFIG_VERSION = 3

# Cap for the recent_tags MRU list. The Tags menu only ever displays 5,
# but we keep more than we show so the "More..." surface has history to
# draw on without a second store.
RECENT_TAGS_MAX = 20

# Migration map for the widget_size rename. The four-tier scheme
# (mini / small / medium / large = 13 / 22 / 48 / 64 px) was collapsed
# into a three-tier scheme (small / medium / large = 13 / 22 / 48 px,
# dropping 64 px). The old 'large' falls back to the new 'large'
# (48 px) since 64 px no longer exists; everything else shifts down a
# slot so the user's physical size is preserved.
_WIDGET_SIZE_MIGRATION_V1_TO_V2 = {
    "mini":   "small",
    "small":  "medium",
    "medium": "large",
    "large":  "large",
}


def _seed_recent_tags() -> list[str]:
    """Build an initial recent_tags MRU from existing session history.

    Distinct tags ordered by their most recent date, newest first,
    capped at RECENT_TAGS_MAX. Without this an upgrading user starts
    with an empty MRU and the fresh-launch picker treats them as a
    first-ever user, even with years of history behind them.

    Best-effort by contract: this runs inside load_config(), which runs
    at startup before anything is on screen, so a damaged or unreadable
    sessions.csv must not stop the app from launching. A failed seed
    costs an empty Tags menu that refills as soon as tags get used; a
    raised exception would cost the whole app. Returns [] on any read
    problem and lets the version bump proceed regardless.

    Dates are day-granular, so tags last used on the same day have no
    true ordering between them. Python's stable sort keeps them in CSV
    order — arbitrary but deterministic, which is all the MRU needs.
    """
    try:
        sessions = load_sessions()
    except (OSError, csv.Error, KeyError, ValueError, TypeError):
        return []
    latest: dict[str, str] = {}
    for row in sessions:
        if not row.tag:
            continue
        if row.tag not in latest or row.date > latest[row.tag]:
            latest[row.tag] = row.date
    ordered = sorted(latest, key=lambda t: latest[t], reverse=True)
    return ordered[:RECENT_TAGS_MAX]


def _new_config() -> dict:
    """Config for a first-ever launch, stamped at the current version.

    The stamp is the point. An unstamped config is indistinguishable
    from a genuine v1 one on the next load, so _migrate_config would
    read version 1 and apply the v1->v2 size remap to a widget_size the
    user had just picked — silently bumping their choice up a tier.

    Seeds the MRU the same way the v2->v3 migration does: no config but
    an existing sessions.csv is a user with history (config deleted, or
    restored from a partial backup), not a new one.
    """
    return {
        "config_version": CURRENT_CONFIG_VERSION,
        "recent_tags": _seed_recent_tags(),
        "tag_schemes": {},
    }


def _migrate_config(config: dict) -> tuple[dict, bool]:
    """Apply any one-time field migrations. Returns (config, changed).

    Steps cascade: `version` is read once, so a v1 config falls through
    every block below and lands on the current version.
    """
    changed = False
    version = config.get("config_version", 1)

    if version < 2:
        size = config.get("widget_size")
        if size in _WIDGET_SIZE_MIGRATION_V1_TO_V2:
            config["widget_size"] = _WIDGET_SIZE_MIGRATION_V1_TO_V2[size]
        config["config_version"] = 2
        changed = True

    if version < 3:
        # Tag MRU list and per-tag colour scheme, both introduced by the
        # tag-management work. Guarded on absence rather than written
        # unconditionally so a config already carrying these keys keeps
        # its data — and so the seed's CSV read is skipped entirely when
        # there's nothing to seed.
        if "recent_tags" not in config:
            config["recent_tags"] = _seed_recent_tags()
        config.setdefault("tag_schemes", {})
        config["config_version"] = 3
        changed = True

    return config, changed


def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        return _new_config()
    with path.open(encoding="utf-8") as f:
        config = json.load(f)
    config, migrated = _migrate_config(config)
    if migrated:
        # Persist the migration so subsequent loads are clean.
        save_config(config)
    return config


def save_config(config: dict) -> None:
    _ensure_dir()
    with get_config_path().open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# ------------------------------------------------------------------
# Recent-tags MRU (spec §2c step 3)
# ------------------------------------------------------------------
#
# last_tag predates recent_tags and holds the same fact as
# recent_tags[0]. Rather than repoint its two readers — auto-resume at
# launch and the tag picker's default — it is kept as a mirror,
# maintained by the two helpers below. That makes them the only places
# either key is written, so the pair cannot drift; a direct assignment
# to config["last_tag"] anywhere else reintroduces exactly that risk.
#
# Both mutate `config` in place and leave persistence to the caller,
# matching how main.py holds a config dict and calls save_config itself.


def push_recent_tag(config: dict, tag: str) -> None:
    """Move `tag` to the front of the MRU, most-recent first.

    Deduplicates, so a re-picked tag moves rather than repeats, and caps
    at RECENT_TAGS_MAX. Blank tags are ignored rather than stored — the
    menu has no use for an empty entry, and the tracker treats "no tag"
    as None, not "".
    """
    tag = (tag or "").strip()
    if not tag:
        return
    recent = [t for t in config.get("recent_tags", []) if t != tag]
    recent.insert(0, tag)
    config["recent_tags"] = recent[:RECENT_TAGS_MAX]
    config["last_tag"] = tag


def rename_recent_tag(config: dict, old_tag: str, new_tag: str) -> None:
    """Follow a tag rename through the MRU, preserving its position.

    A rename is not a use: the tag keeps its place in the order rather
    than jumping to the front. Without this the MRU would keep offering
    a name that no longer exists in the CSV, and last_tag would drift
    away from recent_tags[0] the moment the active tag was renamed.

    If the new name is already present, the two entries merge and the
    earlier (more recent) position wins — mirroring the way rename_tag
    folds colliding rows together in storage.
    """
    old_tag = (old_tag or "").strip()
    new_tag = (new_tag or "").strip()
    if not old_tag or not new_tag or old_tag == new_tag:
        return

    renamed = [
        new_tag if t == old_tag else t
        for t in config.get("recent_tags", [])
    ]
    deduped: list[str] = []
    for t in renamed:
        if t not in deduped:
            deduped.append(t)
    config["recent_tags"] = deduped[:RECENT_TAGS_MAX]

    if (config.get("last_tag") or "").strip() == old_tag:
        config["last_tag"] = new_tag


# ------------------------------------------------------------------
# Seed-mode lookup
# ------------------------------------------------------------------

def today_minutes_for_tag(tag: str, date_str: str) -> int:
    """Return saved minutes for the (tag, date_str) row, or 0 if no
    such row exists. Drives the seed-mode display: when a tag is
    assigned to an active session, the widget's elapsed total is
    pre-loaded with whatever's already on disk for that tag today so
    the displayed time reflects the day's running cumulative rather
    than starting from zero.

    Streams the CSV directly via `csv.DictReader` with short-circuit
    return on first match — avoids parsing the entire file and
    constructing a SessionRow for every line just to find one row.
    Important at startup (called from _auto_resume_today_if_any),
    where the old `load_sessions()` path scaled linearly with total
    session history.

    Tolerant of malformed rows: any row whose `minutes` cell can't
    be parsed as int is silently skipped (treated as no match),
    matching the rest of the codebase's "best-effort, never crash
    startup" stance on CSV I/O.
    """
    path = get_csv_path()
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("tag") == tag and row.get("date") == date_str:
                try:
                    return int(row.get("minutes", 0))
                except (TypeError, ValueError):
                    return 0
    return 0


# ------------------------------------------------------------------
# Crash-safety snapshot
# ------------------------------------------------------------------
#
# A small JSON file written every ~3 min while the tracker is running,
# refreshed on every pause/resume transition, and removed on save /
# discard / clean reset. If the process is killed between save points,
# the file remains and the next launch can prompt the user to recover
# the unsaved work into the appropriate (tag, date) row.
#
# Format (all fields optional except `tag` and `elapsed_seconds`):
#   {
#     "tag":              "work",
#     "elapsed_seconds":  1842,
#     "date":             "2026-05-31",   # date the work belongs to
#     "snapshot_at":      "2026-05-31T16:42:11"  # ISO timestamp
#   }
#
# Worst-case loss: ~3 min of work (one snapshot interval).


def write_active_snapshot(data: dict) -> None:
    """Atomically persist the active session snapshot.

    Writes to a sibling .tmp file then os.replace()s it onto the real
    path — same crash-safe pattern the brief specifies for sessions.csv.
    A process kill mid-write leaves either the old snapshot intact or
    the fully-written new one, never a half-written file."""
    _ensure_dir()
    path = get_active_snapshot_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def read_active_snapshot() -> dict | None:
    """Return the parsed snapshot dict if a snapshot file is present
    and parseable. None if no file exists, or if the file is corrupt /
    unreadable (treated as no recoverable snapshot rather than
    crashing the recovery prompt at startup)."""
    path = get_active_snapshot_path()
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def clear_active_snapshot() -> None:
    """Remove the snapshot file. Called after the in-flight session
    has been safely persisted (save), explicitly discarded, or
    recovered into storage. A missing file is a no-op."""
    path = get_active_snapshot_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------
# Defensive backup
# ------------------------------------------------------------------
#
# A single sessions.csv.bak file refreshed at most once per 24 h.
# Cheap insurance against UI bugs, accidental web-editor deletes,
# or any other path that could mangle the live CSV.
#
# Restore procedure: quit the app, rename sessions.csv aside (e.g.
# to sessions.csv.broken), rename sessions.csv.bak to sessions.csv,
# relaunch.


def maybe_backup_sessions(max_age_hours: int = 24) -> None:
    """Refresh sessions.csv.bak if no recent backup exists.

    Single-file rotating backup — sessions.csv.bak lives alongside
    the live CSV in get_data_dir(). Refreshed once every
    `max_age_hours` (default 24) so frequent app restarts within
    a day don't immediately overwrite a known-good backup with
    later state. Best-effort: copy failures are swallowed (the
    backup is a safety net, not a hard requirement, and we don't
    want a broken filesystem to prevent the app from starting).
    """
    import shutil
    import time as _time
    src = get_csv_path()
    if not src.exists():
        return
    backup = src.parent / "sessions.csv.bak"
    if backup.exists():
        age = _time.time() - backup.stat().st_mtime
        if age < max_age_hours * 3600:
            return  # recent backup exists, leave it alone
    try:
        shutil.copy2(src, backup)
    except OSError:
        # Non-fatal — backup is best-effort. Continuing without
        # a fresh backup is preferable to crashing the app on
        # a transient FS error.
        pass
