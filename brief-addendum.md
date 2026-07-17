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
