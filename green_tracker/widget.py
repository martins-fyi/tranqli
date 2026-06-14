"""
widget.py — Green Tracker's frameless, translucent, rounded tracking widget.

A view that supports two shapes — a rounded **rectangle** (default) and a 35-px
**circle button** — with a calm 2 s crossfade between RUNNING and PAUSED states,
a hover-revealed time, a subtle soft edge around the shape, and a 1 s
soft-elastic width animation that shrinks the rectangle to a square when the
mouse leaves a running widget and re-expands it to fit the current time when
the mouse returns. The widget reads whatever string the provider returns, so
showing `MM` while elapsed < 1 h and `HH:MM` after is handled by the tracker;
the widget just measures the string and sizes to it.

Visibility rules (brief section 5):
- Rectangle: text hidden while RUNNING; revealed on hover. Always shown while PAUSED.
- Circle:    text shown ONLY on hover, in both RUNNING and PAUSED. State is
             communicated by background colour alone.

Animation summary:
- bg_phase     0 ↔ 1 over 2 s InOutCubic     (paused gradient ↔ running green)
- text_opacity 0 ↔ 1 over 2 s InOutCubic     (hover reveal / state reveal)
- widget_width square ↔ expanded over 1 s OutElastic (rectangle only)
                                              (soft single overshoot then settle)

The text alpha at paint time is text_opacity * fit_factor, where fit_factor
linearly grows from 0 at the square width to 1 at the expanded width. This means
text never visibly clips at the rounded edges during a width animation — it
fades in lock-step with the widget being wide enough to show it.

Wiring (done in main.py):

    widget = TrackerWidget(
        time_provider_full=tracker.current_hhmm,   # "HH:MM"  (or "MM" if < 1h)
        time_provider_short=tracker.current_hh,    # "HH"
        font_family=digital_family,                # from QFontDatabase
        start_pos=config.widget_pos,
        size_name=config.widget_size or "medium",
        running=False,
    )
    widget.left_clicked.connect(on_toggle)
    widget.right_clicked.connect(show_context_menu)
    widget.position_changed.connect(config.save_widget_pos)
    # Menu actions call: widget.set_size(...), widget.set_shape(...).
    # On idle auto-pause: widget.set_running(False); widget.ensure_on_screen().
    # Shape is NOT persisted; app always boots in 'rect' (brief section 5).
"""

from __future__ import annotations

import sys
import ctypes
import itertools
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from PySide6.QtCore import (
    Qt, QPoint, QRect, QRectF, Signal, Property, QPropertyAnimation,
    QSequentialAnimationGroup, QPauseAnimation, QEasingCurve, QTimer,
)
from PySide6.QtGui import (
    QPainter, QPainterPath, QColor, QIcon, QLinearGradient, QFont, QFontMetrics,
    QGuiApplication, QFontDatabase, QImage, QBrush, QPixmap, QCursor,
)
from PySide6.QtWidgets import (
    QWidget, QApplication, QPushButton, QHBoxLayout,
)


# --- Locked appearance constants (the look we tuned together) --------------

# --- Color schemes ---------------------------------------------------------
#
# Six named schemes. Earthen is the original locked-in look — bottle
# green running, dusky purple paused, rust idle-auto-pause. The other
# five are alternative palettes that share the same structural rules:
# one colour per state, the two on/off states from distinct hue
# families (Steel is the deliberate exception where on/off are shades
# of neutral grey and the idle state carries the contrast).
#
# Each scheme defines four colours:
#   - running       : solid background when tracking is RUNNING
#   - paused        : solid background when manually paused
#   - auto_pause    : solid background when idle auto-pause fires
#   - text_base     : the readout's mid-gradient colour
#
# The text gloss / shadow stops are derived from text_base by ±12/25
# RGB — same small excursion the original Earthen scheme used. Single
# source of truth: change text_base and gloss / shadow shift with it.
#
# Widget reads its current scheme from self._scheme; menu picker calls
# widget.set_scheme(key) which re-binds and triggers a repaint.

@dataclass
class ColorScheme:
    """One named widget color palette."""
    key: str            # config token, lowercase: "earthen", "twilight", ...
    name: str           # display label for the menu: "Earthen", "Twilight", ...
    running: QColor     # RUNNING bg
    paused: QColor      # PAUSED bg (manual pause)
    auto_pause: QColor  # idle-auto-pause bg
    text_base: QColor   # readout mid-gradient colour

    def text_gloss(self) -> QColor:
        """Top-of-text highlight: text_base lightened by +12 RGB,
        clamped at 255. Subtle — not a sharp two-tone."""
        return QColor(
            min(255, self.text_base.red()   + 12),
            min(255, self.text_base.green() + 12),
            min(255, self.text_base.blue()  + 12),
        )

    def text_shadow(self) -> QColor:
        """Bottom-of-text settled shadow: text_base darkened by -25
        RGB, clamped at 0. Slightly wider excursion than gloss so
        the bottom reads as 'settled', not just dimmer."""
        return QColor(
            max(0, self.text_base.red()   - 25),
            max(0, self.text_base.green() - 25),
            max(0, self.text_base.blue()  - 25),
        )


# Registry. Order here is the display order in the right-click submenu —
# Earthen first as the canonical default, then alphabetical-ish for the
# alternatives.
COLOR_SCHEMES: Dict[str, ColorScheme] = {
    "earthen": ColorScheme(
        key="earthen", name="Earthen",
        running   =QColor("#0d492b"),  # deep bottle green
        paused    =QColor("#463D6D"),  # dusky purple
        auto_pause=QColor("#8B5A2B"),  # rust
        text_base =QColor("#E9E8E4"),  # off-cream
    ),
    "twilight": ColorScheme(
        key="twilight", name="Twilight",
        running   =QColor("#2d4595"),  # royal blue
        paused    =QColor("#583e75"),  # saturated plum
        auto_pause=QColor("#b06530"),  # bright bronze
        text_base =QColor("#ece6f0"),  # cool cream
    ),
    "blossom": ColorScheme(
        key="blossom", name="Blossom",
        running   =QColor("#7a3a55"),  # dusty rose
        paused    =QColor("#3d4a5a"),  # slate blue
        auto_pause=QColor("#a8602d"),  # burnt orange
        text_base =QColor("#ede0e2"),  # rose cream
    ),
    "espresso": ColorScheme(
        key="espresso", name="Espresso",
        running   =QColor("#3d4d5a"),  # slate teal
        paused    =QColor("#3a2620"),  # coffee
        auto_pause=QColor("#a07a40"),  # caramel
        text_base =QColor("#ece0d4"),  # warm cream
    ),
    "hearth": ColorScheme(
        key="hearth", name="Hearth",
        running   =QColor("#9a7030"),  # honey amber
        paused    =QColor("#2d3a4a"),  # deep navy-slate
        auto_pause=QColor("#8a3535"),  # brick red
        text_base =QColor("#ece4d4"),  # warm cream
    ),
    "steel": ColorScheme(
        key="steel", name="Steel",
        running   =QColor("#2d2d2d"),  # neutral charcoal
        paused    =QColor("#5d5d5d"),  # neutral mid grey
        auto_pause=QColor("#7a3535"),  # crimson alert
        text_base =QColor("#eaeaea"),  # neutral light grey
    ),
}

DEFAULT_SCHEME_NAME = "earthen"


def make_scheme_icon(
    scheme: ColorScheme,
    circle_d: int = 12,
    gap: int = 3,
    pad: int = 3,
) -> QIcon:
    """Render a 3-circle preview pixmap for the Color schemes
    submenu. The circles are the scheme's running / paused /
    auto-pause colours, in that order, sized to fit one menu text
    line tall. Default is a 48 × 18 px icon.

    The pixmap is transparent outside the circles so the menu's
    own background shows through correctly across light / dark
    system themes.
    """
    width  = pad * 2 + circle_d * 3 + gap * 2
    height = pad * 2 + circle_d
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    colors = (scheme.running, scheme.paused, scheme.auto_pause)
    for i, color in enumerate(colors):
        x = pad + i * (circle_d + gap)
        painter.setBrush(color)
        painter.drawEllipse(x, pad, circle_d, circle_d)
    painter.end()
    return QIcon(pixmap)


GRADIENT_TILT_FACTOR = 0.08            # horizontal offset of gradient axis as
                                       # a fraction of text width — slight
                                       # tilt so the gloss reads upper-LEFT
                                       # and the shadow lower-RIGHT.

# Geometry — all in logical pixels; Qt handles DPI scaling to physical.
RECT_SIZES        = {"small": 13, "medium": 22, "large": 48}
                                                          # digit font sizes;
                                                          # small ≈ 60 % of
                                                          # medium
PADDING_X         = 8         # horizontal padding inside the visible shape
PADDING_Y         = 11        # slightly taller vertical padding so the digits
                              # sit with a touch more breathing room top & bottom
CIRCLE_DIAMETER   = 35
CIRCLE_FONT_SIZE  = 16
BEVEL_FACTOR      = 0.30      # corner radius = visible_height * BEVEL_FACTOR,
                              # used once the rectangle is clearly wider than
                              # it is tall. At/near square, the corner radius
                              # eases up toward height/2 — see paintEvent.
BEVEL_TRANSITION_RATIO = 1.6  # width:height ratio at which the bevel reaches
                              # the normal BEVEL_FACTOR. At ratio 1.0 (square)
                              # the radius is height/2 so the shape renders
                              # as a true circle; between 1.0 and this value
                              # the corner radius interpolates linearly. Covers
                              # MM-only display, compact running+not-hovered
                              # mode, and the 1 h MM→HH:MM transition in one
                              # smooth shape morph.
FEATHER_MARGIN_PX = 6         # invisible margin around the visible shape so
                              # the soft edge halo + digit glow have room to
                              # spread without being clipped by widget bounds
                              # — wider feather lets the edges blend further
                              # into whatever sits behind the widget
TEXT_ANCHOR_THRESHOLD_PX = 40 # cap on how far the text-position anchor will
                              # project past the widget's current bounds to
                              # the post-animation final centre. Below this,
                              # the text snaps to the final screen position
                              # and stays still through width/offset
                              # animations (no wobble). Above this, the
                              # remaining offset shift is large enough that
                              # painting the text at the projected final
                              # would put it outside the widget — so we fall
                              # back to the natural centre and the text
                              # smoothly tracks the widget instead.
TEXT_BIAS_EM      = 0.0       # no downward bias needed for Uncut Sans —
                              # tight QPainterPath bounding rect already centres
                              # the digits correctly. (Kept as a constant so we
                              # can re-tune if a future font needs it.)

# Typeface — Uncut Sans Semibold, bundled in assets/ and loaded into Qt's
# application font database at startup (see the preview block at the bottom
# of this file, or main.py in the packaged app). The family name is taken
# from whatever Qt assigns when the file is registered, not hardcoded here.
FONT_FILE          = "uncut-sans-medium.otf"
FONT_WEIGHT        = QFont.Medium       # 500 (Medium)

# MM-only display (elapsed < 1 h) gets a smaller font so the two digits sit
# comfortably inside the contracted (now circular) widget shape with
# breathing room at the diagonal corners similar to the HH:MM pill.
MM_FONT_SCALE     = 0.85

# Soft outer halo: a stack of concentric low-alpha rings drawn outside the
# main fill. (delta_px, alpha_0_255). Outer-most first; the main fill covers
# the inner overlap so only the spread outside the edge survives. More rings
# at lower alpha each = a smoother, deeper fall-off that bleeds further into
# what sits behind the widget.
HALO_RINGS = (
    (5.0,    4),
    (3.8,   10),
    (2.7,   20),
    (1.9,   34),
    (1.2,   56),
    (0.7,   85),
    (0.35, 125),
    (0.15, 175),
    (0.06, 220),
)

# Digit "blur": text stamped a handful of times at tight 1–2 px offsets and
# low alpha, underneath the sharp gradient-filled text. Reads as a soft edge
# blur rather than a glow — closer offsets and lower alpha than before.
GLOW_OFFSETS = (
    (-1,  0), ( 1,  0), ( 0, -1), ( 0,  1),   # 4 orthogonal at 1 px (inner)
    (-1, -1), ( 1, -1), (-1,  1), ( 1,  1),   # 4 diagonal at 1 px (inner)
    (-2,  0), ( 2,  0), ( 0, -2), ( 0,  2),   # 4 orthogonal at 2 px (outer)
)
GLOW_ALPHA_INNER = 24         # alpha for the 8 closest (1 px) stamps
GLOW_ALPHA_OUTER = 10         # alpha for the 4 farther (2 px) stamps

# Dithered noise overlay — tiles across the fill to break up the uniform
# colour. Cached on the class as a one-time-generated QPixmap so paintEvent
# stays cheap.
NOISE_TILE_PX        = 96        # tile dimensions; tiled to cover the widget
NOISE_ALPHA_MAX_DARK  = 7        # max alpha for darkening (black) pixels
NOISE_ALPHA_MAX_LIGHT = 3        # max alpha for lightening (white) pixels —
                                 # capped lower because on dark backgrounds
                                 # white noise pixels jump out disproportionately
                                 # due to the much larger luminance gap from
                                 # the bg. Keeps the grain bidirectional in
                                 # feel but balanced in perception across both
                                 # the running (deep green) and paused
                                 # (mid-purple) backgrounds.

# Animation durations and curves.
# State-change fades (start / pause / resume — driven by set_running).
FADE_TO_RUN_MS         = 400   # text opacity fade when transitioning toward
                               # RUNNING — snappy because the user just
                               # clicked and wants to see the change
FADE_BG_TO_RUN_MS      = 600   # bg colour fade when transitioning toward
                               # RUNNING
FADE_BG_TO_RUN_DELAY_MS = 0    # delay before bg starts fading on
                               # transition to RUNNING. Was 100 ms;
                               # now 0 so start/resume changes the bg
                               # colour without any perceived lag.
FADE_TO_PAUSE_MS       = 500   # bg + text fade when transitioning toward
                               # PAUSED — both unified for the snappy
                               # "click registered" feel
# Hover fades — when the user mouses over / off the widget while it's
# already in a state. Separate from state-change because the user's intent
# is different ("I want to read the time" vs "I'm starting a session").
HOVER_IN_MS            = 1000  # text + scale fade-in on mouse-enter.
                               # Was 667 — bumped to 1000 (150 %) for an
                               # even calmer reveal. OutQuint is still
                               # steep early on (text isn't slow to begin
                               # appearing), but over a full second the
                               # rise reads as a deliberate settle into
                               # view rather than a rush. Pairs with
                               # HOVER_IN_DELAY_MS = 583 so the rectangle
                               # is fully settled before the digits
                               # arrive. Grid-aligned (12 × 83.33 ms).
HOVER_IN_DELAY_MS      = 583   # text fade-in waits this long after the
                               # rectangle starts expanding, so the digits
                               # appear during the settling phase of the
                               # width animation rather than fighting it.
                               # Was 333 — shifted later by 250 ms so the
                               # rectangle's expand and elastic settle
                               # are mostly resolved by the time the
                               # text starts arriving. 0 on the way out
                               # (no delay on fade-out). Grid-aligned
                               # (7 × 83.33 ms).
HOVER_SCALE_IN_MS      = 167   # scale fade-in (1.0 → HOVER_SCALE_FACTOR)
                               # on hover-enter — deliberately FAST, so
                               # the vertical / uniform-scale component
                               # of the hover finishes before the width
                               # expansion and text reveal start. After
                               # this, the rest of the expand is purely
                               # horizontal motion (width grows), which
                               # reads more cleanly than scale + width
                               # interpolating in parallel.
                               # 2 × 83.33 ms ≈ 1/12 s — grid-aligned.
HOVER_OUT_MS           = 667   # text fade-out on mouse-leave. Was 333 —
                               # doubled to 667 for a slower, more
                               # deliberate exit. Shape delay = 667 ×
                               # SHRINK_DELAY_RATIO = 420 ms (was
                               # 210 ms), at which point the text has
                               # OutQuint-faded to < 1 % opacity, so
                               # the shape still begins contracting
                               # against an invisible text. Grid-
                               # aligned (8 × 83.33 ms ≈ 1/12 s).
HOVER_SCALE_OUT_MS     = 2333  # the shape's scale-back is twice as long
                               # as the text fade-out, so the size
                               # settling tails noticeably behind the
                               # digit dissolve. Reads as a slow exhale.
                               # Grid-aligned (28 × 83.33).
HOVER_SCALE_FACTOR     = 1.05  # how much larger the whole painted widget
                               # grows at peak hover (uniform scale around
                               # the widget centre). Scale-IN is fast
                               # (HOVER_SCALE_IN_MS) so this lift completes
                               # before the width and text animations,
                               # leaving the rest of the expand horizontal-
                               # only.

# Idle auto-hide: while paused and not hovered, the digits stay visible for
# this long before fading out. Hovering restores them; un-hovering restarts
# the countdown. Doesn't apply in running state — text visibility there is
# already governed by hover.
IDLE_HIDE_DELAY_MS     = 500
FADE_MS                = FADE_TO_RUN_MS   # alias for legacy reads
SHRINK_MS       = 667         # rect width: shrink direction (expanded → square).
                              # Was 1000 — dropped by 1/3 so the shape
                              # animation after hover-out feels less
                              # protracted. With OutCubic the
                              # decelerating settle is still readable,
                              # just over a shorter window. Grid-aligned
                              # (8 × 83.33 ms ≈ 1/12 s).
EXPAND_MS       = 1250        # rect width: expand direction (square → expanded)
                              # Slightly longer than SHRINK_MS so the
                              # settling tail (the part after the initial
                              # snap-to-width) reads as a deliberate
                              # settle rather than rushing to a stop.
                              # Used in _start_width_animation, overriding
                              # the default duration when the animation
                              # is heading toward a wider target.
                              # Grid-aligned (15 × 83.33 ≈ 1/12 s).
REFRESH_MS      = 1000        # hover-scoped minute refresh tick
ELASTIC_AMP     = 1.2         # OutElastic amplitude. Values BELOW 1.0
                              # are treated as 1.0 by Qt's elastic
                              # implementation (the formula uses the
                              # amplitude only when >= 1; below that
                              # it's clamped and a different phase
                              # offset is used). So the previous 0.6
                              # was effectively 1.0, giving ~21 %
                              # overshoot. Bumped to 1.2 for ~33 %
                              # — more visible horizontal stretch
                              # before the settle.
ELASTIC_PERIOD  = 0.45        # period (default 0.3); higher = slower
                              # oscillation. Was 0.7 — dropped to 0.45
                              # for a more pronounced single overshoot
                              # on expand. Brings back the "punch" that
                              # was lost at 0.7, without going into the
                              # multi-bounce ringing territory that the
                              # default 0.3 produces.
SHRINK_DELAY_MS      = 150    # fixed wait before a SHRINK width animation
                              # starts after the trigger fires. Was a ratio
                              # of the active text-fade duration
                              # (SHRINK_DELAY_RATIO × _current_text_fade_ms,
                              # which gave ~210 ms for hover-out at
                              # HOVER_OUT_MS=333). Made absolute so the
                              # shape lead-in feels consistent regardless
                              # of which fade is in flight. Geometry
                              # verified — at 150 ms even on the largest
                              # widget size the OutQuint text fade is at
                              # ~0.4 % opacity by the time the contracting
                              # edges reach the text bounding box, so
                              # there's no visible overlap.
TEXT_LEADS_BG_RATIO  = 0.33   # bg fade waits this fraction of the active fade
                              # duration after the text starts, so the eye
                              # reads "text moves, then the field follows"

# Click feedback: a brief blink of the digit text on left-click. Animates
# CLICK_FLASH_MIN at the midpoint and back to 1.0 — multiplied with the
# regular text opacity in paint, so the digits dip and recover.
CLICK_FLASH_MS  = 200         # total round-trip duration
CLICK_FLASH_MIN = 0.2         # opacity at the dip's deepest point

# Hour-mark chime: at each wall-clock hour while running, the digits fade in,
# hold visible, then fade out — a passive glance-friendly tick that the hour
# turned. Layered on top of normal text_opacity so it shows even while the
# rectangle is in its "running + not hovered" hidden-text state.
CHIME_FADE_IN_MS  = 2000      # Was 1000 — doubled for a more leisurely
                              # "minute is opening" feel that's easier to
                              # catch in peripheral vision.
CHIME_HOLD_MS     = 2000      # Was 1000 — doubled to match. Hold and
                              # fade-in scaled together so the chime
                              # spends a meaningful duration at full
                              # visibility before retreating.
CHIME_FADE_OUT_MS = 4000      # Was 2000 — doubled. Slow exhale; the
                              # chime fades over four seconds, plenty
                              # of time for the eye to register the
                              # tick of the new hour.

# Edge-aware expansion shift: when the widget is in compact (circle /
# square) form near a screen edge and about to expand to a rectangle
# that would clip off-screen, a horizontal offset is animated in parallel
# with the width animation so the expanded shape lands within the screen
# with EDGE_EXPANSION_PADDING_PX of breathing room. The offset returns
# to 0 on contraction.
#
# Both the drag clamp and the expansion shift measure padding against
# the VISIBLE shape, not the widget geometry — the geometry includes
# FEATHER_MARGIN_PX of invisible transparent padding on every side,
# which doesn't count toward edge proximity. The geometry is allowed
# to extend past the screen edge by up to the feather margin.
#
# Drag pad is tight (1 px) so the widget can be placed very close to
# an outer screen edge. The expansion shift uses a slightly larger
# pad (4 px) so the expanded rectangle keeps a small visual gap from
# the edge — the larger visual mass of the rectangle benefits from
# the breathing room.
#
# Padding applies to the outer edges of the widget's CURRENT screen
# (extended one hop through any seams to adjacent screens — see
# _screen_rect). Internal screen seams pass through freely. This
# means every monitor's outer edges get padding regardless of how
# differently sized neighbours are positioned around it.
DRAG_EDGE_PADDING_PX       = 1
EDGE_EXPANSION_PADDING_PX  = 4

# Expansion-shift timing (expansion direction only). Shift and width
# animations start together; the shift just finishes earlier (250 ms vs
# 1500 ms), so visually the widget glides aside slightly ahead of the
# growth without feeling like a two-step sequence.
# (On shrink, both still ride together over SHRINK_MS, no change.)
EXPAND_SHIFT_MS              = 250
WIDTH_AFTER_SHIFT_DELAY_MS   = 0

# Letter-spacing: natural for a properly-engineered sans-serif. (DSEG needed
# ~3 % tighter to close its segment gaps; Uncut Sans does not.)
LETTER_SPACING_PCT = 100.0


def _standard_icon_height(default: int = 32) -> int:
    """Standard Windows icon height (SM_CYICON), ~32 px at 100 % DPI. Returned
    for reference only — sizes were tuned visually relative to this metric.
    Falls back to `default` off-Windows so the module still imports."""
    try:
        h = ctypes.windll.user32.GetSystemMetrics(12)  # SM_CYICON = 12
        return h or default
    except (AttributeError, OSError):
        return default


class TrackerWidget(QWidget):
    """The on-screen widget. Emits intent; never decides timing itself."""

    left_clicked     = Signal()        # a genuine click (not a drag) — toggle state
    right_clicked    = Signal(QPoint)  # global position for the context menu
    position_changed = Signal(QPoint)  # emitted after a drag, for persistence

    _DRAG_THRESHOLD = 4

    # Class-level cache for the noise tile — generated once on first request,
    # shared across all widget instances. Deterministic (fixed RNG seed) so
    # the grain pattern is stable across runs.
    _noise_tile_cache: Optional[QPixmap] = None

    @classmethod
    def _noise_tile(cls) -> "QPixmap":
        if cls._noise_tile_cache is None:
            import random
            rng = random.Random(0xC0FFEE)
            img = QImage(NOISE_TILE_PX, NOISE_TILE_PX, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            # Bidirectional noise — half darkening (black), half lightening
            # (white) pixels. White pixels have a lower alpha cap to keep them
            # from dominating against dark backgrounds.
            for y in range(NOISE_TILE_PX):
                for x in range(NOISE_TILE_PX):
                    if rng.random() < 0.5:
                        a = rng.randint(0, NOISE_ALPHA_MAX_DARK)
                        if a == 0:
                            continue
                        img.setPixelColor(x, y, QColor(0, 0, 0, a))
                    else:
                        a = rng.randint(0, NOISE_ALPHA_MAX_LIGHT)
                        if a == 0:
                            continue
                        img.setPixelColor(x, y, QColor(255, 255, 255, a))
            cls._noise_tile_cache = QPixmap.fromImage(img)
        return cls._noise_tile_cache

    def __init__(
        self,
        time_provider_full: Callable[[], str],
        time_provider_short: Callable[[], str],
        font_family: Optional[str] = None,
        start_pos: Optional[QPoint] = None,
        size_name: str = "medium",
        running: bool = False,
        scheme_name: str = DEFAULT_SCHEME_NAME,
    ) -> None:
        super().__init__(None)
        self._time_full = time_provider_full
        self._time_short = time_provider_short
        self._font_family = font_family
        self._size_name = size_name if size_name in RECT_SIZES else "medium"
        self._shape = "rect"           # always boot in rectangle (brief section 5)
        self._running = running
        self._hovered = False
        # Active colour scheme. Looked up by key in COLOR_SCHEMES; falls
        # back to the default ("earthen") if the persisted config key
        # was renamed or removed between versions. Single source of
        # truth for all colour decisions in paint code — running /
        # paused / auto-pause backgrounds AND the text base + gloss +
        # shadow are all derived from this. Swapped at runtime via
        # set_scheme(); the change repaints immediately.
        self._scheme: ColorScheme = COLOR_SCHEMES.get(
            scheme_name, COLOR_SCHEMES[DEFAULT_SCHEME_NAME],
        )

        # Animated state (driven by QPropertyAnimation; all real Qt properties).
        self._bg_phase = 1.0 if running else 0.0       # 0 = gradient, 1 = green
        # Auto-pause phase — 0 = standard purple paused background;
        # 1 = rust auto-pause background. Goes to 1 when the idle
        # monitor pauses the tracker (main.py.on_idle_detected →
        # set_auto_paused(True)), so the user can tell at a glance
        # the pause was automatic. Goes back to 0 on first mouse-
        # enter via the _auto_pause_anim fade, OR snapped to 0 on
        # any transition to RUNNING (resume via tray menu, etc.).
        # The rust never persists across launches — fresh init is
        # always 0.0.
        self._auto_pause_phase = 0.0
        # Idle-transition progress — 0 = full RUNNING_GREEN bg, 1 =
        # full AUTO_PAUSE_RUST bg, linear in between. Driven from
        # main.py via idle_monitor.idle_progress_changed, which fires
        # every 2s while tracking is RUNNING. Starts crossfading
        # toward rust at 1 min idle and hits full rust at 3 min idle
        # (= the moment auto-pause itself fires, so the transition
        # is seamless: the widget is already rust when the bg_phase
        # animation kicks in to formally enter the paused state).
        # Snaps back to 0 on resume (set_running(True)) so the
        # widget never displays a stale rust tint after a paused
        # period.
        self._idle_progress = 0.0
        self._text_opacity = 0.0                       # set after geometry below
        self._click_flash = 1.0                        # 1.0 normally; dips on click
        # Active text-fade duration in ms — set on each _animate_to_state
        # call so the width-shrink delay scales with whatever animation is
        # currently driving the text out (state change vs hover-out have
        # very different durations).
        self._current_text_fade_ms = FADE_TO_RUN_MS
        # Hover scale: 1.0 normally, animates toward HOVER_SCALE_FACTOR
        # when the mouse enters. Applied as a uniform scale around the
        # widget centre in paintEvent, so the whole shape (halo, bg,
        # text) grows together. Sized to fit within the existing feather
        # margin without clipping noticeable halo rings.
        self._hover_scale = 1.0
        # Motion-blur factor: 0.0 when static, peaks at 1.0 during the
        # fast portion of width animations, decays back to 0 before
        # the slow tail. Scales the alpha of the outer halo rings in
        # paintEvent — the visible "edge" of the widget softens
        # during motion, masking the inherent 1-2 px discrete position
        # steps from integer-rounded geometry math.
        #
        # Driven by _motion_fade_anim, which runs a keyframed
        # sequence over the same duration as the width animation:
        # ramps up by 8 %, holds peak through 30 %, decays to 0 by
        # 60 %, stays at 0 for the remainder. The result is that
        # blur is present only during high-velocity motion, not
        # during the slow deceleration where the discrete steps
        # would already be imperceptible anyway. See
        # _on_width_anim_state for details.
        self._motion_blur_factor = 0.0
        # Paused idle auto-hide: True when the 3 s countdown has expired
        # without a hover. Toggled false on hover / state change / pause.
        # Initialised True so the widget launches with no digits visible
        # — first hover will reveal them, after which the normal 3 s
        # auto-hide cadence takes over.
        self._paused_auto_hidden = True

        # Crossfade animations (2 s, smooth in-out).
        self._bg_anim = QPropertyAnimation(self, b"bg_phase", self)
        self._bg_anim.setDuration(FADE_MS)
        self._bg_anim.setEasingCurve(QEasingCurve.InOutCubic)

        self._opacity_anim = QPropertyAnimation(self, b"text_opacity", self)
        self._opacity_anim.setDuration(FADE_MS)
        self._opacity_anim.setEasingCurve(QEasingCurve.InOutCubic)

        # Hover-in text fade delay: the text fade-in waits HOVER_IN_DELAY_MS
        # after the hover-in is requested, so the rectangle has a chance to
        # snap out to near-final width and start its elastic settle before
        # the digits begin appearing. Stored target/duration so the callback
        # knows what to fire; the timer is canceled on hover-out so an
        # in-flight delay doesn't suddenly reveal the text after the user
        # has already moved away.
        self._text_fade_in_delay_timer = QTimer(self)
        self._text_fade_in_delay_timer.setSingleShot(True)
        self._text_fade_in_delay_timer.timeout.connect(self._start_pending_text_fade_in)
        self._pending_text_fade_target: Optional[float] = None
        self._pending_text_fade_dur: int = HOVER_IN_MS

        # Click-flash animation: 200 ms total, dips to CLICK_FLASH_MIN at the
        # midpoint and returns to 1.0. Multiplied with text_opacity in paint.
        self._flash_anim = QPropertyAnimation(self, b"click_flash", self)
        self._flash_anim.setDuration(CLICK_FLASH_MS)
        self._flash_anim.setKeyValueAt(0.0, 1.0)
        self._flash_anim.setKeyValueAt(0.5, CLICK_FLASH_MIN)
        self._flash_anim.setKeyValueAt(1.0, 1.0)
        self._flash_anim.setEasingCurve(QEasingCurve.InOutCubic)

        # Hover-scale animation: targets 1.0 .. HOVER_SCALE_FACTOR. Same
        # timing as the text fade so the lift and the digit appearance
        # settle together.
        self._hover_scale_anim = QPropertyAnimation(self, b"hover_scale", self)
        self._hover_scale_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Auto-pause fade — animates _auto_pause_phase 1 → 0 on
        # mouse-enter (clearing the rust auto-pause indicator). The
        # opposite direction (0 → 1) is a snap, not animated: when
        # auto-pause fires, the user is by definition away from
        # keyboard and there's no audience for a transition. By the
        # time they look at the widget, it's already rust. A short
        # OutCubic on the fade-back keeps the rust → purple flip
        # noticeable without being abrupt.
        self._auto_pause_anim = QPropertyAnimation(
            self, b"auto_pause_phase", self,
        )
        self._auto_pause_anim.setDuration(300)
        self._auto_pause_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Hour-mark chime: 1 s fade in, 1 s hold, 2 s fade out, layered over
        # the normal text_opacity in paint via max(). Three sub-animations
        # sequenced together because we want different durations and curves
        # per phase.
        self._chime_opacity = 0.0
        _chime_in = QPropertyAnimation(self, b"chime_opacity")
        _chime_in.setDuration(CHIME_FADE_IN_MS)
        _chime_in.setStartValue(0.0)
        _chime_in.setEndValue(1.0)
        _chime_in.setEasingCurve(QEasingCurve.InOutCubic)
        _chime_hold = QPauseAnimation(CHIME_HOLD_MS)
        _chime_out = QPropertyAnimation(self, b"chime_opacity")
        _chime_out.setDuration(CHIME_FADE_OUT_MS)
        _chime_out.setStartValue(1.0)
        _chime_out.setEndValue(0.0)
        _chime_out.setEasingCurve(QEasingCurve.InOutCubic)
        self._chime_anim = QSequentialAnimationGroup(self)
        self._chime_anim.addAnimation(_chime_in)
        self._chime_anim.addAnimation(_chime_hold)
        self._chime_anim.addAnimation(_chime_out)

        # Width animation (1 s, soft elastic — a single gentle overshoot).
        self._width_anim = QPropertyAnimation(self, b"widget_width", self)
        self._width_anim.setDuration(SHRINK_MS)
        # Two curves: OutElastic for expand (the overshoot WIDER is the
        # "punch" feel — width briefly exceeds the rectangle target,
        # then settles). OutCubic for shrink (overshoot NARROWER than
        # the square target would briefly make the widget taller than
        # wide — egg-shaped — which the user found jarring on click-
        # toggle from paused → running). _start_width_animation picks
        # the right one per direction.
        self._expand_curve = QEasingCurve(QEasingCurve.OutElastic)
        self._expand_curve.setAmplitude(ELASTIC_AMP)
        self._expand_curve.setPeriod(ELASTIC_PERIOD)
        self._shrink_curve = QEasingCurve(QEasingCurve.OutCubic)
        self._width_anim.setEasingCurve(self._shrink_curve)

        # Motion-blur fade — runs a keyframed sequence in lockstep
        # with the width animation (same duration). See
        # _on_width_anim_state for the keyframe shape. The easing
        # curve is reset per call there too — kept off the init
        # since the keyframe placement carries the shape, not the
        # curve.
        self._motion_fade_anim = QPropertyAnimation(
            self, b"motion_blur_factor", self,
        )
        # State-change hook on the width animation — triggers the
        # motion-blur fade. Connected here once; lifetime tied to
        # self via QPropertyAnimation's parent= argument above.
        self._width_anim.stateChanged.connect(self._on_width_anim_state)

        # Hover-scoped 1-second minute-refresh timer. Beyond repainting, it
        # also re-fits the widget's width if the displayed string changed
        # length (e.g. MM -> HH:MM at the one-hour mark) so the pill stays
        # snug around the current text instead of being sized to a stale one.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)

        # Delayed-start timer for the width SHRINK animation, so the text has
        # faded below ~20 % before the pill begins to contract.
        self._width_delay_timer = QTimer(self)
        self._width_delay_timer.setSingleShot(True)
        self._width_delay_timer.timeout.connect(self._start_width_animation)

        # Delayed-start timer for the BG colour fade — staggered behind the
        # text fade by 33 % of the active fade duration. Lets the digit
        # opacity move first so the eye perceives the change as "text
        # responding, then the field follows" rather than a single flat
        # crossfade.
        self._bg_delay_timer = QTimer(self)
        self._bg_delay_timer.setSingleShot(True)
        self._bg_delay_timer.timeout.connect(self._bg_anim.start)

        # Paused-idle auto-hide timer. Fires IDLE_HIDE_DELAY_MS after the
        # tracker enters PAUSED + not hovered, at which point the digits
        # fade out (with the hover-out timing). Cancelled the moment the
        # state leaves "paused not hovered".
        self._paused_idle_timer = QTimer(self)
        self._paused_idle_timer.setSingleShot(True)
        self._paused_idle_timer.setInterval(IDLE_HIDE_DELAY_MS)
        self._paused_idle_timer.timeout.connect(self._on_paused_idle_timeout)

        # Topmost re-assertion timer — keeps the widget above other
        # windows by polling SetWindowPos with HWND_TOPMOST every 1 s.
        #
        # Qt.WindowStaysOnTopHint is set on the widget but Windows
        # doesn't strictly enforce it: fullscreen games, UWP apps
        # (Photos, Snipping Tool), some installers, and screen-share
        # surfaces can all push themselves above always-on-top
        # windows, and Windows doesn't reliably restore z-order when
        # they close. The QApplication.focusWindowChanged hook in
        # main.py covers Qt-side focus events but misses the
        # browser-to-editor case (two non-Qt apps swapping focus —
        # no Qt window's state changes, so no signal fires).
        #
        # 1-second poll: one Win32 call per tick (~microseconds),
        # SWP_NOACTIVATE so it never steals focus or flashes the
        # taskbar, _assert_topmost itself short-circuits when the
        # widget isn't visible. Runs for the lifetime of the widget.
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(1000)
        self._topmost_timer.timeout.connect(self._assert_topmost)
        self._topmost_timer.start()

        # Click vs drag bookkeeping.
        self._press_global: Optional[QPoint] = None
        self._win_at_press: Optional[QPoint] = None
        self._dragging = False

        # Horizontal position offset — added to the user's chosen position
        # when the rectangle is expanded near a screen edge so the
        # expansion stays within bounds with EDGE_EXPANSION_PADDING_PX.
        # Animated by _offset_anim alongside the width animation. Returns
        # to 0 when the widget contracts back to compact form, so the
        # widget always converges back to the user's chosen position.
        self._position_offset_x: int = 0

        # Float-precision "effective centre" used during width and offset
        # animations. Refreshed from self.x() + self.width()/2 at the
        # start of each animation, then driven by the offset animation
        # alone (offset deltas shift it). The width animation reads it
        # but doesn't modify it — and that's what eliminates the
        # leftward rounding drift: every tick computes pos from the same
        # float centre, so there's only one rounding step per tick (the
        # final position), not a recursive one that accumulates 0.5-px
        # nudges over ~90 ticks of OutElastic oscillation.
        self._anim_target_center_x: float = 0.0

        # Expansion-shift offset animation: rides alongside _width_anim,
        # shifting the widget horizontally away from a near screen edge
        # so the expanded rectangle fits within bounds with
        # EDGE_EXPANSION_PADDING_PX. Animates back to 0 when contracting,
        # which (in combination with the locked centre) lands the widget
        # back at its resting position.
        self._offset_anim = QPropertyAnimation(self, b"position_offset_x", self)
        self._offset_anim.setDuration(SHRINK_MS)   # matches the width animation
        self._offset_anim.setEasingCurve(QEasingCurve.OutCubic)

        # Delayed-start timer for the offset animation on SHRINK. Runs
        # in parallel with _width_delay_timer so the offset animation
        # waits alongside the width animation rather than starting at
        # t=0. Without this, the offset would race back to 0 while the
        # widget is still expanded, briefly poking past the screen edge.
        self._offset_delay_timer = QTimer(self)
        self._offset_delay_timer.setSingleShot(True)
        self._offset_delay_timer.timeout.connect(
            self._start_offset_animation_to_pending
        )
        self._pending_offset_target: Optional[int] = None

        # State-drift watchdog. Drives _animate_to_state periodically
        # so text_opacity (and the rest of the animated state) gets
        # reconciled with _text_should_show() even when no event has
        # fired. Catches the rare case where text stays visible while
        # not hovered — usually a hover-out animation that stopped
        # mid-flight at a non-zero end value. 5 s is the slowest the
        # user could plausibly notice the stuck state before it self-
        # corrects; the cost (one cheap reconcile on a timer) is
        # negligible. Skipped when an opacity animation is in flight
        # so we don't interrupt a normal fade.
        self._state_watchdog_timer = QTimer(self)
        self._state_watchdog_timer.setInterval(5000)
        self._state_watchdog_timer.timeout.connect(self._on_watchdog_tick)
        self._state_watchdog_timer.start()

        # Window setup: frameless, on top, translucent. Qt.Window (NOT
        # Qt.Tool) so the widget gets a normal taskbar entry — the
        # taskbar icon is supplied via QApplication.setWindowIcon() in
        # main.py from assets/Tranqli.ico. Qt.Tool would explicitly
        # exclude the window from the taskbar (and alt-tab); we want
        # both, so the user can switch to Tranqli like any other app.
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Window
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._font = self._make_font(self._current_font_size())
        self._apply_geometry()

        # Initial text visibility (no animation at startup).
        self._text_opacity = 1.0 if self._text_should_show() else 0.0

        if start_pos is not None:
            self.move(start_pos)

    # ---- Qt animated properties (target of QPropertyAnimation) -------

    def _get_bg_phase(self) -> float:
        return self._bg_phase

    def _set_bg_phase(self, v: float) -> None:
        self._bg_phase = max(0.0, min(1.0, v))
        self.update()

    bg_phase = Property(float, _get_bg_phase, _set_bg_phase)

    def _get_auto_pause_phase(self) -> float:
        return self._auto_pause_phase

    def _set_auto_pause_phase(self, v: float) -> None:
        self._auto_pause_phase = max(0.0, min(1.0, v))
        self.update()

    auto_pause_phase = Property(
        float, _get_auto_pause_phase, _set_auto_pause_phase,
    )

    def _get_idle_progress(self) -> float:
        return self._idle_progress

    def _set_idle_progress(self, v: float) -> None:
        self._idle_progress = max(0.0, min(1.0, v))
        self.update()

    idle_progress = Property(
        float, _get_idle_progress, _set_idle_progress,
    )

    def set_idle_progress(self, value: float) -> None:
        """Public setter wired to the idle monitor's
        idle_progress_changed signal — main.py connects them up.

        Value is 0..1 where 0 = full RUNNING_GREEN bg, 1 = full
        AUTO_PAUSE_RUST. The monitor emits this every 2s while
        tracking is RUNNING; the widget snaps to the new value and
        repaints, producing a stepwise crossfade over the 2-minute
        transition window (60 → 180s idle). Steps are ~1.67 % of
        the color range each, well below the human-perception
        threshold for slow gradient changes.

        Not animated between calls — the monitor's poll rate IS
        the animation rate. Adding a QPropertyAnimation here would
        be redundant and would risk drift if the polling jitters.
        """
        self._set_idle_progress(value)

    def _get_text_opacity(self) -> float:
        return self._text_opacity

    def _set_text_opacity(self, v: float) -> None:
        self._text_opacity = max(0.0, min(1.0, v))
        self.update()

    text_opacity = Property(float, _get_text_opacity, _set_text_opacity)

    def _get_click_flash(self) -> float:
        return self._click_flash

    def _set_click_flash(self, v: float) -> None:
        self._click_flash = max(0.0, min(1.0, v))
        self.update()

    click_flash = Property(float, _get_click_flash, _set_click_flash)

    def _get_chime_opacity(self) -> float:
        return self._chime_opacity

    def _set_chime_opacity(self, v: float) -> None:
        self._chime_opacity = max(0.0, min(1.0, v))
        self.update()

    chime_opacity = Property(float, _get_chime_opacity, _set_chime_opacity)

    def _get_hover_scale(self) -> float:
        return self._hover_scale

    def _set_hover_scale(self, v: float) -> None:
        # Clamp to a safety band — we never want to scale below 1.0 (shrinking
        # would clip the unscaled-text painting) or above HOVER_SCALE_FACTOR
        # (would push the halo outside the feather margin).
        self._hover_scale = max(1.0, min(HOVER_SCALE_FACTOR, v))
        self.update()

    hover_scale = Property(float, _get_hover_scale, _set_hover_scale)

    def _get_motion_blur_factor(self) -> float:
        return self._motion_blur_factor

    def _set_motion_blur_factor(self, v: float) -> None:
        # Clamped [0, 1] for paint sanity — boosts beyond 1.0 would
        # blow out the inner-ring alpha caps and make the widget look
        # like it's catching on fire mid-animation rather than gently
        # softening at the edge.
        self._motion_blur_factor = max(0.0, min(1.0, v))
        self.update()

    motion_blur_factor = Property(
        float, _get_motion_blur_factor, _set_motion_blur_factor,
    )

    def _on_width_anim_state(self, new_state, _old_state) -> None:
        """Fade the motion-blur factor through the width animation
        via a keyframed sequence whose duration matches the width
        animation, so the blur is concentrated on the fast portion
        and gone before the slow tail.

        Keyframes (relative to width_dur, linear between):
        - 0%   start from current factor (smooth handoff)
        - 8%   peak (1.0) — fast onset
        - 30%  still at peak — hold through the high-velocity portion
        - 60%  decayed to 0 — completes before the slow settling tail
        - 100% still at 0

        Concrete examples:

        - OutElastic expand (1250 ms): blur peaks at 100 ms, held
          through the main rise + first overshoot (375 ms), decays
          through the early settling (375-750 ms), and is gone for
          the final 500 ms of low-amplitude oscillation.
        - OutCubic shrink (667 ms): blur peaks at 53 ms, held
          through the steep early portion (200 ms), decays through
          the deceleration (200-400 ms), and is gone for the last
          267 ms of gentle settling.

        Both cases match the perceptual rule "blur during fast
        motion only" — by the time the width is barely moving per
        tick, the blur has already gone.

        Stopped handler is now just a safety net. Under normal
        completion, the keyframed sequence reaches 0 on its own
        before width_anim fires Stopped. If the width animation
        is cancelled early (e.g., a new one preempts it), Stopped
        fires with the blur still mid-sequence — snap to 0 so it
        doesn't linger.
        """
        if new_state == QPropertyAnimation.State.Running:
            width_dur = self._width_anim.duration()
            self._motion_fade_anim.stop()
            self._motion_fade_anim.setDuration(width_dur)
            # Linear easing between keyframes — the keyframe placement
            # itself shapes the visible curve. Using a non-linear
            # easing here would compound with the keyframe spacing in
            # confusing ways.
            self._motion_fade_anim.setEasingCurve(QEasingCurve.Linear)
            # The 5 keyframes always occupy the same step positions,
            # so subsequent calls overwrite cleanly in place — no
            # need to clear prior keyframes between runs.
            self._motion_fade_anim.setStartValue(self._motion_blur_factor)
            self._motion_fade_anim.setKeyValueAt(0.08, 1.0)
            self._motion_fade_anim.setKeyValueAt(0.30, 1.0)
            self._motion_fade_anim.setKeyValueAt(0.60, 0.0)
            self._motion_fade_anim.setEndValue(0.0)
            self._motion_fade_anim.start()
        elif new_state == QPropertyAnimation.State.Stopped:
            # Normal completion: keyframed sequence already reached 0.
            # Edge case: width animation was cancelled before the 60 %
            # decay point completed — snap blur to 0 directly so it
            # doesn't linger on a static widget.
            if self._motion_blur_factor > 0.01:
                self._motion_fade_anim.stop()
                self._set_motion_blur_factor(0.0)

    def show_hour_chime(self) -> None:
        """Trigger the hour-mark chime: fade in 1 s, hold 1 s, fade out 2 s.

        Called by main.py at each wall-clock hour while the tracker is
        running. Safe to call repeatedly — restarts cleanly from the
        beginning, so a chime in progress is replaced by a fresh one.
        """
        self._chime_anim.stop()
        self._chime_anim.start()

    def _get_widget_width(self) -> float:
        return float(self.width())

    def _set_widget_width(self, w: float) -> None:
        h = self.height()
        # Floor width at the current height. Every legitimate width
        # target is >= height (square == height, expanded > height),
        # so this is a no-op for normal animation steps. It catches
        # the case where Qt or a Windows frame quirk would otherwise
        # let the widget render with w < h — which the bevel would
        # clamp into an ellipse (egg). With this floor, the shortest
        # the widget can ever be is square (= circle render).
        candidate = max(h, int(round(w)))
        # Snap candidate's parity to the locked centre's parity. The
        # centre `self._anim_target_center_x` is `start_x + start_w/2.0`,
        # locked at animation start — so its fractional part is .0 (when
        # the starting width was even) or .5 (when the starting width
        # was odd). For the rounded position `center - candidate/2` to
        # land EXACTLY on an integer (no rounding noise), candidate's
        # parity has to match: even width with integer centre, odd width
        # with half-integer centre. Without this, the rounded position
        # and rounded width don't move in lockstep — one edge advances
        # 1 px while the other stays put on alternating ticks, which
        # reads as a horizontal tremor. The fix coarsens the visible
        # width step from 1 px/tick to 2 px/tick (every other tick is
        # a no-op skipped below), but both edges then move together,
        # which the eye reads as smoother than a fast wobble.
        center_frac = self._anim_target_center_x - int(self._anim_target_center_x)
        needs_odd = 0.25 < center_frac < 0.75
        if needs_odd != (candidate % 2 == 1):
            # Bump candidate by 1 to fix parity. +1 is safe — we're
            # already at-or-above the height floor, so adding 1 only
            # moves us further from the floor.
            candidate += 1
        # Position derived from the locked float centre with the now-
        # parity-correct candidate width — falls on an integer exactly.
        # No rounding noise to alternate between adjacent integer
        # positions.
        new_x = int(round(self._anim_target_center_x - candidate / 2.0))
        # Skip when nothing visibly changes. Checking BOTH width and x
        # matters because the parity snap may leave candidate equal to
        # the current width while the locked centre and target imply
        # the position is fine where it is — an extra setGeometry
        # would be a no-op round-trip to the window manager.
        if candidate == self.width() and new_x == self.x():
            return
        # Single atomic geometry update — one Win32 call vs three for
        # setFixedSize+move (setFixedSize internally calls setMinimum-
        # Size and setMaximumSize). Eliminates the transient moment
        # between size and position updates where the widget would
        # have new size at old position, briefly visible to the
        # compositor.
        self.setGeometry(new_x, self.y(), candidate, h)

    widget_width = Property(float, _get_widget_width, _set_widget_width)

    def _get_position_offset_x(self) -> int:
        return self._position_offset_x

    def _set_position_offset_x(self, x) -> None:
        new_x = int(round(float(x)))
        delta = new_x - self._position_offset_x
        if delta == 0:
            return
        self._position_offset_x = new_x
        # The offset shifts the effective centre — track that on the
        # locked float centre so a concurrent width animation reading it
        # sees the post-shift position. Then compute pos from the new
        # centre in one rounding step (same as _set_widget_width).
        self._anim_target_center_x += float(delta)
        new_widget_x = int(round(self._anim_target_center_x - self.width() / 2.0))
        self.move(new_widget_x, self.y())

    position_offset_x = Property(int, _get_position_offset_x, _set_position_offset_x)

    # ---- Public API used by main.py ----------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def shape(self) -> str:
        return self._shape

    @property
    def size_name(self) -> str:
        return self._size_name

    def set_scheme(self, scheme_name: str) -> None:
        """Switch the widget's colour scheme.

        Resolves `scheme_name` against COLOR_SCHEMES; if the key is
        unknown, falls back to the default Earthen scheme. No-op
        when the requested scheme is already active.

        Triggers an immediate repaint — all paint code reads from
        self._scheme, so the new colours show up on the next paint
        without needing to restart in-flight animations. Mid-fade
        crossfades smoothly absorb the new endpoints because the
        _bg_phase / _auto_pause_phase / _idle_progress values stay
        the same — the colours those phases interpolate between are
        what change.
        """
        scheme = COLOR_SCHEMES.get(scheme_name)
        if scheme is None:
            scheme = COLOR_SCHEMES[DEFAULT_SCHEME_NAME]
        if scheme is self._scheme:
            return
        self._scheme = scheme
        self.update()

    def set_running(self, running: bool) -> None:
        if running == self._running:
            return
        self._running = running
        # Going RUNNING clears the auto-pause indicator AND the idle
        # transition tint — both are "paused-via-idle" signals,
        # irrelevant once tracking resumes. Cleared even if the user
        # resumes via a path that doesn't fire enterEvent (tray menu,
        # etc.). The idle monitor will also emit progress=0 when it
        # next ticks; this snap is the belt-and-suspenders that
        # avoids a momentary rust tint before that signal arrives.
        if running:
            if self._auto_pause_phase > 0.0:
                self._auto_pause_anim.stop()
                self._set_auto_pause_phase(0.0)
            if self._idle_progress > 0.0:
                self._set_idle_progress(0.0)
        self._update_paused_idle_timer()
        self._animate_to_state()
        self._update_refresh_timer()

    def set_auto_paused(self, val: bool) -> None:
        """Toggle the rust auto-pause indicator.

        Called from main.py.on_idle_detected (True, when the idle
        monitor pauses the tracker) and from enterEvent / set_running
        (False, when the user comes back to the widget or resumes
        tracking via any path).

        True is a snap — the user was AFK when the indicator turned
        on, so there's no audience for an animation. By the time
        they look, the rust is already there.

        False is animated — a 300 ms OutCubic crossfade rust →
        purple, so the indicator visibly clears at the moment of
        first interaction rather than disappearing instantly.
        """
        target = 1.0 if val else 0.0
        if abs(self._auto_pause_phase - target) < 1e-3:
            return
        self._auto_pause_anim.stop()
        if val:
            # Snap to rust (no animation when going INTO auto-pause).
            self._set_auto_pause_phase(1.0)
        else:
            # Animated fade out of auto-pause back to standard purple.
            self._auto_pause_anim.setStartValue(self._auto_pause_phase)
            self._auto_pause_anim.setEndValue(0.0)
            self._auto_pause_anim.start()

    def set_size(self, name: str) -> None:
        if name not in RECT_SIZES or name == self._size_name:
            return
        self._size_name = name
        if self._shape == "circle":
            self._shape = "rect"
        self._font = self._make_font(self._current_font_size())
        # Snap to new dimensions for the new size — keep centre stable.
        self._width_anim.stop()
        self._apply_geometry()
        self._update_paused_idle_timer()
        self._animate_to_state()
        self._update_refresh_timer()

    def set_shape(self, shape: str) -> None:
        if shape not in ("rect", "circle") or shape == self._shape:
            return
        self._shape = shape
        self._font = self._make_font(self._current_font_size())
        self._width_anim.stop()
        self._apply_geometry()
        self._update_paused_idle_timer()
        self._animate_to_state()
        self._update_refresh_timer()

    def refresh(self) -> None:
        """Force a repaint (e.g. right after a toggle)."""
        self.update()

    def ensure_on_screen(self) -> None:
        """Bring the widget into view on a visible monitor, without
        stealing focus — brief section 6.

        On Windows, Qt's `activateWindow()` requests foreground focus,
        which the OS blocks for background-process windows and instead
        surfaces as a taskbar attention flash (icon flash, sometimes
        the taskbar itself surfacing if auto-hide is on). The user's
        rust auto-pause bg colour is enough notification of the pause
        — we don't want the taskbar joining in.

        So we deliberately avoid both `activateWindow()` and `raise_()`.
        The widget has `Qt.WindowStaysOnTopHint`, so once it's visible
        and not minimized, it sits above other always-on-top siblings
        on its own. We just need to:

        1. Un-minimize if Windows had it minimized to the taskbar.
        2. Show it if it was hidden-to-tray.
        3. Move onto a visible monitor if the stored position is off-
           screen (e.g., a now-disconnected secondary display).
        4. Re-assert HWND_TOPMOST via Win32 — some Windows apps
           (notably UWP apps like Photos / Snipping Tool) push
           themselves above always-on-top windows and don't always
           restore z-order on close, leaving Tranqli buried even
           though Qt.WindowStaysOnTopHint is still set.

        None of these operations request focus.
        """
        if self.windowState() & Qt.WindowMinimized:
            # Clear ONLY the minimized bit; preserve any other window
            # state flags. setWindowState({}) would also un-maximize,
            # which we don't want to touch.
            self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        if not self.isVisible():
            self.show()
        frame = self.frameGeometry()
        on_a_screen = any(
            s.availableGeometry().intersects(frame)
            for s in QGuiApplication.screens()
        )
        if not on_a_screen:
            area = QGuiApplication.primaryScreen().availableGeometry()
            self.move(area.left() + 40, area.top() + 40)
            self.position_changed.emit(self.frameGeometry().topLeft())
        self._assert_topmost()

    def _assert_topmost(self) -> None:
        """Re-assert HWND_TOPMOST on the widget via Win32 SetWindowPos.

        Qt.WindowStaysOnTopHint is set at window construction and
        should keep us above other windows, but some Windows apps
        (Photos, the Snipping Tool, certain installers) push
        themselves above always-on-top windows and don't always
        restore z-order when they close — Tranqli stays buried even
        though the flag is still notionally set.

        Two important details:

        1. **NOTOPMOST → TOPMOST toggle.** Some Windows apps (notably
           fullscreen UWP like Photos) leave the z-order in a state
           where simply re-asserting TOPMOST against TOPMOST is
           ignored as a no-op. Demoting to NOTOPMOST first forces
           Windows to actually re-process the flag on the subsequent
           re-promotion to TOPMOST.

        2. **Explicit ctypes argtypes.** Without argtypes, ctypes
           treats Python ints as c_int (32-bit). HWND is pointer-
           sized — 64-bit on modern Windows — so an HWND with any
           bits set in the upper 32 gets truncated and the call
           silently fails. wintypes.HWND (alias for c_void_p) handles
           the full 64-bit width.

        SWP_NOACTIVATE keeps focus where it is — no taskbar attention
        flash, no focus theft. Cheap calls (microseconds).

        Guards:
        - Windows only — call is a no-op on other platforms.
        - winId() is only valid once the widget has been shown
          and has an actual HWND; skip if not visible yet.
        """
        if sys.platform != "win32":
            return
        if not self.isVisible():
            return
        try:
            HWND_TOPMOST       = -1
            HWND_NOTOPMOST     = -2
            SWP_NOSIZE         = 0x0001
            SWP_NOMOVE         = 0x0002
            SWP_NOACTIVATE     = 0x0010
            SWP_NOSENDCHANGING = 0x0400

            SetWindowPos = ctypes.windll.user32.SetWindowPos
            # Idempotent: set argtypes once per process. Subsequent
            # calls hit the same function object and skip the setup.
            if not getattr(SetWindowPos, "_tranqli_argtypes_set", False):
                SetWindowPos.argtypes = [
                    ctypes.c_void_p,   # hwnd (pointer-sized)
                    ctypes.c_void_p,   # hwndInsertAfter
                    ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int,
                    ctypes.c_uint,
                ]
                SetWindowPos.restype = ctypes.c_bool
                SetWindowPos._tranqli_argtypes_set = True

            hwnd = int(self.winId())
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOSENDCHANGING

            # Toggle: demote, then re-promote. Forces Windows to
            # re-process the topmost flag in cases where the z-order
            # has been corrupted by a fullscreen UWP exit.
            SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            SetWindowPos(hwnd, HWND_TOPMOST,   0, 0, 0, 0, flags)
        except (OSError, AttributeError):
            # Best-effort — if the Win32 call fails for any reason
            # (DLL unavailable, HWND invalidated mid-call) just
            # skip; the next timer tick will retry.
            pass

    # ---- Internal helpers --------------------------------------------

    def _current_font_size(self) -> int:
        if self._shape == "circle":
            return CIRCLE_FONT_SIZE
        return RECT_SIZES[self._size_name]

    def _make_font(self, pixel_size: int) -> QFont:
        font = QFont(self._font_family) if self._font_family else QFont()
        font.setPixelSize(pixel_size)
        font.setWeight(FONT_WEIGHT)
        font.setLetterSpacing(QFont.PercentageSpacing, LETTER_SPACING_PCT)
        return font

    def _visible_height(self) -> int:
        """Height of the visible shape (excluding the feather margin)."""
        if self._shape == "circle":
            return CIRCLE_DIAMETER
        return self._current_font_size() + 2 * PADDING_Y

    def _effective_font_for_text(self, text: str) -> QFont:
        """The font actually used to render `text` right now. For rectangle
        widgets, MM-only strings (length <= 2) shrink by MM_FONT_SCALE so
        the two digits sit comfortably inside the contracted widget shape.

        Small is the exception — at 13 px base, the MM shrink would push
        the digits to ~11 px and hurt legibility, so small keeps its full
        font size in the circular MM display.
        """
        if self._shape == "rect" and len(text) <= 2 and self._size_name != "small":
            return self._make_font(int(round(self._current_font_size() * MM_FONT_SCALE)))
        return self._font

    def _expanded_outer_width(self) -> int:
        """Outer widget width when the rectangle is expanded around the text."""
        text = self._time_full()
        # MM-only display: keep the widget square so its visible shape is a
        # circle. Height stays unchanged across MM and HH:MM displays — only
        # the width differs — so the 1 h transition just animates wider.
        if self._shape == "rect" and len(text) <= 2:
            return self._visible_height() + 2 * FEATHER_MARGIN_PX
        font = self._effective_font_for_text(text)
        text_w = QFontMetrics(font).horizontalAdvance(text)
        return text_w + 2 * PADDING_X + 2 * FEATHER_MARGIN_PX

    def _square_outer_width(self) -> int:
        """Outer widget width when the rectangle is shrunk to a square."""
        return self._visible_height() + 2 * FEATHER_MARGIN_PX

    def _compact_width(self) -> int:
        """Outer widget width in the widget's COMPACT (no-text) form,
        regardless of whether it's currently expanded. Used by the drag
        clamp so positioning reflects the resting form the user will
        actually see when the mouse leaves the widget — not the
        temporarily-expanded rectangle during the drag itself.

        Circle shape: fixed CIRCLE_DIAMETER + feather margin.
        Rectangle shape: the square form (visible_height + feather).
        """
        if self._shape == "circle":
            return CIRCLE_DIAMETER + 2 * FEATHER_MARGIN_PX
        return self._square_outer_width()

    def _target_width(self) -> int:
        """Outer widget width target given current shape/state/hover/text-visibility.

        Width follows text visibility: when the digits aren't shown the
        rectangle contracts to a square (which, because the bevel is
        height/2 in that case, renders as a circle). This applies in both
        states — running widget is hidden-by-default on leave, paused
        widget auto-hides after IDLE_HIDE_DELAY_MS — so in both cases the
        widget settles into a compact disc when the mouse is away.
        """
        if self._shape == "circle":
            return CIRCLE_DIAMETER + 2 * FEATHER_MARGIN_PX
        if not self._text_should_show():
            return self._square_outer_width()
        return self._expanded_outer_width()

    def _apply_geometry(self) -> None:
        """Snap to size dictated by current shape/state; keep centre stable.

        Uses setGeometry rather than setFixedSize so the size isn't
        locked between calls — the per-tick animation setter
        (_set_widget_width) relies on setGeometry to update size
        each tick, and a sticky setFixedSize constraint would clamp
        those updates and freeze the animation at the last
        _apply_geometry size. The frameless Qt.Window has no resize
        handle, so the OS won't try to resize the widget; constraints
        aren't needed for that.
        """
        h_total = self._visible_height() + 2 * FEATHER_MARGIN_PX
        w_total = self._target_width()
        if self.isVisible():
            # Keep the visual centre stable across the size change —
            # used by set_size / set_shape so a configuration change
            # doesn't appear to jump.
            old_center = self.frameGeometry().center()
            new_x = old_center.x() - w_total // 2
            new_y = old_center.y() - h_total // 2
            self.setGeometry(new_x, new_y, w_total, h_total)
        else:
            self.resize(w_total, h_total)

    def _stable_text_center_x(self) -> float:
        """Widget-local X where the digit text should be centred so its
        SCREEN position stays anchored at the post-animation final
        location throughout in-flight width/offset animations.

        At rest the result equals the natural centre (`self.width() / 2`),
        so this is a no-op when nothing is animating.

        While the width animation runs alone, `_anim_target_center_x`
        stays constant (the widget grows/shrinks around it). Computing
        `_anim_target_center_x - self.x()` gives a widget-local x whose
        SCREEN value is exactly `_anim_target_center_x` every tick —
        even though `self.x()` and `self.width()` keep snapping to
        new integers during the elastic settle. Without this, the
        float-to-int rounding of widget position and width alternate
        in a way that drifts the text's rasterised screen x by a
        sub-pixel each tick — visible as a left-right shake.

        While the offset animation runs (expansion-shift near a
        screen edge), `_anim_target_center_x` ITSELF changes each
        tick by the offset delta. We project past those remaining
        deltas to where the centre will end up, so the text snaps
        to its post-shift final position immediately rather than
        wobbling along with the widget.

        TEXT_ANCHOR_THRESHOLD_PX caps the projection: if the
        remaining offset shift is large enough that painting the
        text at the final centre would put it well outside the
        current widget bounds, we fall back to the natural centre
        so the text moves smoothly with the widget rather than
        being painted far away from its visible bounds.
        """
        natural = self.width() / 2.0
        width_running = (
            self._width_anim.state() == QPropertyAnimation.Running
        )
        offset_running = (
            self._offset_anim.state() == QPropertyAnimation.Running
        )
        if not width_running and not offset_running:
            # At rest — natural is correct, and `_anim_target_center_x`
            # may be stale (it's only refreshed when an animation
            # starts), so don't trust it here.
            return natural
        remaining_offset_delta = 0.0
        if offset_running:
            remaining_offset_delta = (
                float(self._offset_anim.endValue()) - self._position_offset_x
            )
        if abs(remaining_offset_delta) > TEXT_ANCHOR_THRESHOLD_PX:
            return natural
        final_screen_center = (
            self._anim_target_center_x + remaining_offset_delta
        )
        return final_screen_center - self.x()

    def _text_should_show(self) -> bool:
        """Visibility rule (brief section 5):
            Rectangle: shown when paused (modulo idle auto-hide),
                       OR when (running AND hovered).
            Circle:    shown ONLY on hover, in both states.

        Paused-idle auto-hide: after IDLE_HIDE_DELAY_MS of paused-and-not-hovered
        the text fades out (driven by the _paused_idle_timer). Hover restores
        it; un-hovering restarts the countdown.
        """
        if self._shape == "circle":
            return self._hovered
        if self._running:
            return self._hovered
        # paused (rect)
        if self._hovered:
            return True
        return not self._paused_auto_hidden

    def _update_paused_idle_timer(self) -> None:
        """Arm or cancel the paused-idle auto-hide countdown based on the
        current state. Called whenever state or hover changes."""
        if self._running or self._hovered or self._shape == "circle":
            # Not in a paused-rect-not-hovered situation; cancel and reset.
            self._paused_idle_timer.stop()
            self._paused_auto_hidden = False
            return
        # Paused, rect, not hovered. If we're already auto-hidden, no need
        # to re-arm. Otherwise (re)start the countdown.
        if not self._paused_auto_hidden:
            self._paused_idle_timer.start()

    def _on_paused_idle_timeout(self) -> None:
        """3 s of paused-not-hovered have passed — fade the digits out.

        Uses the hover-out timing/curve so it reads as a graceful settle
        rather than a state-change snap."""
        # Re-check in case state changed between the timer firing and now.
        if self._running or self._hovered or self._shape == "circle":
            return
        self._paused_auto_hidden = True
        self._animate_to_state(reason="hover")

    def _animate_to_state(self, reason: str = "state") -> None:
        """Drive bg_phase, text_opacity, and (for rect) widget_width toward
        the current state's targets.

        `reason` selects the timing profile:
          - "state" (default): full transition — set_running / set_size /
            set_shape. Text and bg both animate (bg with a small delay so
            text leads). Eased InOutCubic for the calm tracking rhythm.
          - "hover": mouse enter/leave only. Just the text animates (the
            bg colour doesn't change on hover, only the running/paused
            state changes it). Eased OutCubic so most of the visibility
            change happens in the first ~40 % of the duration — the eye
            registers the appearance/disappearance quickly even if the
            tail is long.
        """
        if reason == "hover":
            target_op = 1.0 if self._text_should_show() else 0.0
            if abs(self._text_opacity - target_op) > 1e-3:
                going_in = target_op > self._text_opacity
                text_dur = HOVER_IN_MS if going_in else HOVER_OUT_MS
                if going_in:
                    # Park the target and duration; the timer (or an
                    # immediate call when delay is 0) hands them to
                    # _start_pending_text_fade_in.
                    self._opacity_anim.stop()
                    self._pending_text_fade_target = target_op
                    self._pending_text_fade_dur = text_dur
                    self._text_fade_in_delay_timer.stop()
                    # Both states fire the text fade immediately on
                    # hover-in. The timer machinery is left in place
                    # in case we want delays back later, but the
                    # current direct path is just to call through.
                    self._start_pending_text_fade_in()
                else:
                    # Hover-out fades immediately. Cancel any pending
                    # delayed fade-in so a stale reveal doesn't fire
                    # after the user has already moved away.
                    self._text_fade_in_delay_timer.stop()
                    self._pending_text_fade_target = None
                    self._opacity_anim.stop()
                    self._opacity_anim.setDuration(text_dur)
                    # OutQuint (was InOutCubic): mapped from startValue
                    # = 1.0 to endValue = 0.0, OutQuint drops the
                    # opacity hard at the start — at 100 ms into a
                    # 500 ms fade, the text is already at ~10 %
                    # opacity. The remaining 400 ms is the gentle
                    # settle into invisibility. Snappy onset without
                    # an abrupt cut. The previous InOutCubic at
                    # 833 ms had a slow first half (text lingered)
                    # which read as wrong for "I moved my mouse
                    # away, the text should leave."
                    self._opacity_anim.setEasingCurve(QEasingCurve.OutQuint)
                    self._opacity_anim.setStartValue(self._text_opacity)
                    self._opacity_anim.setEndValue(target_op)
                    self._opacity_anim.start()
                self._current_text_fade_ms = text_dur

            # Hover scale: separate IN and OUT durations.
            # IN is fast (HOVER_SCALE_IN_MS, ~167 ms) so the uniform
            # 5 % lift completes BEFORE the width and text animations
            # do most of their work — leaving the rest of the expand
            # as horizontal-only motion (width grows, text fades in)
            # without scale interpolating in parallel. OUT stays slow
            # (HOVER_SCALE_OUT_MS) for the "slow exhale" feel.
            # 1.0 → HOVER_SCALE_FACTOR.
            target_scale = HOVER_SCALE_FACTOR if self._hovered else 1.0
            if abs(self._hover_scale - target_scale) > 1e-3:
                going_in = target_scale > self._hover_scale
                scale_dur = HOVER_SCALE_IN_MS if going_in else HOVER_SCALE_OUT_MS
                self._hover_scale_anim.stop()
                self._hover_scale_anim.setDuration(scale_dur)
                self._hover_scale_anim.setStartValue(self._hover_scale)
                self._hover_scale_anim.setEndValue(target_scale)
                self._hover_scale_anim.start()
            # No state-change bg animation on hover — bg phase doesn't move.
        else:
            # State-change durations differ by direction.
            if self._running:
                text_dur = FADE_TO_RUN_MS
                bg_dur   = FADE_BG_TO_RUN_MS
                bg_delay = FADE_BG_TO_RUN_DELAY_MS
            else:
                text_dur = FADE_TO_PAUSE_MS
                bg_dur   = FADE_TO_PAUSE_MS
                bg_delay = int(text_dur * TEXT_LEADS_BG_RATIO)

            target_op = 1.0 if self._text_should_show() else 0.0
            if abs(self._text_opacity - target_op) > 1e-3:
                self._opacity_anim.stop()
                self._opacity_anim.setDuration(text_dur)
                self._opacity_anim.setEasingCurve(QEasingCurve.InOutCubic)
                self._opacity_anim.setStartValue(self._text_opacity)
                self._opacity_anim.setEndValue(target_op)
                self._opacity_anim.start()
            self._current_text_fade_ms = text_dur

            target_bg = 1.0 if self._running else 0.0
            if abs(self._bg_phase - target_bg) > 1e-3:
                # Cancel any pending or in-flight bg fade — about to re-aim it.
                self._bg_delay_timer.stop()
                self._bg_anim.stop()
                self._bg_anim.setDuration(bg_dur)
                self._bg_anim.setStartValue(self._bg_phase)
                self._bg_anim.setEndValue(target_bg)
                self._bg_delay_timer.start(max(0, bg_delay))

        if self._shape == "rect":
            self._schedule_width_animation()

    def _start_width_animation(self) -> None:
        target_w = self._target_width()
        if abs(self.width() - target_w) < 1:
            return
        # If an animation is already heading to essentially this target, don't
        # restart it — restarting would re-trigger the elastic overshoot from
        # the current (possibly mid-overshoot) width, causing the pill to look
        # transiently too wide or off-centred.
        if (self._width_anim.state() == QPropertyAnimation.Running
                and abs(self._width_anim.endValue() - target_w) < 1):
            return
        # Direction-based duration AND curve. Expand uses EXPAND_MS
        # (longer) with OutElastic (overshoot wider, settle back).
        # Shrink uses SHRINK_MS (shorter) with OutCubic (decelerate
        # cleanly into the target square without overshoot — an
        # OutElastic shrink dips narrower than square and looks like
        # an egg).
        expanding = target_w > self.width()
        self._width_anim.setDuration(
            EXPAND_MS if expanding else SHRINK_MS
        )
        self._width_anim.setEasingCurve(
            self._expand_curve if expanding else self._shrink_curve
        )
        # Lock the float effective centre from the current widget pos. The
        # width setter uses this same value every tick (one rounding step,
        # no accumulation), and the concurrent offset animation shifts it
        # by the offset delta each tick. See the comment on
        # _anim_target_center_x for the rationale.
        self._anim_target_center_x = self.x() + self.width() / 2.0
        self._width_anim.stop()
        self._width_anim.setStartValue(float(self.width()))
        self._width_anim.setEndValue(float(target_w))
        self._width_anim.start()

    def _schedule_width_animation(self) -> None:
        """Width changes: expansions start immediately, shrinks wait until
        the text has faded below ~20 % opacity so the pill doesn't begin
        contracting while digits are still clearly visible. The delay is
        scaled off whichever text-fade is currently driving the change
        (state-change ~400 ms vs hover-out 1400 ms).

        Also drives the horizontal expansion-shift offset alongside the
        width: on expansion, the offset is animated to whatever shift is
        needed to keep the expanded rectangle within
        EDGE_EXPANSION_PADDING_PX of the screen edge; on contraction it
        animates back to 0 so the widget converges on its resting
        position.
        """
        self._width_delay_timer.stop()
        target_w = self._target_width()
        if abs(self.width() - target_w) < 1:
            # Width target matches current. But the offset may still need
            # to come home if we're sitting at a compact width after a
            # cancelled animation or shape change.
            if (target_w == self._square_outer_width()
                    and self._position_offset_x != 0):
                # Width's not animating, so no edge-poke risk — animate
                # the offset home immediately.
                self._offset_delay_timer.stop()
                self._pending_offset_target = None
                self._animate_offset_to(0)
            return
        if target_w < self.width():
            # Shrink: delayed start; offset returns to 0 in parallel —
            # but with the SAME delay as the width animation, so the
            # offset doesn't race ahead and briefly push the still-
            # expanded widget past the screen edge.
            delay_ms = SHRINK_DELAY_MS
            self._width_delay_timer.start(delay_ms)
            if self._position_offset_x != 0:
                self._offset_delay_timer.stop()
                self._pending_offset_target = 0
                self._offset_delay_timer.start(delay_ms)
        else:
            # Expand: shift and width animations both start now; the
            # shift just completes earlier (EXPAND_SHIFT_MS) than the
            # width (SHRINK_MS). Cancel any pending shrink-delayed
            # offset since we're heading the other way now.
            self._offset_delay_timer.stop()
            self._pending_offset_target = None
            target_offset = self._compute_expansion_shift(target_w)
            if target_offset != self._position_offset_x:
                self._animate_offset_to(target_offset, duration_ms=EXPAND_SHIFT_MS)
            # WIDTH_AFTER_SHIFT_DELAY_MS may be 0 (synchronous start) or
            # positive (a small lead-in for the shift); branch to avoid
            # the timer round-trip when it's 0.
            if WIDTH_AFTER_SHIFT_DELAY_MS > 0:
                self._width_delay_timer.start(WIDTH_AFTER_SHIFT_DELAY_MS)
            else:
                self._start_width_animation()

    def _animate_offset_to(self, target: int, duration_ms: int = SHRINK_MS) -> None:
        """Animate the horizontal position offset toward `target`. Duration
        defaults to SHRINK_MS so contraction-direction offset returns
        track the width animation; expansion-direction calls pass a
        shorter duration (EXPAND_SHIFT_MS) so the shift visibly leads
        the width."""
        if target == self._position_offset_x:
            return
        # No-op-restart guard, like _start_width_animation: if an
        # animation is already heading to this target with the same
        # duration, leave it alone.
        if (self._offset_anim.state() == QPropertyAnimation.Running
                and self._offset_anim.endValue() == target
                and self._offset_anim.duration() == duration_ms):
            return
        # Refresh the locked centre in case the widget was moved
        # externally (drag, guard snap) since the last animation.
        self._anim_target_center_x = self.x() + self.width() / 2.0
        self._offset_anim.stop()
        self._offset_anim.setDuration(duration_ms)
        self._offset_anim.setStartValue(self._position_offset_x)
        self._offset_anim.setEndValue(int(target))
        self._offset_anim.start()

    def _start_offset_animation_to_pending(self) -> None:
        """Fired by _offset_delay_timer: kicks off the delayed offset
        animation toward whatever target _schedule_width_animation parked
        in _pending_offset_target. A no-op if the pending target was
        cleared (e.g. a subsequent expand pre-empted the shrink)."""
        if self._pending_offset_target is None:
            return
        self._animate_offset_to(self._pending_offset_target)
        self._pending_offset_target = None

    def _start_pending_text_fade_in(self) -> None:
        """Fired by _text_fade_in_delay_timer: kicks off the text fade-in
        toward whatever target _animate_to_state parked in
        _pending_text_fade_target. A no-op if the pending target was
        cleared (e.g. hover-out fired before the delay expired).

        Re-checks the state before starting — if the user already moved
        away during the delay window, the current _text_should_show()
        target won't match the pending one and we abort, leaving the
        text where it is. Hover-out is handled separately and fades
        without delay."""
        if self._pending_text_fade_target is None:
            return
        target_now = 1.0 if self._text_should_show() else 0.0
        if abs(target_now - self._pending_text_fade_target) > 1e-3:
            # State changed during the delay — don't fade in to a stale target.
            self._pending_text_fade_target = None
            return
        target = self._pending_text_fade_target
        dur = self._pending_text_fade_dur
        self._pending_text_fade_target = None
        self._opacity_anim.stop()
        self._opacity_anim.setDuration(dur)
        # OutQuint (was OutCubic): much steeper onset. At 100 ms into
        # a 333 ms fade, OutQuint is at ~91 % vs OutCubic's ~70 % —
        # the digits feel like they snap into existence and then
        # settle, rather than easing in over the whole duration.
        # Tail is still smooth (quintic asymptote rounds out the
        # last 9 % gracefully).
        self._opacity_anim.setEasingCurve(QEasingCurve.OutQuint)
        self._opacity_anim.setStartValue(self._text_opacity)
        self._opacity_anim.setEndValue(target)
        self._opacity_anim.start()

    # ---- Screen-bound geometry helpers -------------------------------

    def _screen_rect(self, widget_rect: Optional[QRect] = None) -> QRect:
        """Return the bounding rect of the widget's current screen plus
        any screens that share a seam with it AND that the widget can
        actually reach across that seam given its current x/y range.

        Compared with screen-only seam detection (where two screens share
        a seam if their perpendicular ranges overlap anywhere), this
        version also checks whether the WIDGET's perpendicular range
        overlaps the seam region. That matters for misaligned monitors:
        if the secondary screen sits above only PART of the primary's
        x range, then at x values outside that overlap there's no seam
        the widget can actually use — the primary's top is an OUTER
        edge for the widget at that x, not a seam. Same idea for
        differing-height neighbours and side seams.

        `widget_rect` defaults to the widget's current frame. Callers
        that need "where would the widget be if we moved here?" pass a
        proposed rect instead.

        Three-tier screen match for the active screen:
        1. Screen whose bounds CONTAIN the widget centre.
        2. CLOSEST screen by distance (dead-zone fallback).
        3. Primary screen, final fallback.
        """
        if widget_rect is None:
            widget_rect = self.frameGeometry()
        center = widget_rect.center()

        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        if not screens:
            return primary.availableGeometry() if primary else QRect()

        current: Optional[QRect] = None
        for s in screens:
            if s.availableGeometry().contains(center):
                current = s.availableGeometry()
                break

        if current is None:
            best_dist = float("inf")
            for s in screens:
                ag = s.availableGeometry()
                nearest_x = max(ag.left(), min(ag.right(), center.x()))
                nearest_y = max(ag.top(), min(ag.bottom(), center.y()))
                dx = center.x() - nearest_x
                dy = center.y() - nearest_y
                dist = dx * dx + dy * dy
                if dist < best_dist:
                    best_dist = dist
                    current = ag
            if current is None:
                return primary.availableGeometry() if primary else QRect()

        # Widget-aware seam extension. Each seam between current and a
        # neighbour `og` only counts if the WIDGET's perpendicular
        # range actually overlaps the seam region — otherwise the seam
        # is somewhere the widget can't reach, and current's edge there
        # should be treated as outer.
        left, right = current.left(), current.right()
        top, bottom = current.top(), current.bottom()
        for s in screens:
            og = s.availableGeometry()
            if og == current:
                continue
            # Left seam: neighbour's right just before ours.
            if og.right() + 1 == current.left():
                seam_y_min = max(og.top(), current.top())
                seam_y_max = min(og.bottom(), current.bottom())
                if (widget_rect.bottom() >= seam_y_min
                        and widget_rect.top() <= seam_y_max):
                    left = min(left, og.left())
            # Right seam.
            if og.left() == current.right() + 1:
                seam_y_min = max(og.top(), current.top())
                seam_y_max = min(og.bottom(), current.bottom())
                if (widget_rect.bottom() >= seam_y_min
                        and widget_rect.top() <= seam_y_max):
                    right = max(right, og.right())
            # Top seam.
            if og.bottom() + 1 == current.top():
                seam_x_min = max(og.left(), current.left())
                seam_x_max = min(og.right(), current.right())
                if (widget_rect.right() >= seam_x_min
                        and widget_rect.left() <= seam_x_max):
                    top = min(top, og.top())
            # Bottom seam.
            if og.top() == current.bottom() + 1:
                seam_x_min = max(og.left(), current.left())
                seam_x_max = min(og.right(), current.right())
                if (widget_rect.right() >= seam_x_min
                        and widget_rect.left() <= seam_x_max):
                    bottom = max(bottom, og.bottom())

        return QRect(left, top, right - left + 1, bottom - top + 1)

    def _clamp_pos_to_workspace(self, pos: QPoint) -> QPoint:
        """Clamp `pos` so the widget's COMPACT visible shape sits at
        least DRAG_EDGE_PADDING_PX inside the active screen-group's
        outer perimeter on every side. The widget geometry can extend
        past the perimeter by up to FEATHER_MARGIN_PX — that's just
        the transparent halo padding, invisible to the user.

        Uses COMPACT dimensions on purpose: during a drag the widget
        is visually expanded (mouse is over it), but the user is
        positioning the form they'll see when the mouse leaves — the
        compact circle / square. Clamping by the expanded rectangle
        would force the resting form too far from the edge. The
        expanded form may extend past the screen edge during the drag
        itself; that's transient, and the next hover-driven expansion
        triggers the expansion-shift, which slides it back inside.

        The "active screen group" is the widget's current screen plus
        any seam-adjacent screens whose seam region is actually reachable
        at this widget rect (see _screen_rect). We pass the COMPACT
        rect at the proposed position — both clamp and seam-reachability
        check use the same resting form, so they agree about which
        seams matter.
        """
        h = self.height()
        w_compact = self._compact_width()
        fm = FEATHER_MARGIN_PX
        pad = DRAG_EDGE_PADDING_PX
        # Compact rect at the proposed position. Used both for screen
        # lookup and for seam-reachability inside _screen_rect, so the
        # "can the widget cross this seam?" decision uses the form the
        # user will see at rest.
        proposed_compact_rect = QRect(pos.x(), pos.y(), w_compact, h)
        screen = self._screen_rect(widget_rect=proposed_compact_rect)
        # Clamp using compact width — the expanded form going partially
        # off-screen during drag is acceptable. Height is the same in
        # both forms so no compact/expanded distinction there.
        min_x = screen.left() + pad - fm
        max_x = screen.right() + 1 - w_compact + fm - pad
        min_y = screen.top() + pad - fm
        max_y = screen.bottom() + 1 - h + fm - pad
        return QPoint(
            max(min_x, min(max_x, pos.x())),
            max(min_y, min(max_y, pos.y())),
        )

    def _compute_expansion_shift(self, target_w: int) -> int:
        """Return the position_offset_x value needed so the expanded
        widget's VISIBLE shape sits at least EDGE_EXPANSION_PADDING_PX
        inside the workspace's outer perimeter horizontally. Returns 0
        when the unshifted expansion already fits.

        Uses the COMPACT rect at the RESTING position for the screen
        lookup — same form _clamp_pos_to_workspace uses, so both
        functions agree about which seams are reachable. The target
        width is the EXPANDED width, but that only affects the visible
        edge math below, not which screen the widget is on.
        """
        fm = FEATHER_MARGIN_PX
        pad = EDGE_EXPANSION_PADDING_PX
        w_now = self.width()
        h = self.height()
        w_compact = self._compact_width()
        resting_x = self.x() - self._position_offset_x
        # _set_widget_width keeps the screen-centre stable as width
        # changes. Resting centre (offset=0) is at:
        resting_center_x = resting_x + w_now / 2.0
        # The COMPACT widget sits centred on resting_center_x. Build
        # its rect at the resting (offset=0) position.
        compact_resting_x = int(round(resting_center_x - w_compact / 2.0))
        compact_resting_rect = QRect(compact_resting_x, self.y(), w_compact, h)
        screen = self._screen_rect(widget_rect=compact_resting_rect)
        # Where an unshifted expanded widget's VISIBLE right pixel and
        # VISIBLE left pixel would land. Geometry left = centre - w/2.
        # Visible right pixel = geometry.x + target_w - fm - 1
        #                     = resting_center_x + target_w/2 - fm - 1
        # Visible left pixel  = resting_center_x - target_w/2 + fm
        unshifted_visible_right = resting_center_x + target_w / 2.0 - fm - 1
        unshifted_visible_left  = resting_center_x - target_w / 2.0 + fm
        if unshifted_visible_right > screen.right() - pad:
            # Shift left so visible right pixel sits at screen.right - pad.
            target_center = screen.right() - pad - target_w / 2.0 + fm + 1
            return int(round(target_center - resting_center_x))   # < 0
        if unshifted_visible_left < screen.left() + pad:
            # Shift right so visible left pixel sits at screen.left + pad.
            target_center = screen.left() + pad + target_w / 2.0 - fm
            return int(round(target_center - resting_center_x))   # > 0
        return 0

    # ---- Refresh timer (1 s, hover-scoped) ---------------------------

    def _update_refresh_timer(self) -> None:
        """The 1-second timer runs only when the displayed minute could change
        AND the text is actually visible — i.e. running and hovered. Otherwise
        stopped."""
        if self._running and self._hovered:
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        else:
            self._refresh_timer.stop()

    def _on_refresh_tick(self) -> None:
        """Called once per second while running+hovered. Repaints, and if the
        displayed string's natural width has changed (the MM -> HH:MM crossover
        is the main case), smoothly re-animates the widget to the new fit.

        Also acts as a hover-state watchdog: if `_hovered` is True but the
        OS cursor is no longer inside the widget's screen rect, a leaveEvent
        was missed (most likely during a width-shrink animation that pulled
        the widget out from under a near-edge cursor before Qt registered
        the cross-over). We force a leaveEvent so text fades back out and
        the state converges to reality. Uses QCursor.pos() rather than
        self.underMouse() because the latter is set by the same hover
        machinery we're trying to second-guess — it'd report the same
        stuck value."""
        if self._hovered:
            cursor_local = self.mapFromGlobal(QCursor.pos())
            if not self.rect().contains(cursor_local):
                # State drift detected — let the normal leave path do its
                # work (text fade, width settle, refresh-timer stop).
                self.leaveEvent(None)
                return
        self.update()
        if self._shape == "rect":
            target_w = self._target_width()
            if abs(self.width() - target_w) >= 1:
                self._start_width_animation()

    def _on_watchdog_tick(self) -> None:
        """Reconcile text_opacity (and the rest of animated state)
        with what _text_should_show() dictates, on a slow timer.

        The bug this catches: occasionally the hover-out fade
        animation gets stopped mid-flight and the opacity value
        is left somewhere above zero with no event scheduled to
        finish the job. The refresh timer is gated on _hovered=True
        so it's stopped in that scenario — the displayed minute
        also stops updating, which is how the symptom shows up
        (the digits stay visible and stale until something else
        triggers a repaint).

        Calling _animate_to_state(reason="hover") here re-evaluates
        the target from current state and starts an animation if
        the actual opacity has drifted. When state is already
        consistent (the common case), the threshold check inside
        _animate_to_state makes this a cheap no-op. Skipped when
        the opacity animation is genuinely running so we don't
        interrupt a normal fade in progress.
        """
        if self._opacity_anim.state() == QPropertyAnimation.State.Running:
            return
        self._animate_to_state(reason="hover")

    @staticmethod
    def _interp_color(c0: QColor, c1: QColor, t: float) -> QColor:
        t = max(0.0, min(1.0, t))
        return QColor(
            int(c0.red()   + (c1.red()   - c0.red())   * t),
            int(c0.green() + (c1.green() - c0.green()) * t),
            int(c0.blue()  + (c1.blue()  - c0.blue())  * t),
        )

    def _edge_color(self) -> QColor:
        """Representative colour for the soft outer halo, interpolated between
        a darker-paused average and the running green by bg_phase. The
        paused average shifts to rust via _auto_pause_phase, and the
        running color shifts toward rust via _idle_progress — so the
        halo tracks the bg through both the idle-transition crossfade
        AND the auto-pause snap, without visible discontinuities at
        the handoff."""
        paused = self._interp_color(
            self._scheme.paused, self._scheme.auto_pause,
            self._auto_pause_phase,
        )
        running = self._interp_color(
            self._scheme.running, self._scheme.auto_pause,
            self._idle_progress,
        )
        return self._interp_color(paused, running, self._bg_phase)

    # ---- Painting -----------------------------------------------------

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Hover scale — uniform scale around the widget centre, applied
        # before everything else so halo, bg, hover overlay, noise, and
        # text all grow together. At self._hover_scale == 1.0 the
        # transform is identity, so this is free in the non-hovered case.
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        painter.translate(cx, cy)
        painter.scale(self._hover_scale, self._hover_scale)
        painter.translate(-cx, -cy)

        # The widget itself is FEATHER_MARGIN_PX larger than the visible shape
        # on each side so we have room to paint the soft halo outside the main
        # fill without being clipped by widget bounds.
        inset_rect = QRectF(self.rect()).adjusted(
            FEATHER_MARGIN_PX, FEATHER_MARGIN_PX,
            -FEATHER_MARGIN_PX, -FEATHER_MARGIN_PX,
        )

        def make_path(rect: QRectF) -> QPainterPath:
            path = QPainterPath()
            if self._shape == "circle":
                path.addEllipse(rect)
            else:
                # Dynamic bevel: at/near square the corner radius is
                # shorter/2 (rendering as a circle), easing down to
                # BEVEL_FACTOR as the rect grows longer than tall.
                #
                # SYMMETRIC in w vs h: if w >= h we get a horizontal
                # pill (the normal HH:MM rectangle), if w < h we get a
                # vertical pill. Without the symmetry, w < h falls
                # through with r = h/2, which addRoundedRect then
                # clamps to w/2 in the x-direction — producing an
                # ELLIPSE (egg). Using min(w, h) for the radius keeps
                # the rounding aligned with the shorter axis, so the
                # shape stays a true pill in either orientation.
                h = rect.height()
                w = rect.width()
                shorter = min(w, h)
                longer = max(w, h)
                ratio = longer / shorter if shorter > 0 else 1.0
                blend = min(1.0,
                    (ratio - 1.0) / (BEVEL_TRANSITION_RATIO - 1.0))
                factor = 0.5 * (1.0 - blend) + BEVEL_FACTOR * blend
                r = shorter * factor
                path.addRoundedRect(rect, r, r)
            return path

        # ---- Subtle soft outer halo: a stack of concentric low-alpha rings
        # just outside the main fill. Each ring sits outside the previous; the
        # main fill covers the inner overlap so only the spread BEYOND the edge
        # survives. With a larger feather margin we can stack four for a
        # softer, deeper glow than before.
        #
        # During a width animation, the outer rings' alpha is boosted —
        # _motion_blur_factor goes 0→1 on anim start and back on stop.
        # Boost is weighted by delta so the outermost rings (currently
        # very faint at alpha 4-10) get the biggest proportional bump,
        # widening the visible soft edge. Inner rings (already near
        # opaque, contributing the "hard edge" perception) get only a
        # minor tweak. Net effect: the widget's visible edge softens
        # during motion, masking the inherent 1-2 px discrete position
        # steps from integer-rounded geometry math.
        edge_col = self._edge_color()
        blur = self._motion_blur_factor
        for delta, alpha in HALO_RINGS:
            if blur > 0.0:
                # Linear delta-weighted boost: outermost rings (delta=5)
                # get up to 4x alpha at peak motion; innermost (delta=
                # 0.06) get only ~1.07x. The 3.0 coefficient was tuned
                # up from 1.0 because the original boost was nearly
                # invisible — outermost alpha 4 → 8 looked the same as
                # plain alpha 4 to the eye. At 3.0 the outer halo
                # actually thickens visibly during motion, which is
                # what the soft-edge masking is supposed to do.
                boost = 1.0 + blur * (delta / 5.0) * 3.0
                effective_alpha = min(255, int(round(alpha * boost)))
            else:
                effective_alpha = alpha
            halo_rect = inset_rect.adjusted(-delta, -delta, delta, delta)
            halo_path = make_path(halo_rect)
            c = QColor(edge_col)
            c.setAlpha(effective_alpha)
            painter.fillPath(halo_path, c)

        # ---- Background: paused gradient as base, green overlaid by bg_phase.
        # The paused base itself is lerp(scheme.paused, scheme.auto_pause,
        # _auto_pause_phase) — standard scheme paused colour by default,
        # auto-pause colour when the idle monitor has auto-paused the
        # tracker. Crossfades back via the _auto_pause_anim when the
        # user returns and hovers in.
        # The running overlay is lerp(scheme.running, scheme.auto_pause,
        # _idle_progress) — pure running colour at progress=0, fully
        # auto-pause at progress=1 (= the moment auto-pause fires). The
        # seamless handoff: at that moment both the running color and
        # the paused color match scheme.auto_pause, so the bg_phase
        # animation 1→0 that follows doesn't cross any visible color.
        # paused_top and paused_bot use the same scheme.paused value —
        # placeholder for a future top/bottom gradient that's currently
        # flat by design.
        paused_top = self._interp_color(
            self._scheme.paused, self._scheme.auto_pause,
            self._auto_pause_phase,
        )
        paused_bot = paused_top
        running_col = self._interp_color(
            self._scheme.running, self._scheme.auto_pause,
            self._idle_progress,
        )
        main_path = make_path(inset_rect)
        bg_gradient = QLinearGradient(inset_rect.topLeft(), inset_rect.bottomLeft())
        bg_gradient.setColorAt(0.0, paused_top)
        bg_gradient.setColorAt(1.0, paused_bot)
        painter.fillPath(main_path, bg_gradient)
        if self._bg_phase > 0.0:
            painter.save()
            painter.setOpacity(self._bg_phase)
            painter.fillPath(main_path, running_col)
            painter.restore()

        # ---- Dithered noise overlay, clipped to the main shape. Breaks the
        # uniformity of the colour fill subtly — barely perceptible film grain
        # rather than visible texture. The tile is generated once and cached
        # on the class, so this costs only a tiled blit per paint.
        painter.save()
        painter.setClipPath(main_path)
        painter.drawTiledPixmap(self.rect(), self._noise_tile())
        painter.restore()

        # ---- Text. Two layers under one effective_alpha gate:
        #   1) glow stamps — the text path filled multiple times at low alpha
        #      and small offsets, faking an LCD-glow blur under the sharp edge;
        #   2) sharp text — the same path filled with a non-linear
        #      gloss-top → base → settled-shadow gradient whose axis tilts
        #      slightly so the gloss reads upper-LEFT and shadow lower-RIGHT.
        # Opacity is gated by both the text_opacity animation and a fit_factor
        # based on how wide the widget currently is (0 at square, 1 at full
        # expanded) so text fades in lock-step with shape, never clipping.
        if self._text_opacity > 0.001:
            text = self._time_short() if self._shape == "circle" else self._time_full()
            font = self._effective_font_for_text(text)

            if self._shape == "rect":
                exp_w = self._expanded_outer_width()
                sq_w = self._square_outer_width()
                if exp_w > sq_w:
                    fit_factor = max(0.0, min(1.0,
                        (self.width() - sq_w) / (exp_w - sq_w)))
                else:
                    fit_factor = 1.0
            else:
                fit_factor = 1.0

            # The chime briefly overrides the normal text fade — `max` so
            # whichever is higher wins, no double-up when both are visible
            # (e.g. hover + chime overlap).
            effective_alpha = (max(self._text_opacity, self._chime_opacity)
                               * self._click_flash * fit_factor)
            if effective_alpha > 0.001:
                # Build the text as a QPainterPath, centred horizontally
                # on the stable post-animation centre (eliminates the
                # sub-pixel wobble during width-elastic settle and
                # during expansion-shift offset animation) and
                # vertically on inset_rect with the descender-less
                # bias applied. We use the same path for both the
                # glow stamps and the sharp gradient fill so they
                # align perfectly.
                text_path = QPainterPath()
                text_path.addText(0.0, 0.0, font, text)
                br = text_path.boundingRect()
                dx = self._stable_text_center_x() - br.center().x()
                dy = (inset_rect.center().y() - br.center().y()
                      + font.pixelSize() * TEXT_BIAS_EM)
                text_path.translate(dx, dy)
                bbox = text_path.boundingRect()

                base_col   = self._scheme.text_base
                gloss_col  = self._scheme.text_gloss()
                shadow_col = self._scheme.text_shadow()

                painter.save()
                painter.setOpacity(effective_alpha)

                # 1) Glow stamps — first 8 inner (closer, brighter), then 4
                #    outer (further, dimmer). translate/fillPath/translate back.
                for i, (ox, oy) in enumerate(GLOW_OFFSETS):
                    is_outer = i >= 8
                    glow_c = QColor(base_col)
                    glow_c.setAlpha(GLOW_ALPHA_OUTER if is_outer else GLOW_ALPHA_INNER)
                    painter.translate(ox, oy)
                    painter.fillPath(text_path, glow_c)
                    painter.translate(-ox, -oy)

                # 2) Sharp text with non-linear gradient. Tilt the gradient
                #    axis off vertical so the gloss reads upper-LEFT.
                tilt = bbox.width() * GRADIENT_TILT_FACTOR
                text_gradient = QLinearGradient(
                    bbox.center().x() - tilt, bbox.top(),
                    bbox.center().x() + tilt, bbox.bottom(),
                )
                # Narrow gloss at top, long base middle, narrow shadow at bottom.
                text_gradient.setColorAt(0.00, gloss_col)
                text_gradient.setColorAt(0.18, base_col)
                text_gradient.setColorAt(0.78, base_col)
                text_gradient.setColorAt(1.00, shadow_col)

                painter.fillPath(text_path, text_gradient)
                painter.restore()

    # ---- Hover --------------------------------------------------------

    def enterEvent(self, _event) -> None:
        self._hovered = True
        # Clear the rust auto-pause indicator the moment the mouse
        # arrives — the AFK signal has served its purpose. Animates
        # rust → purple over 300 ms via _auto_pause_anim. No-op if
        # we weren't in auto-pause to begin with.
        if self._auto_pause_phase > 0.0:
            self.set_auto_paused(False)
        self._update_paused_idle_timer()
        self._animate_to_state(reason="hover")
        self._update_refresh_timer()

    def leaveEvent(self, _event) -> None:
        self._hovered = False
        self._update_paused_idle_timer()
        self._animate_to_state(reason="hover")
        self._update_refresh_timer()

    def showEvent(self, event) -> None:
        """On first show, clamp the (possibly stale) saved position into
        the current workspace bounds. Handles cases where the config
        position was made on a now-disconnected monitor, or saved under
        an earlier build that allowed off-screen placement.

        Also arms the paused-idle timer so the widget converges from
        its initial wide / text-visible state to the compact disc that
        the rest of the lifecycle assumes. Without this kickstart the
        widget stays wide on launch until a click or hover transition
        triggers _update_paused_idle_timer from one of the usual paths.
        """
        super().showEvent(event)
        if not getattr(self, "_clamped_on_show", False):
            self._clamped_on_show = True
            clamped = self._clamp_pos_to_workspace(self.pos())
            if clamped != self.pos():
                self.move(clamped)
                # Resync the locked centre and persist the corrected pos.
                self._anim_target_center_x = (
                    float(clamped.x()) + self.width() / 2.0
                )
                self.position_changed.emit(clamped)
        # Idempotent. The check inside _update_paused_idle_timer
        # ensures running / hovered / circle / already-hidden states
        # are no-ops; only the launch case (paused + rect + text-
        # visible) actually starts the timer.
        self._update_paused_idle_timer()
        # Reapply font + geometry on show. Defensive against a
        # suspected font-load race: _apply_geometry() during __init__
        # computes width with QFontMetrics, but if the bundled font
        # hasn't finished registering by that point Qt falls back to
        # a system font with wider character widths. The rectangle
        # ends up sized for the fallback while paint later renders
        # with the correctly-loaded bundled font — symptom is widget
        # launching at a wider-than-expected size with correctly-
        # sized text inside. By showEvent the font registration has
        # settled, so a fresh measurement gives the right width.
        self._font = self._make_font(self._current_font_size())
        self._apply_geometry()

    # ---- Mouse: click vs drag, right-click menu trigger ---------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            self.right_clicked.emit(event.globalPosition().toPoint())
            return
        if event.button() == Qt.LeftButton:
            # Click flash — kick off the dip-and-restore animation. Restart
            # from the beginning if a previous flash is still running so
            # rapid clicks each get a fresh visual ping.
            self._flash_anim.stop()
            self._flash_anim.start()

            self._press_global = event.globalPosition().toPoint()
            self._win_at_press = self.frameGeometry().topLeft()
            self._dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is None:
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if not self._dragging and delta.manhattanLength() >= self._DRAG_THRESHOLD:
            self._dragging = True
            # Drag start: kill any in-flight expansion / shift animations
            # so they don't fight the manual moves. Offset is also reset
            # to 0 (silently) — the drag pos becomes the new resting.
            self._width_anim.stop()
            self._width_delay_timer.stop()
            self._offset_anim.stop()
            self._offset_delay_timer.stop()
            self._pending_offset_target = None
            self._position_offset_x = 0
        if self._dragging:
            new_pos = self._win_at_press + delta
            # Clamp to the workspace's outer perimeter (union of every
            # screen) with EDGE_EXPANSION_PADDING_PX of padding. The
            # widget can never be dragged past the outer edge, but
            # internal screen seams are pass-through.
            new_pos = self._clamp_pos_to_workspace(new_pos)
            self.move(new_pos)
            # Keep the locked centre tracking the dragged position so a
            # subsequent animation (e.g. on hover-out) uses the dragged
            # location as its starting centre rather than a stale
            # pre-drag value.
            self._anim_target_center_x = float(new_pos.x()) + self.width() / 2.0

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._press_global is None:
            return
        if self._dragging:
            self.position_changed.emit(self.frameGeometry().topLeft())
        else:
            self.left_clicked.emit()
        self._press_global = None
        self._win_at_press = None
        self._dragging = False


# --- Standalone preview ----------------------------------------------------
# Run on Windows with `python green_tracker/widget.py` (with venv active) to
# eyeball the look. A small controls window lets you toggle running/paused,
# cycle Small/Medium/Large, and switch between rectangle and circle without
# the full app wiring.
if __name__ == "__main__":
    from pathlib import Path
    app = QApplication(sys.argv)

    # Load Uncut Sans Semibold from the project's assets folder. Path is
    # resolved relative to this file so the preview runs the same regardless
    # of the working directory it's launched from.
    script_dir = Path(__file__).resolve().parent
    # green_tracker/widget.py → green-tracker/assets/<file>
    asset_path = script_dir.parent / "assets" / FONT_FILE
    family: Optional[str] = None
    if asset_path.is_file():
        fid = QFontDatabase.addApplicationFont(str(asset_path))
        fams = QFontDatabase.applicationFontFamilies(fid)
        family = fams[0] if fams else None
    if not family:
        print(f"[widget preview] Warning: font not loaded from {asset_path} — "
              f"Qt will fall back to a default. Place '{FONT_FILE}' under "
              f"the assets/ folder to get the intended look.")

    # Cycle a couple of plausible times. The "short" variants emulate the
    # MM-only display the tracker will return while elapsed < 1 hour.
    times_full = itertools.cycle(["00:00", "37", "01:37", "12:48", "07:05"])
    times_short = itertools.cycle(["00", "00", "01", "12", "07"])
    state = {"full": next(times_full), "short": next(times_short)}

    w = TrackerWidget(
        time_provider_full=lambda: state["full"],
        time_provider_short=lambda: state["short"],
        font_family=family,
        size_name="medium",
        running=False,
    )

    sizes_iter = itertools.cycle(["large", "small", "medium"])
    shapes_iter = itertools.cycle(["circle", "rect"])

    def toggle() -> None:
        # Update the displayed string first, then trigger the state change —
        # this way the width animation in set_running uses the new string,
        # not the previous one.
        state["full"] = next(times_full)
        state["short"] = next(times_short)
        w.set_running(not w.is_running)
        w.refresh()

    w.left_clicked.connect(toggle)
    w.right_clicked.connect(lambda pos: print("right-click at", pos))

    panel = QWidget()
    panel.setWindowTitle("Preview controls")
    layout = QHBoxLayout(panel)
    b_toggle = QPushButton("Toggle")
    b_size = QPushButton("Next size")
    b_shape = QPushButton("Next shape")
    b_toggle.clicked.connect(toggle)
    b_size.clicked.connect(lambda: w.set_size(next(sizes_iter)))
    b_shape.clicked.connect(lambda: w.set_shape(next(shapes_iter)))
    for b in (b_toggle, b_size, b_shape):
        layout.addWidget(b)
    panel.show()

    w.show()
    sys.exit(app.exec())
