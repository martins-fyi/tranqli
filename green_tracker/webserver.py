"""
webserver.py — Local Flask CSV editor for sessions (brief §9, §10).

A tiny on-demand server that hosts an editable table view of the sessions
CSV. Started lazily when the user picks "Edit data (web)…" from the menu;
the editor opens in the default browser at the configured loopback port.

The server runs on a daemon thread inside the desktop app's process — no
separate executable, no system service. Storage is decoupled via callbacks
so this module doesn't import storage.py directly; main.py wires the two.

Wiring (in main.py's TrackerApp.__init__):

    self.csv_editor = CsvEditorServer(
        read_rows=self._read_rows_for_web,
        write_rows=self._write_rows_for_web,
        rename_tag=self._rename_tag_for_web,
        undo=self._undo_for_web,        # POST /api/undo
        can_undo=storage.can_undo,      # GET /api/undo_state
    )
    # bound for the menu callback:
    open_csv_editor = self.csv_editor.open_in_browser

The callbacks are main.py methods, not storage functions passed straight
through. They wrap storage and then repair app state the web edit
invalidates — _write_rows_for_web calls storage.save_sessions and then
re-seeds the widget's carry, since editing today's active-tag row
otherwise leaves the on-screen total stale; _rename_tag_for_web
additionally re-points the live tracker if the tag being renamed is the
one currently running. Going straight to storage would skip that.

The browser POSTs the full rows array back on Save; the server validates
each row and hands the whole list to write_rows, which crash-safely
overwrites the CSV (brief §10 — write-temp-then-os.replace) and records
an undo snapshot (spec §5). Both happen inside storage.save_sessions, so
this module gets them without knowing they exist.

This server's request handlers run on the Flask daemon thread while the
widget mutates from Qt's main thread. Storage serialises those writers
internally; nothing here needs to lock.
"""

from __future__ import annotations

import logging
import re
import threading
import webbrowser
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

# Flask is imported lazily inside `_build_app()` rather than at module
# load. The rationale is purely startup-time: importing Flask pulls in
# Werkzeug, Jinja2, MarkupSafe, click, itsdangerous, and blinker — on
# a frozen PyInstaller bundle this can cost 100-500 ms of cold-start
# even when the user never opens the CSV editor in this session.
# `CsvEditorServer` is already lazily-started; moving the imports
# matches that intent.
#
# `from __future__ import annotations` (above) keeps the `Optional[Flask]`
# type hint on `_app` working as a stringified annotation — no
# runtime resolution needed, so the type-only reference doesn't
# defeat the deferral.
if TYPE_CHECKING:
    from flask import Flask  # noqa: F401 — type-only import for annotations


# Bound to a private port that doesn't collide with the usual development
# defaults (8000 / 8080 / 5000 / 3000). Loopback-only — never exposed
# externally.
HOST = "127.0.0.1"
PORT = 49377


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---- Embedded editor page -------------------------------------------------
# Self-contained HTML + CSS + JS. The page fetches /api/rows on load,
# renders an editable table, and POSTs the full list back to /api/rows
# on Save. Inputs are built via createElement (not innerHTML) so any CSV
# value renders literally, no escape concerns.

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Green Tracker — Sessions</title>
<style>
  :root {
    --bg: #f7f7f4;
    --card-bg: #ffffff;
    --text: #2a2a2a;
    --text-muted: #6a6a6a;
    --border: #e0ddd0;
    --accent: #0C3420;
    --accent-hover: #0a2818;
    --danger: #8a2020;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 2rem 1rem;
  }
  .container { max-width: 900px; margin: 0 auto; }
  h1 {
    font-size: 1.4rem;
    margin: 0 0 1.5rem 0;
    color: var(--accent);
    font-weight: 500;
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    font-size: 0.95rem;
  }
  th, td {
    padding: 0.4rem 0.5rem;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }
  th {
    background: #efece4;
    font-weight: 500;
    font-size: 0.75rem;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  tbody tr:last-child td { border-bottom: none; }
  input {
    width: 100%;
    padding: 0.3rem 0.4rem;
    border: 1px solid transparent;
    background: transparent;
    font-family: inherit;
    font-size: inherit;
    color: inherit;
    border-radius: 3px;
    box-sizing: border-box;
  }
  input:focus {
    border-color: var(--accent);
    background: var(--card-bg);
    outline: none;
  }
  input[type="number"] { text-align: right; }
  .actions {
    margin-top: 1rem;
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }
  button {
    background: var(--card-bg);
    border: 1px solid var(--border);
    padding: 0.4rem 0.9rem;
    border-radius: 4px;
    font-family: inherit;
    font-size: 0.9rem;
    cursor: pointer;
    color: inherit;
    transition: background 100ms;
  }
  button:hover { background: #efece4; }
  button.primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  button.primary:hover {
    background: var(--accent-hover);
    border-color: var(--accent-hover);
  }
  button.delete {
    color: var(--danger);
    border: none;
    background: transparent;
    padding: 0.15rem 0.45rem;
    font-size: 1.1rem;
    line-height: 1;
  }
  button.delete:hover { background: #f0e0e0; }
  button.icon-btn {
    padding: 0.3rem 0.5rem;
    display: inline-flex;
    align-items: center;
  }
  button.icon-btn img { width: 18px; height: 18px; display: block; }
  /* Disabled Undo: greyed and non-interactive, clearly distinct from the
     enabled state (empty stack → nothing to undo). */
  button.icon-btn:disabled { cursor: default; opacity: 0.35; }
  button.icon-btn:disabled:hover { background: var(--card-bg); }
  /* Tag filter bar — view-only; hidden rows still save. */
  .tag-filter {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-bottom: 1.25rem;
  }
  .tag-filter:empty { display: none; }
  .filter-chip {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-left-width: 4px;                 /* accent stripe = tag colour */
    border-radius: 4px;
    padding: 0.3rem 0.7rem;
    font-size: 0.85rem;
    cursor: pointer;
    transition: background 100ms, border-color 100ms;
  }
  .filter-chip:hover { background: #efece4; }
  .filter-chip.active {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
  #status {
    margin-left: auto;
    color: var(--text-muted);
    font-size: 0.85rem;
    min-width: 120px;
    text-align: right;
  }
  #status.error { color: var(--danger); }
  #status.success { color: var(--accent); }
  .empty {
    padding: 2rem;
    text-align: center;
    color: var(--text-muted);
  }
  .section-label {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-muted);
    font-weight: 500;
    margin: 1.25rem 0 0.5rem 0;
  }
  .section-label:first-child { margin-top: 0; }
  .tag-totals {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-bottom: 1.25rem;
  }
  .tag-totals:empty { display: none; }
  .tag-chip {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.3rem 0.6rem;
    font-size: 0.85rem;
    display: inline-flex;
    align-items: baseline;
    gap: 0.5rem;
    cursor: pointer;
    transition: background 100ms, border-color 100ms;
  }
  .tag-chip:hover {
    background: #efece4;
    border-color: #c8c4b8;
  }
  .tag-chip[title] { cursor: pointer; }
  .tag-chip-label {
    font-weight: 500;
    color: var(--text);
  }
  .tag-chip-total {
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }
</style>
</head>
<body>
<div class="container">
  <h1>Sessions</h1>
  <div class="section-label">Tag totals</div>
  <div id="tag-totals" class="tag-totals"></div>
  <div class="section-label">Filter</div>
  <div id="tag-filter" class="tag-filter"></div>
  <table>
    <thead>
      <tr>
        <th>Date</th>
        <th>Tag</th>
        <th>Session name</th>
        <th style="width: 140px;">Duration</th>
        <th style="width: 36px;"></th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="actions">
    <button onclick="addRow()">+ Add row</button>
    <button class="primary" onclick="saveRows()">Save</button>
    <button id="undo-btn" class="icon-btn" onclick="undo()"
            title="Undo" aria-label="Undo" disabled>
      <img alt="Undo" src="data:image/svg+xml;utf8,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20fill='none'%20stroke='%233a3a3a'%20stroke-width='2'%20stroke-linecap='round'%20stroke-linejoin='round'%3E%3Cpath%20d='M9%2014L4%209l5-5'/%3E%3Cpath%20d='M4%209h11a5%205%200%200%201%205%205v0a5%205%200%200%201-5%205H9'/%3E%3C/svg%3E">
    </button>
    <span id="status"></span>
  </div>
</div>
<script>
const KEYS = ['date', 'tag', 'session_name', 'minutes'];

// View-only tag filter. `activeFilter` null = show all; otherwise the
// tag whose rows are shown. Filtering only toggles row visibility — every
// row stays in the DOM, so collectRows() (and therefore save and undo)
// sees the full set regardless of what's filtered. `tagColors` mirrors the
// archive's per-tag colour, fetched best-effort to tint the filter chips.
let activeFilter = null;
let tagColors = {};

// Format an integer minutes value as "Xd Xh Xm" with cascading
// omission of leading zero fields. Mirrors _format_dhm in main.py
// so the archive and the web editor present durations identically.
//   45    -> "45m"
//   90    -> "01h 30m"
//   1500  -> "01d 01h 00m"
function formatDhm(minutes) {
  const m = Math.max(0, parseInt(minutes, 10) || 0);
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  const mins = m % 60;
  const pad = (n) => String(n).padStart(2, '0');
  if (d > 0) return `${pad(d)}d ${pad(h)}h ${pad(mins)}m`;
  if (h > 0) return `${pad(h)}h ${pad(mins)}m`;
  return `${pad(mins)}m`;
}

// Parse a duration string back to integer minutes. Flexible input:
//   "01d 02h 30m"  -> 1590    (canonical suffix form)
//   "1d 2h 30m"    -> 1590    (unpadded)
//   "2h 30m"       -> 150
//   "30m"          -> 30
//   "01:02:30"     -> 1590    (legacy colon form, still accepted)
//   "2:30"         -> 150     (HH:MM)
//   "90"           -> 90      (plain integer = minutes)
//   ""             -> 0
// Suffix detection runs first; if any of d/h/m suffixes are present,
// missing fields default to zero (so "1d 30m" → 1470, no hours). Colon
// fallback applies only when no suffixes are found.
function parseDhm(s) {
  const t = String(s == null ? '' : s).trim();
  if (!t) return 0;
  if (/^\d+$/.test(t)) return parseInt(t, 10);
  const dMatch = t.match(/(\d+)\s*d/i);
  const hMatch = t.match(/(\d+)\s*h/i);
  const mMatch = t.match(/(\d+)\s*m/i);
  if (dMatch || hMatch || mMatch) {
    const d = dMatch ? parseInt(dMatch[1], 10) : 0;
    const h = hMatch ? parseInt(hMatch[1], 10) : 0;
    const m = mMatch ? parseInt(mMatch[1], 10) : 0;
    return d * 1440 + h * 60 + m;
  }
  const parts = t.split(':').map(p => parseInt(p, 10));
  if (parts.some(isNaN)) return 0;
  if (parts.length === 3) return parts[0] * 1440 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0];
}

async function loadRows() {
  try {
    await loadTagColors();
    const res = await fetch('/api/rows');
    const rows = await res.json();
    renderRows(rows);
  } catch (e) {
    setStatus('Failed to load', 'error');
  }
  updateUndoState();
}

// Sync the Undo button's disabled state with the shared, in-process undo
// stack (which the desktop app also feeds), by asking the server — the
// page can't see the Python stack itself. Called on load and after every
// mutation, so the button greys the moment there's nothing to undo.
async function updateUndoState() {
  const btn = document.getElementById('undo-btn');
  try {
    const res = await fetch('/api/undo_state');
    const data = await res.json();
    btn.disabled = !data.can_undo;
  } catch (e) {
    btn.disabled = true;
  }
}

async function undo() {
  setStatus('Undoing…', '');
  try {
    const res = await fetch('/api/undo', { method: 'POST' });
    if (res.ok) {
      const data = await res.json();
      await loadRows();   // reflect the restored CSV; also refreshes state
      setStatus(data.undone ? 'Undone' : 'Nothing to undo', 'success');
      setTimeout(() => setStatus('', ''), 2000);
    } else {
      const err = await res.json().catch(() => ({}));
      setStatus('Error: ' + (err.error || 'undo failed'), 'error');
    }
  } catch (e) {
    setStatus('Network error', 'error');
  }
}

function renderRows(rows) {
  const tbody = document.getElementById('rows');
  tbody.innerHTML = '';
  if (rows.length === 0) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 5;
    td.className = 'empty';
    td.textContent = 'No sessions yet. Add a row to start.';
    tr.appendChild(td);
    tbody.appendChild(tr);
    renderTagTotals([]);
    activeFilter = null;
    renderFilterBar([]);
    return;
  }
  rows.forEach((row, i) => {
    const tr = document.createElement('tr');
    KEYS.forEach(key => {
      const td = document.createElement('td');
      const input = document.createElement('input');
      if (key === 'minutes') {
        // Show as "Xd Xh Xm" with leading zero fields omitted. Text
        // input (not number) so spaces and letters work. parseDhm()
        // accepts this format, the legacy colon form, or raw minutes.
        input.placeholder = 'e.g. 01h 30m';
        input.value = formatDhm(row[key]);
      } else {
        input.value = row[key] != null ? row[key] : '';
      }
      input.dataset.key = key;
      input.dataset.idx = i;
      // Live-update tag totals as the user edits. Tag changes
      // re-partition the totals; minutes changes re-sum them.
      // Cheap enough to run on every keystroke for a small table.
      input.addEventListener('input', () => renderTagTotals(collectRows()));
      td.appendChild(input);
      tr.appendChild(td);
    });
    const td = document.createElement('td');
    const btn = document.createElement('button');
    btn.textContent = '\u00d7';
    btn.className = 'delete';
    btn.title = 'Delete row';
    btn.onclick = () => deleteRow(i);
    td.appendChild(btn);
    tr.appendChild(td);
    tbody.appendChild(tr);
  });
  renderTagTotals(rows);
  // If the active filter's tag no longer exists (renamed/all deleted),
  // fall back to All so the table isn't left mysteriously empty.
  if (activeFilter !== null && !distinctTags(rows).includes(activeFilter)) {
    activeFilter = null;
  }
  renderFilterBar(rows);
  applyFilterVisibility();
}

// Sum minutes per tag across the current rows and render them as
// chips above the table, sorted by total descending so the most-
// worked tags lead. Called from renderRows on every re-render and
// on every input keystroke (via the listener wired in renderRows).
// Falsy tags (empty string after trim) are skipped — a row being
// added with no tag yet shouldn't appear as a phantom chip.
function renderTagTotals(rows) {
  const totals = {};
  rows.forEach(r => {
    const tag = String(r.tag == null ? '' : r.tag).trim();
    if (!tag) return;
    const mins = parseInt(r.minutes, 10);
    if (isNaN(mins) || mins <= 0) return;
    totals[tag] = (totals[tag] || 0) + mins;
  });
  const container = document.getElementById('tag-totals');
  container.innerHTML = '';
  const entries = Object.entries(totals).sort((a, b) => b[1] - a[1]);
  entries.forEach(([tag, mins]) => {
    const chip = document.createElement('div');
    chip.className = 'tag-chip';
    chip.title = 'Click to rename';
    chip.onclick = () => renameTag(tag);
    const tagSpan = document.createElement('span');
    tagSpan.className = 'tag-chip-label';
    tagSpan.textContent = tag;
    const totalSpan = document.createElement('span');
    totalSpan.className = 'tag-chip-total';
    totalSpan.textContent = formatDhm(mins);
    chip.appendChild(tagSpan);
    chip.appendChild(totalSpan);
    container.appendChild(chip);
  });
}

// Fetch the archive's per-tag colours so the filter chips can match them.
// Best-effort: on any failure the chips just render without accent tint.
async function loadTagColors() {
  try {
    const res = await fetch('/api/tag_colors');
    if (res.ok) tagColors = await res.json();
  } catch (e) { /* chips fall back to the default border */ }
}

// Distinct non-empty tags across the current rows, in first-seen order.
function distinctTags(rows) {
  const seen = [];
  rows.forEach(r => {
    const t = String(r.tag == null ? '' : r.tag).trim();
    if (t && !seen.includes(t)) seen.push(t);
  });
  return seen;
}

// Toggle the filter. Clicking "All" (tag == null) or the already-active
// tag clears it; any other tag becomes the active filter.
function setFilter(tag) {
  activeFilter = (tag === null || tag === activeFilter) ? null : tag;
  applyFilterVisibility();
  renderFilterBar(collectRows());
}

// Show only rows whose current tag matches the active filter. Reads each
// row's live tag input, so it respects edits made since the last render.
// Purely visual: rows set to display:none stay in the DOM and still POST.
function applyFilterVisibility() {
  document.querySelectorAll('#rows tr').forEach(tr => {
    const tagInput = tr.querySelector('input[data-key=\"tag\"]');
    if (!tagInput) return;   // the "no sessions" placeholder row
    const tag = tagInput.value.trim();
    tr.style.display = (activeFilter === null || tag === activeFilter) ? '' : 'none';
  });
}

// Build the filter bar: an "All" chip plus one per distinct tag, the
// active one highlighted. Each tag chip carries its archive colour as a
// left accent stripe when available.
function renderFilterBar(rows) {
  const bar = document.getElementById('tag-filter');
  bar.innerHTML = '';
  const tags = distinctTags(rows);
  if (tags.length === 0) return;   // nothing to filter; :empty hides the bar

  const mkChip = (label, tag, color) => {
    const chip = document.createElement('div');
    chip.className = 'filter-chip' + (tag === activeFilter ? ' active' : '');
    chip.textContent = label;
    if (color) chip.style.borderLeftColor = color;
    chip.onclick = () => setFilter(tag);
    return chip;
  };
  // "All" clears the filter (tag null); active when nothing is filtered.
  bar.appendChild(mkChip('All', null, null));
  tags.forEach(tag => bar.appendChild(mkChip(tag, tag, tagColors[tag])));
}

// Prompt for a new name and POST to /api/rename_tag. Reloads from
// the server on success so the table and chips reflect any merged
// (tag, date) collisions handled by the backend. No-ops on cancel,
// empty input, or same-as-old input.
async function renameTag(oldTag) {
  const newTag = prompt(`Rename "${oldTag}" to:`, oldTag);
  if (newTag == null) return;
  const trimmed = newTag.trim();
  if (!trimmed || trimmed === oldTag) return;
  setStatus('Renaming\u2026', '');
  try {
    const res = await fetch('/api/rename_tag', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_tag: oldTag, new_tag: trimmed }),
    });
    if (res.ok) {
      const data = await res.json();
      if (data.affected) {
        setStatus('Renamed', 'success');
        setTimeout(() => setStatus('', ''), 2000);
        // Reload — merges may have collapsed rows, can't infer locally.
        await loadRows();
      } else {
        setStatus('Nothing to rename', '');
        setTimeout(() => setStatus('', ''), 2000);
      }
    } else {
      const err = await res.json().catch(() => ({}));
      setStatus('Error: ' + (err.error || 'unknown'), 'error');
    }
  } catch (e) {
    setStatus('Network error', 'error');
  }
}

function collectRows() {
  const inputs = document.querySelectorAll('#rows input');
  const byIdx = {};
  inputs.forEach(input => {
    const idx = input.dataset.idx;
    if (!byIdx[idx]) byIdx[idx] = {};
    if (input.dataset.key === 'minutes') {
      // Parse DD:HH:MM / HH:MM / raw-int back to minutes for the
      // backend, which still stores minutes as the canonical unit.
      byIdx[idx][input.dataset.key] = parseDhm(input.value);
    } else {
      byIdx[idx][input.dataset.key] = input.value;
    }
  });
  return Object.values(byIdx);
}

function nextTaskName(rows) {
  // Pick the next unused "Task N" so a freshly-added row has a sensible
  // default. Scans existing tags for the same pattern and takes max+1.
  const pattern = /^Task (\d+)$/;
  let max = 0;
  rows.forEach(r => {
    const m = String(r.tag || '').match(pattern);
    if (m) {
      const n = parseInt(m[1], 10);
      if (n > max) max = n;
    }
  });
  return 'Task ' + (max + 1);
}

function addRow() {
  const rows = collectRows();
  const today = new Date().toISOString().slice(0, 10);
  const tag = nextTaskName(rows);
  rows.push({
    date: today,
    tag: tag,
    session_name: tag,
    minutes: 0,
  });
  // A new row's tag won't match an active filter, so clear it — otherwise
  // the row you just added would be added but hidden.
  activeFilter = null;
  renderRows(rows);
}

function deleteRow(idx) {
  const rows = collectRows();
  rows.splice(idx, 1);
  renderRows(rows);
}

function setStatus(text, kind) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = kind || '';
}

async function saveRows() {
  const rows = collectRows();
  setStatus('Saving\u2026', '');
  try {
    const res = await fetch('/api/rows', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(rows),
    });
    if (res.ok) {
      setStatus('Saved', 'success');
      updateUndoState();   // the save just pushed a snapshot
      setTimeout(() => setStatus('', ''), 2000);
    } else {
      const err = await res.json().catch(() => ({}));
      setStatus('Error: ' + (err.error || 'unknown'), 'error');
    }
  } catch (e) {
    setStatus('Network error', 'error');
  }
}

window.addEventListener('load', loadRows);
</script>
</body>
</html>
"""


# ---- Validation -----------------------------------------------------------

def _validate_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce + validate one row dict. Raises ValueError on invalid input.

    Permissive on tag / session_name (any string accepted, trimmed) but
    strict on date (must be YYYY-MM-DD) and minutes (non-negative int)."""
    required = ("date", "tag", "session_name", "minutes")
    for k in required:
        if k not in row:
            raise ValueError(f"missing field: {k}")

    date = str(row["date"]).strip()
    if not _DATE_RE.match(date):
        raise ValueError(f"invalid date {date!r} (expected YYYY-MM-DD)")

    tag = str(row["tag"]).strip()
    session_name = str(row["session_name"]).strip()

    try:
        minutes = int(row["minutes"])
    except (TypeError, ValueError):
        raise ValueError(f"invalid minutes: {row['minutes']!r}")
    if minutes < 0:
        raise ValueError(f"minutes must be >= 0, got {minutes}")

    return {
        "date":         date,
        "tag":          tag,
        "session_name": session_name,
        "minutes":      minutes,
    }


# ---- Server ---------------------------------------------------------------

class CsvEditorServer:
    """Lazily-started Flask server hosting the sessions CSV editor.

    Storage-agnostic — receives read/write callbacks at construction.
    `open_in_browser()` starts the server (idempotent) and opens the editor
    page in the user's default browser."""

    def __init__(self,
                 read_rows:  Callable[[], List[Dict[str, Any]]],
                 write_rows: Callable[[List[Dict[str, Any]]], None],
                 rename_tag: Optional[Callable[[str, str], bool]] = None,
                 undo:       Optional[Callable[[], bool]] = None,
                 can_undo:   Optional[Callable[[], bool]] = None,
                 tag_color:  Optional[Callable[[str], str]] = None,
                 host: str = HOST,
                 port: int = PORT) -> None:
        self._read_rows  = read_rows
        self._write_rows = write_rows
        self._rename_tag = rename_tag
        # undo() pops+restores the last CSV snapshot and returns whether it
        # did; can_undo() reports whether the (shared, in-process) stack is
        # non-empty, driving the button's disabled state. The Flask page
        # can't reach the Python stack directly, hence these callbacks (§5).
        self._undo       = undo
        self._can_undo   = can_undo
        # tag_color(tag) -> "#rrggbb", the archive's per-tag colour. Only
        # used to tint the filter chips (a nice-to-have); optional, so the
        # page renders fine when it's not wired.
        self._tag_color  = tag_color
        self._host       = host
        self._port       = port
        self._app:    Optional[Flask]            = None
        self._thread: Optional[threading.Thread] = None
        self._lock                               = threading.Lock()
        self._started                            = False

    # ---- Public API -----------------------------------------------------

    def open_in_browser(self) -> None:
        """Start the server (idempotent) and launch the editor in the
        default browser."""
        self._ensure_started()
        webbrowser.open(f"http://{self._host}:{self._port}/")

    # ---- Internal -------------------------------------------------------

    def _ensure_started(self) -> None:
        """Idempotent. If the server's already running, return immediately."""
        with self._lock:
            if self._started:
                return
            # Quiet werkzeug — the desktop user doesn't want per-request
            # lines cluttering the console, especially under --windowed.
            logging.getLogger("werkzeug").setLevel(logging.ERROR)
            self._build_app()
            self._thread = threading.Thread(
                target=self._app.run,
                kwargs={
                    "host":         self._host,
                    "port":         self._port,
                    "debug":        False,
                    "use_reloader": False,
                    "threaded":     True,   # concurrent /api/rows hits OK
                },
                daemon=True,  # dies with the main process; no explicit stop
            )
            self._thread.start()
            self._started = True

    def _build_app(self) -> None:
        # Deferred Flask import — see the module-level note. This is
        # the only place Flask is actually instantiated, and it runs
        # at most once per session (gated by `_started` in
        # `_ensure_started`). Cold-import cost lands here on first
        # use, NOT on every app launch.
        from flask import Flask, jsonify, request

        app = Flask(__name__)

        @app.route("/")
        def _index():
            return _HTML

        @app.route("/api/rows", methods=["GET"])
        def _api_get_rows():
            return jsonify(self._read_rows())

        @app.route("/api/rows", methods=["POST"])
        def _api_post_rows():
            try:
                raw = request.get_json(force=True)
                if not isinstance(raw, list):
                    raise ValueError("expected JSON array of rows")
                rows = [_validate_row(r) for r in raw]
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            try:
                self._write_rows(rows)
            except Exception as e:
                return jsonify({"error": f"write failed: {e}"}), 500
            return jsonify({"ok": True})

        @app.route("/api/rename_tag", methods=["POST"])
        def _api_rename_tag():
            """Rename a tag across ALL stored rows.

            Body: {"old_tag": "...", "new_tag": "..."}.
            Returns {affected: bool} on success — false when the rename
            was a no-op (empty / whitespace / same-name / tag absent).
            Distinct from editing the tag column on a single row, which
            still goes through POST /api/rows."""
            if self._rename_tag is None:
                return jsonify({"error": "rename_tag not wired"}), 501
            try:
                raw = request.get_json(force=True)
                if not isinstance(raw, dict):
                    raise ValueError("expected JSON object")
                old_tag = str(raw.get("old_tag", "")).strip()
                new_tag = str(raw.get("new_tag", "")).strip()
                if not old_tag or not new_tag:
                    raise ValueError("both old_tag and new_tag required")
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            try:
                affected = bool(self._rename_tag(old_tag, new_tag))
            except Exception as e:
                return jsonify({"error": f"rename failed: {e}"}), 500
            return jsonify({"ok": True, "affected": affected})

        @app.route("/api/undo_state", methods=["GET"])
        def _api_undo_state():
            """Whether an undo is currently available — drives the Undo
            button's disabled state. Polled on load and after every
            mutation, since the stack is shared with the desktop app."""
            can = bool(self._can_undo()) if self._can_undo is not None else False
            return jsonify({"can_undo": can})

        @app.route("/api/undo", methods=["POST"])
        def _api_undo():
            """Undo the last CSV mutation (§5). Returns whether a snapshot
            was popped, plus the resulting can_undo so the client can set
            the button state without a second round-trip."""
            if self._undo is None:
                return jsonify({"error": "undo not wired"}), 501
            try:
                undone = bool(self._undo())
            except Exception as e:
                return jsonify({"error": f"undo failed: {e}"}), 500
            can = bool(self._can_undo()) if self._can_undo is not None else False
            return jsonify({"ok": True, "undone": undone, "can_undo": can})

        @app.route("/api/tag_colors", methods=["GET"])
        def _api_tag_colors():
            """Per-tag colours for tinting the filter chips (nice-to-have).

            Maps each distinct tag currently in the CSV to its archive
            colour. Returns {} when tag_color isn't wired, so the chips
            just render without an accent stripe."""
            if self._tag_color is None:
                return jsonify({})
            tags = {r.get("tag") for r in self._read_rows()}
            colors = {}
            for tag in tags:
                if tag:
                    try:
                        colors[tag] = self._tag_color(tag)
                    except Exception:
                        pass  # skip a tag that fails rather than 500 the page
            return jsonify(colors)

        self._app = app
