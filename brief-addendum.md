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
