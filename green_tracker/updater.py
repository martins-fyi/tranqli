"""
updater.py — lightweight "a newer version exists" check (no auto-updater).

On startup, at most once per calendar day, we ask GitHub for the latest
published release and compare it to the running version. If it's newer we
surface it two ways:

  - a passive "Update Available" menu item, shown whenever a newer version
    is known (no throttling — it just sits there until clicked); and
  - an interruptive popup, throttled separately so it can't nag more than
    once every POPUP_MIN_INTERVAL_DAYS even if several releases land close
    together.

Nothing here downloads or installs anything — the "Update" action just
opens the releases page in the browser. The network call
(fetch_latest_version) MUST only run on the worker thread, never the UI
thread, and fails completely silently (returns None) on any error.

All the config helpers read/write the config["update_check"] block that
storage's v4 migration adds; they also self-heal a missing block so they
work if handed a bare dict (e.g. in tests).
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date

from PySide6.QtCore import QThread, Signal

# Human-facing releases page — the "Update" button and menu item open this.
GITHUB_RELEASE_URL = "https://github.com/martins-fyi/tranqli/releases/latest"
# JSON API for the actual check. /releases/latest returns only the newest
# PUBLISHED, non-draft, non-prerelease release — which is what lets us
# "release only when ready" and have clients notice exactly then.
_GITHUB_API_URL = (
    "https://api.github.com/repos/martins-fyi/tranqli/releases/latest"
)
_TIMEOUT = 4  # seconds; the check is best-effort, never block the app on it

# How long the interruptive popup stays quiet after showing once. Bump this
# to nag less often; it is intentionally the one knob for popup frequency.
# (The passive menu item is NOT throttled — only this popup is.)
POPUP_MIN_INTERVAL_DAYS = 3


# ---------------------------------------------------------------------------
# Version parsing / comparison
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple[int, ...]:
    """Parse "v0.2.1" / "0.2.1" into (0, 2, 1).

    Strips a leading "v"/"V", splits on ".", and takes the leading digits
    of each part (so a stray suffix like "1-beta" reads as 1; a part with
    no digits reads as 0). Never raises on well-formed-ish input.
    """
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    parts: list[int] = []
    for part in v.split("."):
        # Leading digits only, so a stray suffix is dropped rather than
        # merged in: "2rc3" -> 2, "1-beta" -> 1, "beta" -> 0.
        lead = ""
        for ch in part:
            if ch.isdigit():
                lead += ch
            else:
                break
        parts.append(int(lead) if lead else 0)
    return tuple(parts)


def is_newer(remote: str, local: str) -> bool:
    """True if `remote` is a strictly newer version than `local`.

    Pads both tuples to equal length with zeros first, so "0.2" and
    "0.2.0" compare equal rather than one looking newer than the other.
    """
    r, lo = _parse_version(remote), _parse_version(local)
    n = max(len(r), len(lo))
    r = r + (0,) * (n - len(r))
    lo = lo + (0,) * (n - len(lo))
    return r > lo


# ---------------------------------------------------------------------------
# Network (worker thread only)
# ---------------------------------------------------------------------------

def fetch_latest_version() -> str | None:
    """Return the latest release's version (leading "v" stripped), or None.

    Blocking GET to the GitHub API. WORKER THREAD ONLY — never call this on
    the UI thread. Fails completely silently: any network / timeout / HTTP
    / JSON / shape error returns None, never raises, never surfaces to the
    user.
    """
    try:
        req = urllib.request.Request(
            _GITHUB_API_URL,
            headers={
                "User-Agent": "Tranqli-update-check",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").strip()
        if not tag:
            return None
        if tag[:1] in ("v", "V"):
            tag = tag[1:]
        return tag or None
    except Exception:
        # Deliberately broad — this must never take the app down or nag the
        # user about a failed check. Offline is a non-event.
        return None


class UpdateCheckWorker(QThread):
    """Runs fetch_latest_version() off the UI thread.

    Emits finished_check with the result (a version str, or None on any
    failure). Hold a reference to the instance until it finishes — an
    unreferenced QThread can be garbage-collected mid-run.
    """

    finished_check = Signal(object)  # str | None

    def run(self) -> None:  # noqa: D401 - QThread entry point
        self.finished_check.emit(fetch_latest_version())


# ---------------------------------------------------------------------------
# Config state helpers  (operate on the config["update_check"] block)
# ---------------------------------------------------------------------------

def _uc(config: dict) -> dict:
    """The update_check block, created if absent (storage v4 adds it; this
    self-heal keeps the helpers usable with a bare config in tests)."""
    return config.setdefault(
        "update_check",
        {
            "last_checked": None,
            "latest_version": None,
            "dismissed_version": None,
            "last_popup_shown": None,
        },
    )


def _today_iso() -> str:
    return date.today().isoformat()


def _days_since(iso_str: str | None) -> int | None:
    """Whole days from an ISO date string to today, or None if unparseable."""
    try:
        then = date.fromisoformat(iso_str)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None
    return (date.today() - then).days


def should_check_today(config: dict) -> bool:
    """True if we haven't already checked today (any outcome counts)."""
    return _uc(config).get("last_checked") != _today_iso()


def record_check_result(config: dict, latest: str | None) -> None:
    """Stamp today's date as last_checked regardless of outcome, so a
    failed/offline check waits until tomorrow rather than retrying every
    launch. Update latest_version only when we actually got a value."""
    uc = _uc(config)
    uc["last_checked"] = _today_iso()
    if latest is not None:
        uc["latest_version"] = latest


def pending_update(config: dict, current_version: str) -> str | None:
    """The known-newer version, or None. Reads only stored state — no
    network — so it's cheap to call on every menu build."""
    latest = _uc(config).get("latest_version")
    if latest and is_newer(latest, current_version):
        return latest
    return None


def is_dismissed(config: dict, version: str) -> bool:
    return _uc(config).get("dismissed_version") == version


def dismiss(config: dict, version: str) -> None:
    """Mark a version as already shown (Skip or Update) so we don't re-nag
    about it. The passive menu item stays — dismissal only silences the
    popup for this version."""
    _uc(config)["dismissed_version"] = version


def should_show_popup(config: dict, latest: str) -> bool:
    """Whether the interruptive popup may fire for `latest` right now.

    False if this version was already dismissed, or if the popup fired
    fewer than POPUP_MIN_INTERVAL_DAYS ago (the throttle, independent of
    the daily check). True otherwise.
    """
    uc = _uc(config)
    if latest == uc.get("dismissed_version"):
        return False
    last = uc.get("last_popup_shown")
    if last:
        days = _days_since(last)
        if days is not None and days < POPUP_MIN_INTERVAL_DAYS:
            return False
    return True


def record_popup_shown(config: dict) -> None:
    _uc(config)["last_popup_shown"] = _today_iso()
