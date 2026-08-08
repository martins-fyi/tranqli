# Tranqli — Build Brief Addendum

Supplements `green-tracker-build-brief.md`. Where the two disagree, this
addendum wins — it records behaviour finalised after the original brief,
most of it the tag-management overhaul. Section numbers here are local to
this document.

---

## 1. Naming

The app was **Green Tracker**, briefly **Traenky**, and is now **Tranqli**.

- User-facing name and product: **Tranqli**.
- Distributable: **`Tranqli.exe`** (PyInstaller `--name Tranqli`).
- Data directory: **`%APPDATA%\Tranqli\`** (`sessions.csv`, `config.json`,
  `active_session.json`, `sessions.csv.bak`). A one-time startup migration
  moves a pre-rename `%APPDATA%\Traenky\` directory across if found.
- The Python package directory stays **`green_tracker/`** on purpose —
  internal package naming has no user-facing impact, and renaming it would
  churn every relative import. Do not rename it.

The original brief still carries a couple of pre-rename spellings in prose;
those are historical and not authoritative.

---

## 2. Colour schemes (widget)

Predates the tag-management work; never written down before. The tracking
widget paints from one of six named schemes, each defining three state
colours (RUNNING / manually PAUSED / idle auto-pause) plus a text base:

| Scheme (key)        | running        | paused          | auto-pause     |
|---------------------|----------------|-----------------|----------------|
| **Earthen** (default) | deep bottle green `#0d492b` | dusky purple `#463d6d` | rust `#8b5a2b` |
| **Twilight**        | royal blue `#2d4595` | saturated plum `#583e75` | bright bronze `#b06530` |
| **Blossom**         | dusty rose `#7a3a55` | slate blue `#3d4a5a` | burnt orange `#a8602d` |
| **Espresso**        | slate teal `#3d4d5a` | coffee `#3a2620` | caramel `#a07a40` |
| **Hearth**          | honey amber `#9a7030` | deep navy-slate `#2d3a4a` | brick red `#8a3535` |
| **Steel**           | neutral charcoal `#2d2d2d` | mid grey `#5d5d5d` | crimson `#7a3535` |

- Selected via right-click → **Colour schemes** (radio group, current one
  checked, each entry previewed by a 3-circle icon).
- Persisted as `config["color_scheme"]` — the lowercase **key**
  (`"earthen"`), not the display name. Default `earthen`.
- `set_scheme()` falls back to Earthen for an unknown key.

This global scheme is the fallback for the per-tag schemes in §7.

---

## 3. Widget always-on-top

The tracking **widget** is frameless, translucent, and carries
`Qt.WindowStaysOnTopHint`; on Windows the app re-asserts HWND_TOPMOST on
every foreground-window change, because some apps (Photos, Snipping Tool,
installers) push themselves above always-on-top windows and don't restore
z-order on close.

This is **widget-only**. The Archive and other dialogs are deliberately
*not* topmost — they must be normal, coverable windows. (The Archive is
created parentless precisely so it doesn't inherit the widget's topmost
z-order; see §8.)

---

## 4. Config schema (config.json), version 3

`CURRENT_CONFIG_VERSION = 3`. Migrations run on load and persist. Beyond
the earlier keys (`widget_size`, `widget_pos`, `color_scheme`, `last_tag`,
`archive_display_mode`, `archive_hours_per_day`, `tag_color_overrides`),
v3 adds:

- **`recent_tags`** — MRU list, most-recent first. Retained up to
  `RECENT_TAGS_MAX = 20`; pickers show `RECENT_TAGS_SHOWN = 5`. Seeded on
  first v3 load from existing `sessions.csv` (distinct tags by most-recent
  date) so an upgrading user isn't treated as brand-new. `last_tag` is kept
  as a mirror of `recent_tags[0]`.
- **`tag_schemes`** — per-tag colour-scheme overrides, `{tag: schemeKey}`
  (§7). Empty by default.

Migration steps cascade (v1→v2 size remap still applies to a v1 config on
its way to v3). A fresh install writes a config already stamped at the
current version, so its `widget_size` is never re-remapped by mistake.

CSV schema is unchanged: `date, tag, session_name, minutes`, one row per
`(tag, date)`. Tags are implicit — defined solely by the strings in the
`tag` column; there is no separate tag registry.

---

## 5. Right-click menu: the Tags ▸ submenu

All tag actions live under a single **Tags ▸** entry, in this order:

```
Tags ▸
    Current: <name>          (display-only, disabled; the active tag)
    ─────────────
    New Tag…                 free-text entry → creates and switches to it
    Switch Between Tags ▸    5 most-recent tags (MRU), current one checked
    Retag session ▸          existing tags; gated on an active session
    Tag Edit ▸               per-tag: Rename… / Delete… / Merge… / Add record / Open Archive
    ─────────────
    More…                    opens the Archive
```

- **Switch Between Tags** always enabled (switching with nothing running is
  just picking what's next; `New Tag…` must be reachable with no tags).
- **Retag session** needs a live session to retag.
- **Tag Edit** disabled when no tags exist.
- The old flat top-level `Switch task` / `Retag session` / `Tags edit`
  entries were consolidated here.

The **Undo** item sits in the root menu just above *Minimize to tray*
(`… → Undo → Minimize to tray → Quit`), on its own separator (§6).

---

## 6. Undo system

- **Global, in-process, 8-deep** (`UNDO_STACK_DEPTH = 8`), LIFO. A snapshot
  is the whole `sessions.csv` as bytes, taken before each mutating write.
  In-memory only — not persisted across restarts (deliberately; avoids a
  second crash-safety surface).
- Captures mutations from **any** surface: widget save, tag-switch
  auto-save, Archive edits, web-editor saves, and tag rename/delete/merge.
  Any surface can undo the most recent one.
- Restores via the same **write-temp-then-`os.replace()`** crash-safe path
  as a normal save. That atomic path was added for `sessions.csv` as part
  of this work — brief §10 had specified it but `save_sessions()` had been
  truncating in place. All CSV mutation now also serialises through one
  reentrant lock spanning the full read-modify-write, closing a
  widget-vs-web load-modify-save race.
- Undo restores stored history only; it never rewinds the running tracker.
- **Three UI surfaces**, all calling the same `storage.undo()`:
  1. **Menu** — "Undo" item, greyed when the stack is empty.
  2. **Archive** — circular-arrow button in the bottom bar, greyed when
     empty, re-synced on every archive rebuild.
  3. **Web editor** — circular-arrow button (`alt="Undo"`), driven by
     `POST /api/undo` and `GET /api/undo_state` since the browser can't
     touch the in-process stack.

---

## 7. Tag switching, retagging, and per-tag colour schemes

Two distinct operations, deliberately different, now clearly labelled:

- **Switch Between Tags** (`on_switch_tag`) — *commit-then-rebind*. Banks
  the current tag's unsaved time first via the normal Save path (rounding,
  midnight split, `(tag, date)` merge), then binds the picked tag at
  **00:00, PAUSED** — never auto-resumes, so a mis-click can't silently
  record against the wrong tag. Pushes the tag to the front of the MRU.
  Picking the already-active tag is a no-op.
- **Retag session** (`on_set_tag`) — *re-attribution*. Rebinds the current
  session's tag and **carries its accumulated time across** (for when you
  started on the wrong tag). Commits nothing; the time moves.

**Per-tag colour schemes** (§2c step 4): binding a tag looks up
`tag_schemes[tag]` and applies that scheme via the widget's existing
`set_scheme()`; with no entry it falls back to the global
`config["color_scheme"]`. Applied on every bind path — the launch gate,
Switch Between Tags, New Tag, Retag, and auto-resume. `tag_schemes` stores
scheme **keys** (`"earthen"`), not display names. Binding never *writes*
`tag_schemes` — it reflects a choice, it doesn't silently pin one.

**Fresh-launch gate (§2a):** before the first left-click of a session can
start tracking, a picker offers the 5 most-recent tags plus **New tag…**.
With no history at all it goes straight to text entry. It's once per
session, not per click — after a tag is chosen, left-click is plain
start/pause/resume. New-tag creation is available at this gate too, not
only once something is already running.

---

## 8. Tag management: rename / delete / merge

Under **Tag Edit ▸**, per tag (the tag is already chosen, so none of these
opens a "which tag?" picker):

- **Rename** — bulk find/replace across the `tag` column; colliding
  `(tag, date)` rows merge (minutes summed). The MRU and `tag_schemes`
  follow the rename, keeping the tag's MRU position (a rename is not a use).
- **Delete** — removes every row for the tag. If it's the live unsaved
  session, warns that the in-progress time dies with it and cannot be
  recovered by Undo (Save-first is *not* offered here — banking would just
  write a row the delete then removes). Resets the tracker if it was on the
  deleted tag, so it isn't recreated on the next save.
- **Merge** — `merge_tags(target, absorbed)`; the absorbed tag's rows move
  to the target, colliding dates sum, the absorbed tag disappears from CSV,
  MRU, and `tag_schemes`. If the absorbed tag has a live unsaved session,
  **Save-first is offered** (it genuinely preserves the time, which then
  merges into the target).

All three push an undo snapshot; no-op cases (unknown/blank tag) touch
nothing and burn no undo slot.

---

## 9. Archive window (tabs + per-tag colouring)

- **Tabbed**: an **All** tab (the classic newest-first Recent + Year→Month
  tree + Tags overview) plus **one tab per tag**, ordered by recency of
  last activity. Each per-tag tab shows that tag's Recent + Year→Month
  list with its lifetime total in the tab label. Per-month **Total** rows
  are kept; per-tag rows carry the tag's background colour just like the
  All tab.
- **One colour resolver.** Every surface that paints a tag —
  All-tab chips, section and Total rows, the Tags overview, per-tag tab
  rows and Totals — calls a single `_tag_color(tag)`: a
  `tag_color_overrides` entry wins, else a slot in the 16-hue archive
  palette indexed by one global recency order (overridden tags don't
  consume a slot). This replaced several independent palette-index
  computations that could show one tag in two colours. **Not** the widget
  colour schemes of §2/§7 — the Archive palette and the widget scheme are
  separate systems.
- **No in-progress marker.** The Archive used to paint the live session's
  row rust. Removed: with a tag auto-resumed on launch, its live row read
  rust while its Tags-overview summary read the tag colour — one tag,
  two colours, indistinguishable from a bug. The widget already shows
  running/paused, and the Archive is for reviewing sessions, so it now
  paints one colour per tag with no exceptions.
- Tab labels are plain default text (accent lives in the row colours).
  Overflowing tab strips get scroll arrows plus a corner search box that
  filters tabs without changing the selection.
- **Normal window**: created parentless so it doesn't inherit the widget's
  topmost z-order (§3); it's coverable like any window.

---

## 10. Web editor (Edit data)

Local Flask page (`Edit data (web)…`), same in-process undo stack.

- **Branding**: tab title *Tranqli — Sessions*, heading **Tranqli
  Sessions**, and a footer linking **github.com/martins-fyi/tranqli** —
  the app's de-facto About (there is no About screen on the desktop side).
- **Tag filter**: a chip bar above the table — **All** plus one chip per
  distinct tag, each tinted with its archive `_tag_color`. Clicking a tag
  shows only its rows; **All** or re-clicking the active tag clears it.
  **View-only** — every row stays in the DOM, so save (`POST /api/rows`),
  edits, and undo always act on the full set regardless of the filter. A
  new row clears the filter (so it isn't added-but-hidden); if the active
  tag vanishes the filter falls back to All. Filter state is in-memory and
  resets on reload.
- Endpoints: `GET/POST /api/rows`, `POST /api/rename_tag`,
  `GET /api/undo_state`, `POST /api/undo`, `GET /api/tag_colors`.

## 11. Update checking

- **Config schema v4** (`CURRENT_CONFIG_VERSION = 4`): adds an `update_check`
  block — `last_checked`, `latest_version`, `dismissed_version`,
  `last_popup_shown`, all `None` by default. Migrated from v3 the same way
  `recent_tags`/`tag_schemes` were added for v3.
- **Check mechanics** (`green_tracker/updater.py`): a threaded
  (`QThread`) GET against the GitHub releases API
  (`api.github.com/repos/martins-fyi/tranqli/releases/latest`), never the
  human-facing releases page. This endpoint only returns published,
  non-draft, non-prerelease releases — so tagging WIP builds as
  **pre-release** on GitHub keeps them invisible to the check with zero
  code involved. Fails silently on any network/parse error; retried the
  next calendar day, never retried same-day.
- **Two independent throttles**, deliberately decoupled:
  - The *check itself* runs at most once per calendar day
    (`should_check_today`), scheduled via `QTimer.singleShot` ~1.5 s after
    the widget first shows — never blocking first paint or adding to the
    startup-time TODO.
  - The *popup* is further throttled to at most once every
    `POPUP_MIN_INTERVAL_DAYS` (3) days, and never twice for the same
    version once dismissed (`dismissed_version`). This means multiple
    releases landing close together can't spam the popup, even if the
    daily check itself succeeds every time.
- **The menu item is uncapped.** "Update Available (vX.Y.Z)" sits at the
  top of the shared right-click menu (widget + tray, §7) whenever a newer
  version is known, computed fresh on every menu build — it reflects
  ground truth regardless of whether the popup already fired or was
  throttled.
- **Popup dialog**: parentless, non-topmost (same convention as Archive
  §9 and About — coverable, not pinned above other windows). Skip and
  Update both call `dismiss()`; Update additionally opens the releases
  page in the default browser. Neither action touches the stored session
  data — this is purely a notice, no auto-download or install logic.
- Not a full auto-updater by design: no install machinery, no dependency
  on code signing (still open in the backlog). Shipped in **v0.2.1**.

---

## 12. Web-editor port, tab-bar delete, live readout and totals, undo glyph

Five changes landing after §11, in four areas — §12c covers two. Amends
§6 (undo surfaces), §8 (delete), §9 (Archive), §10 (web editor).

### 12a. Web editor port: 49377 → 8377

**Symptom**: `Edit data (web)…` opened a browser tab showing
`ERR_CONNECTION_REFUSED`. No code near the web editor had changed.

**Cause**: 49377 sits in the IANA **ephemeral** range (49152–65535).
On Windows that range is carved into reserved *exclusion ranges* by
Hyper-V / WSL / Docker, assigned **dynamically at boot**. A port inside
one fails to bind with `WinError 10013` ("access forbidden"). So a port
that worked for months can start refusing after an unrelated reboot,
with no code change and nothing in the app's history to blame. Confirm
with `netsh interface ipv4 show excludedportrange protocol=tcp`; a bare
`bind()` to the port reproduces it in isolation.

**Do not** treat a future recurrence as a regression in the editor. Check
the exclusion ranges first.

The default is now **8377** — registered range, IANA-unassigned, below
49152 and so outside anything Windows reserves dynamically.

Two structural fixes alongside the move, since the port alone only
makes the failure rarer, not visible:

- **Synchronous bind.** Startup went from `app.run()`-in-a-thread to
  `werkzeug.serving.make_server()` on the calling thread, with only
  `serve_forever()` handed to the daemon thread. `make_server()` has
  bound and is listening by the time it returns, so `_ensure_started()`
  returning *is* the readiness signal — the old race between the bind
  and `webbrowser.open()` is gone structurally, with no poll or sleep.
- **Failures surface.** Binding inside the thread meant the `OSError`
  died with the thread while `_started` was set `True` regardless — so
  the editor silently no-opped for the rest of the session and the user
  just saw a dead tab. The bind now raises to the caller, `_started` is
  only set on success (a later attempt can retry), and `main.py` shows a
  warning dialog instead of opening a tab that cannot load.
- **Fallback.** If the preferred port is unavailable, the server rebinds
  on port 0 (OS-assigned) and `open_in_browser()` reads back
  `server.server_port`, so the URL always follows the actual bind. A
  collision is never fatal.

### 12b. "Delete tag" from an Archive tab (amends §8, §9)

Right-clicking a **per-tag** Archive tab offers a single **Delete tag**
entry. The "All" tab has none — it is the fallback view, not a tag.

**No new delete semantics.** The entry calls the same `on_delete_tag`
handler as Tag Edit ▸ Delete (§8), so the live-unsaved-session warning,
both confirmation dialogs, the config cleanup and the tracker reset come
along unchanged. The only addition is a tab-set rebuild afterwards, so a
deleted tag's tab disappears. **No separate confirmation was added** —
`on_delete_tag` already confirms, and a second prompt would double-ask
and duplicate copy.

The menu is bound to the tab **bar**, not the tab widget, so it never
answers a right-click inside a tree (which has its own row menu).

The action is wired through `QAction.triggered`, **not** by comparing
`QMenu.exec()`'s return value. `exec()` only reports an action when the
menu was dismissed by a mouse click, so the original wiring silently did
nothing on keyboard selection.

Tabs now carry their tag in `tabData`, not parsed back out of the label
— the label also holds the lifetime total and the live readout below.
The tab search box matches `tabData` too, so a query like "30" no longer
surfaces tags by their minute counts.

### 12c. In-progress readout and lifetime total on a tag's own tab (amends §9)

Two per-tag-tab surfaces, sharing one live-session condition and one
timer — documented together because that shared plumbing is the point.

**The in-progress readout.** When the widget's session (RUNNING or PAUSED
with time on the clock) is bound to a tag, **that tag's own tab** appends
a live elapsed readout:

```
work   00d 01h   ● 01:23
```

Never on "All", which has no single tag to be in progress. This is *not*
a return of the rust in-progress row marker removed in §9 — that painted
one tag in two colours; this is a text suffix on one tab label and
touches no colour.

Condition for showing it is `_tag_has_live_unsaved_session` — the same
predicate delete uses (§8), so "in progress" and "delete would destroy
live time" can never disagree.

**Refresh is scoped, not background.** A `QTimer` at
`ARCHIVE_LIVE_REFRESH_MS` (1000 ms, matching the widget's hover-scoped
`REFRESH_MS`) is parented to the Archive dialog and runs only while
**both** hold: the Archive is open **and** some tag owns a live session.
It stops the moment either lapses — session banked, tracker reset, tag
deleted, or window closed — so a closed Archive costs nothing. The
readout is minute-resolution, so most ticks are a no-op string compare;
labels are written back only when actually changed, to avoid churning
tab-bar layout. Every tick recomposes *all* per-tag labels, so the
readout is removed when a session ends, not only added when one starts.

**The lifetime-total header.** The same per-tag tab also carries a header
line in its content area, above the first year/month group:

```
Lifetime total: 02d 02h
```

Never on "All" — same scoping as the readout, and for the same reason: a
tab spanning every tag has no single lifetime to total.

The value comes from `_archive_format_tag_total()` — days+hours only
(brief §4), but honouring the archive's Hours / Workdays day divisor. So
it agrees with the tab label (same function) and, up to the minutes they
truncate, with the Tags overview and the right-click Tags submenu, both
of which apply that same divisor via `_archive_format_duration`. All four
archive totals read the same in both modes.

**An earlier version of this got the shared source wrong** — recorded
here so it isn't reintroduced. The header first used
`storage.format_tag_total()`, on the stated guarantee that it matched
"the Tags submenu totals." It did not: `format_tag_total` hardcodes a
24 h day, whereas the submenu (`_get_tag_lifetimes` →
`_archive_format_duration`) honours the Workdays toggle. The guarantee
had been checked against the *tab label* — which used the same 24 h
function — not the submenu it actually named. Hours mode hid the gap (the
two divisors coincide there); Workdays mode exposed it, with 8,109 stored
minutes reading "05d 15h" on the header and label but "16d 07h" in the
overview and submenu — the same time under two day-length conventions,
not a data bug. The fix is one divisor-aware formatter shared by all
four. Same lesson as §12a: a "same source, can't disagree" claim is only
as strong as the source you actually verified against.

When `_tag_has_live_unsaved_session` holds — the same condition as the
readout — the tracker's unbanked elapsed time is added in. That elapsed
is time not yet written to CSV, so adding it counts the in-progress
session once, not twice.

The header is re-texted inside `_tick_archive_live_tab`, riding the timer
above rather than owning one: both draw on the same live elapsed time, so
a second timer would be redundant and could disagree mid-tick. The label
is located by `objectName` (`_ARCHIVE_TAG_TOTAL_NAME`) rather than cached
on the instance, because the tab set is rebuilt wholesale on every
mutation and a stored widget reference would dangle. Non-live changes — a
save, delete, or merge — reach the header through that rebuild, so they
land immediately rather than waiting out a tick; this matters because
with no live session the timer is not running at all.

**The header deliberately does not move minute-by-minute.**
`_archive_format_tag_total` is days-plus-hours resolution and truncates,
so a live session only shifts the header on the hour. That is a choice,
not an oversight: the `● HH:MM` readout above already gives minute
resolution for the in-progress time, and mirroring it in the header would
put two live clocks on one tab saying the same thing in two formats —
while also breaking the header's agreement with the Tags overview and
submenu, which is the reason it shares their day divisor in the first
place. Do not "fix" this into a second minute-resolution display.

### 12d. Undo icon → ↩ (amends §6)

Both undo surfaces from §6 now use **↩ (U+21A9, LEFTWARDS ARROW WITH
HOOK)** in place of the circular arrow. Visual swap only — no change to
undo behaviour, placement, sizing, or the greyed-when-empty state.

- **Archive**: painted to a `QIcon`, keeping the 16 px box and `#3A3A3A`.
- **Web editor**: `<span class="undo-glyph">&#x21A9;</span>` replacing the
  inline-SVG `<img>`, at the same 18 px / `#3a3a3a`, so `:disabled`
  opacity and the button's metrics are unchanged.

**Sizing must not come from font metrics.** `QFontMetrics.tightBoundingRect`
over-reports ↩'s height by ~50 % in Segoe UI (40 px reported vs 26 px of
actual ink), and the error differs per fallback font — centring off it
sits the glyph visibly high, and a naive "fill the box" multiplier
overflows the pixmap and clips (at 64 px, the visible ink was a single
edge column). The icon is therefore rendered oversized to a scratch
pixmap, its **actual ink bounds measured from the alpha channel**
(`QRegion(pixmap.mask()).boundingRect()`), then scaled to 72 % of the box
— the span of the arc it replaced — and centred on those bounds. This is
font-independent.

Appearance is **not** covered by tests: the offscreen Qt platform used in
CI substitutes a stub font, so rendered ink there says nothing about the
real glyph. Tests assert the codepoint and the HTML entity; the visual
check is manual.

---

## 13. Retime session

Lets you directly rewrite a session's recorded duration. Two entry
points with genuinely different implementations, not two views of the
same code — documented separately below.

### 13a. What it is

- **Active/in-progress session** — "Retime session" on the widget's
  right-click menu and the tray icon menu (both route through the same
  `on_retime_session` handler). Acts on today's currently-tracked
  session for whichever tag is active.
- **Any saved past session** — "Retime" on an Archive row's right-click
  menu (`_archive_retime`). Acts on that specific stored row.
- Since v0.2.4 these are **not** the same dialog, and not the same
  duration format either — the divergence is deliberate (§13d):
  - the Archive one is a plain retype-the-value prompt in the app's
    usual `Dd Hh Mm`, and is the escape hatch for multi-day
    corrections alongside the web editor;
  - the active-session one takes `Hh Mm` only — no days, because it
    edits one (tag, date) row and a row cannot span days — and adds an
    Add row, quick-add buttons and a 24 h ceiling on accept.
- Underlying storage: `storage.set_minutes_for_tag_date()` — a
  **replace**, not an add, distinct from `commit_session()`'s
  merge/add semantics. Replacing to zero drops the row, matching the
  no-row-for-empty-day convention used throughout.
- Naming: "Retime" (not "Change time") deliberately parallels "Retag,"
  already established in the Archive.
- The Archive path is straightforward and was always correct — it
  prefills from that row's own stored value and edits a static past
  row with no live-session complication.

### 13b. Active-session Retime — bugs found and fixed (v0.2.3)

The active-session path had three related issues, none of them a code
regression — this was the feature's original behavior since it was
first built (July 2026), just not previously exposed clearly:

- **Wrong prefill.** The dialog prefilled from *banked* minutes only
  (`today_minutes_for_tag`), not the live/unbanked time still accruing
  in the tracker — so it opened showing `00` (or a stale low number)
  instead of the session's real current total.
- **Apparent add-not-replace.** Confirming looked like it added to the
  old value, because a later Save would merge leftover unbanked time
  on top of whatever was just entered.
- **Wrong date on midnight crossing.** A session spanning midnight
  could target the wrong day entirely, since the tracker's
  `start_timestamp` could belong to a different calendar day than
  "today."

**Fix:** prefill now computes today's true total (banked +
correctly midnight-split live elapsed), writes to today's actual date,
and adds `Tracker.rebase_day(day, now)` — zeroes unbanked time on or
after the given day while preserving any portion from *before* it, so
a straddling session banks its pre-boundary portion instead of losing
it to a blanket reset.

**A second bug surfaced only through live testing** (there were zero
tests for Retime before this work): `rebase_day` correctly zeroed the
tracker, but the crash-recovery snapshot (`active_session.json`)
wasn't cleared alongside it. A stale snapshot survived to the next
launch, got re-offered as "unsaved work," and recovering + saving it
added the old amount on top of the already-banked retimed total — a
real, reproduced doubling (187 → 374 minutes on a live data row).

Fixed with two layers:
- `on_retime_session` explicitly clears the snapshot after rebasing,
  mirroring `on_save_session`'s existing clear for the same reason.
- `_write_snapshot` now actively clears any existing snapshot when
  elapsed is zero, rather than merely skipping the write — closing the
  same hole for any *future* code path that zeroes the tracker without
  remembering to clear the snapshot itself.

Genuine crash recovery (an actual unclean shutdown) remains correctly
preserved — verified by a dedicated test alongside the fix.

### 13c. Known gap, deliberately left open

`Tracker.elapsed_seconds()` (the live widget display) is a flat sum,
not midnight-aware on its own — `get_daily_seconds()` is the only
midnight-splitting path, and it only runs at save/rollover, per the
original design (§3). A continuously-running app crossing midnight
could in theory show a mixed two-day total on the widget face for a
window of time. In practice this is bounded to roughly the interval of
the existing always-on rollover timer that detects the date change and
self-corrects — so real exposure is brief, not persistent. Not fixed
as part of this work; noted here so it isn't rediscovered from scratch
later.

### 13d. The Add row, hours-only, and the 24 h ceiling (v0.2.4) — active session only

Retiming used to mean doing the arithmetic yourself: read the running
total off the dialog, add the twenty minutes you forgot to track, type
the sum back in. The Add row does that arithmetic for you.

**Layout** (`RetimeSessionDialog`, replacing the plain `QInputDialog`
that `on_retime_session` used to call):

- **Total** — an `Hh Mm` field, prefilled with today's true total
  (banked + midnight-split live elapsed) per §13b. Retyping it and
  pressing OK behaves as it always did.
- **Add** — a single optional free-text field, empty by default.
  Enter in it fires the dialog's default button (OK).
- **+15m / +30m / +1h** — three quick-add buttons under the field.

**No days field, in either input.** A Retime edits exactly one
(tag, date) row, and a row cannot span days, so a `d` field here is
meaningless — `1d` is a mistake worth surfacing, not a quantity worth
parsing. It is gone from the top row (which now formats via
`_format_hm`, not `_format_dhm`) and from the grammar below. Hours
simply keep counting: 1500 minutes displays as `25h 00m`, never
`01d 01h 00m`.

**Deferred fold-in.** The Add field does not touch the Total field as
you type; the top row keeps showing what is actually stored right up
to the accept. Only on accept is the sum computed:

    new total = Total field + Add field + (a quick button's amount)

**Apply-and-close buttons.** Clicking a quick button folds in the base
field, the Add field's text if any, and that button's amount, then
accepts and closes — one click, done. The buttons do not increment the
Add field and do not stack; two clicks are impossible because the first
closes the dialog. They are explicitly not auto-default, so Enter still
means OK rather than +15m.

**Input grammar** (`_parse_add_delta` — a standalone pure function,
kept separate from `_parse_dhm` and testable without Qt). Both fields
share it: the Add field, where the sign carries meaning, and the top
row, where a total is just an unsigned magnitude. One grammar rather
than a lenient total and a strict adjustment, so `1d` in the top row is
refused outright instead of being silently misread as zero.

This table is the canonical record of the accepted input syntax —
anything the parser rejects is listed below it too, including the cases
the original brief didn't name. Case-insensitive and whitespace-
tolerant. A sign is accepted only at the very front: `-` negates the
whole value, `+` is a no-op that reads the same as no sign at all (the
quick buttons say "+15m", so typing the plus is the natural thing to
do).

| Accepted | Reads as |
| --- | --- |
| `` (empty) | no delta — not an error |
| `90` | 90 m — a bare integer is minutes |
| `-20` | −20 m |
| `1:30`, `0:45` | 90 m, 45 m — colon notation is H:MM |
| `-1:30` | −90 m |
| `45m`, `2h` | 45 m, 120 m |
| `1h30` | 90 m — a trailing bare number after `h` is minutes |
| `1h30m`, `1h 30m` | 90 m |
| `-1h30m` | −90 m |
| `+30`, `+1:30`, `+1h30m` | 30 m, 90 m, 90 m — identical to the unsigned forms |
| `25h`, `36h 30m` | 1500 m, 2190 m — hours are uncapped *by the grammar*; see the ceiling below |

Rejected, raising `AddDeltaError`:

- `1d`, `1d2h`, `1d 2h 30m`, `-1d` — days. Not a unit this dialog has;
  every one of these parsed before the third amendment.
- `1:75` — minutes ≥ 60 in colon notation. Read as a typo signal, not
  as 135 minutes; guessing here is how a mistyped entry becomes a
  silently wrong row.
- `1:30m` — mixed colon and suffix.
- `1:2:3` — three colon fields. `D:H:M` went out with the days unit, so
  this is now just malformed rather than a form to translate. (The
  legacy `DD:HH:MM` still lives in `_parse_dhm`, which the Archive
  dialog and the web editor use.)
- `1.5h` — decimals.
- `1x`, `abc` — unknown letters.
- `h`, `m` — a unit with no number.
- `-`, `+`, `--20`, `++30`, `+-20`, `1h-30`, `1h+30`, `20-` — a sign
  with no number after it, doubled signs, or a sign anywhere but the
  very front.
- `30m 1h` — out-of-order units. A duration reads large-to-small, so
  each unit must be strictly smaller than the one before it.
- `1h 2h` — repeated units. Falls out of the same ordering rule.
- `30m20`, `1 30` — a bare trailing number is minutes only directly
  after an `h`, and only as the last token. After an `m`, or with no
  unit before it at all, it's ambiguous rather than shorthand.

The out-of-order, repeated-unit and bare-trailing-number rejections are
ones the original v0.2.4 brief didn't enumerate; they're recorded here
because this section, not the brief, is the grammar's canonical record.

Note the deliberate difference from `_parse_dhm`, which returns 0 for
junk: these fields are *corrections*, so reading unparseable input as
"no change" — or as zero — would look like the app ignored the user.
On OK or a quick button with a non-empty, unparseable field the dialog
does **not** accept; it stays open and shows a short inline error label
under the Add field. No modal error box.

**Clamped at zero, and zero drops the row.** The folded result is
converted to minutes and clamped to a minimum of 0, so subtracting more
than the day holds empties it rather than going negative. Because the
write is still `storage.set_minutes_for_tag_date()`, landing on 0 drops
the row entirely — the same no-row-for-empty-day convention as typing
`0m` by hand. Overflow normalises for free, since everything is summed
in minutes: 50m + 30m stores 80, which `_format_hm` renders `01h 20m`.

**24 h ceiling, refused on accept.** A Retime can only run against a
day in progress, so a total above 24 h is a typo rather than data. On
OK or a quick button, a folded result over `RetimeSessionDialog.
MAX_MINUTES` (1440) does not accept: the dialog stays open and the same
inline label reads `24h limit exceeded`. Specifics that matter:

- **Exactly 1440 is allowed.** Only strictly greater is refused; 24 h
  is a legal day.
- **Refused, not clamped** — deliberately asymmetric with the
  clamp-at-0 above. Zero is a meaningful total (it drops the row);
  1441 is not a meaningful total, so silently rewriting it to 1440
  would be inventing a number the user didn't ask for.
- **Checked on the folded total**, not on either field alone. `+8h`
  is fine by itself and refused on top of `20h`, and a quick button can
  trip it (23h50m + `+15m`).
- **Not a grammar rule.** `_parse_add_delta` still parses any
  magnitude — `25h` is a perfectly good parse. The ceiling is
  accept-time validation on the result, which is what lets the two
  concerns be tested separately.
- **The prefill is exempt.** Rows over 1440 already exist — the web
  editor and the Archive dialog can both write one, and this dialog
  accepted them before this change. So it opens on such a row and
  renders it (`33h 20m` for 2000 minutes); only the accept is refused,
  leaving the user free to reduce it and confirm. Refusing to *load*
  would make the one tool for fixing a bad number the one tool that
  won't open on it.

**It is not a new write path.** The dialog only produces a different
number; `on_retime_session` then does exactly what §13b established —
`set_minutes_for_tag_date` to *today's* real date, `Tracker.rebase_day`,
and an explicit `storage.clear_active_snapshot()`. An add-then-confirm
that skipped that clear would reintroduce the 187 → 374 doubling, so
the regression is pinned by tests on both the OK and quick-button
routes, alongside one proving a genuine crash after an add is still
recoverable.

**None of this went into the Archive dialog, deliberately.** The
Archive row's Retime (`_archive_retime`) stays a plain
retype-the-value prompt in `Dd Hh Mm`, with no Add row, no quick
buttons and no ceiling. Two separate reasons, both intentional:

- **No Add row.** It exists because a *live* session's total is a
  moving number you are correcting mid-flight; a finished past row is
  a static value you are simply restating, and quick-add buttons there
  would invite nudging history a quarter-hour at a time.
- **Days and over-24h values stay reachable there.** The Archive
  dialog is the escape hatch — alongside the web editor — for
  correcting a row that legitimately needs `d`, or for repairing one
  that already exceeds a day. Removing days and capping at 24 h
  everywhere would have left no in-app way to fix such a row.

So the two dialogs now differ in units (`Hh Mm` vs `Dd Hh Mm`), in
having an Add row, and in enforcing a ceiling. That divergence is a
decision, not drift: §13a records it, and it should not be quietly
"reconciled" later by making the two match again.
