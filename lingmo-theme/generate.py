#!/usr/bin/env python3
"""Generates the Lingmo app icon theme (and its dark twin) from code.

    python3 generate.py --out DIR [--only NAME...] [--no-png] [--optimize]

writes
    DIR/icons/Lingmo/                 index.theme, apps/scalable/*.svg, apps/<px>/*.png, apps@2x/<px>/*.png
    DIR/icons/Lingmo-dark/            index.theme + links to Lingmo's app dirs
    DIR/icons/hicolor/                fallback copies for names no app ships itself
    DIR/lingmo-icons/plate*.{svg,png} neutral plates + plate.json for lingmo-icons-update

Everything is drawn here: the plate (a superellipse "squircle", not a rounded
rectangle), its gradient, light and shadow, and one bold glyph per app built from
a handful of primitives in a single stroke language.  No network, no randomness:
the same input gives byte-identical SVGs.  PNGs are rendered with rsvg-convert
(or Inkscape) when one is installed.

Design notes
  * Grid: 1024 canvas, plate 896 (87.5 %), the same footprint as WhiteSur's
    plates so Lingmo and inherited icons line up in the dock and the launcher.
  * Lingmo light: a soft glow falls from the top left, like the highlight on the
    Lingmo ribbon logo; plates are vertical gradients of one palette colour with
    a small hue turn, so the set reads as one family.
  * Glyphs are white (or ink on the few paper plates), 56-64 units thick with
    round caps and joins; cut-outs use the plate gradient itself so they line up.
  * 16/22/24 px use a simplified variant: a bigger plate, no shadow, no detail
    elements, thicker strokes (or a hand-made small glyph).
  * QtSvg-safe: no filters, masks or clip paths (the shadow is stacked layers).
"""
import argparse
import colorsys
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------
# Design system: every visual parameter lives here
# --------------------------------------------------------------------------
DESIGN = {
    "canvas": 1024,
    "plate_inset": 64,             # plate = 1024 - 2*64 = 896 (WhiteSur grid)
    "plate_inset_small": 20,       # 16-24 px: the plate fills the pixels
    "squircle_n": 5.0,             # superellipse exponent (continuous curvature corners)
    "squircle_points": 72,         # samples on the curve (Catmull-Rom -> cubic Béziers)
    "grad_lightness": 0.075,       # top lighter / bottom darker, in HLS lightness
    "grad_hue_turn": 5.0,          # degrees: top turns cooler, bottom warmer
    "sheen_alpha": 0.20,           # Lingmo light: radial glow from the top left
    "sheen_center": (0.22, 0.02),  # in plate units
    "sheen_radius": 0.85,
    "highlight_alpha": 0.16,       # top band
    "highlight_stop": 0.45,        # fraction of the plate height
    "base_shade_alpha": 0.10,      # darkening towards the bottom
    "edge_width": 6,               # inner edge stroke (canvas units)
    "edge_top_alpha": 0.55,
    "edge_bottom_alpha": 0.14,
    "light_border_alpha": 0.10,    # hairline around paper plates
    "shadow_layers": 8,            # stacked copies emulate a blur (no SVG filters)
    "shadow_spread": 22,           # canvas units the last layer grows
    "shadow_offset": 16,           # canvas units the last layer drops
    "shadow_alpha": 0.32,          # total opacity under the plate
    "contact_alpha": 0.14,         # tight shadow right under the edge
    "glyph_stroke": 60,            # the stroke weight of the glyph language
    "glyph_small_scale": 1.45,     # stroke multiplier for 16-24 px
    "glyph_shadow_alpha": 0.20,
    "glyph_shadow_offset": 10,
    "sizes": [32, 48, 64, 96, 128, 256, 512],
    "small_sizes": [16, 22, 24],
    "hidpi_sizes": [16, 22, 24, 32, 48, 64, 128, 256],   # @2x
    "plate_content": 0.62,         # third-party logos fit this fraction of the canvas
}

# Lingmo palette: brand blue (lingmo-artwork/brand/lingmo-logo.svg) and the
# accent colours of lingmo-core's theme manager, widened into a full wheel.
PALETTE = {
    "brand":    "#2779FF",   # logo
    "blue":     "#3385FF",   # accent 0
    "red":      "#FF5C6D",   # accent 1
    "green":    "#35BF56",   # accent 2
    "purple":   "#8266FF",   # accent 3
    "magenta":  "#CA64AC",   # accent 4
    "orange":   "#FEA042",   # accent 5
    "slate":    "#4F596B",   # accent 6
    # derived
    "sky":      "#1EA7FF",
    "cyan":     "#12B4C8",
    "teal":     "#0D9C8C",
    "mint":     "#19C08A",
    "lime":     "#86C232",
    "yellow":   "#FFC22E",
    "coral":    "#FF6B4A",
    "rose":     "#FF4F8B",
    "crimson":  "#C8283F",
    "violet":   "#A257F2",
    "indigo":   "#4B5BD8",
    "navy":     "#1F2D52",
    "plum":     "#6E2C8C",
    "steel":    "#4E6A8E",
    "sand":     "#C48A52",
    "charcoal": "#222833",
    "ink":      "#2B3445",
    "deepsea":  "#13566E",
    "nightblue": "#2B3A5C",
    "white":    "#FFFFFF",
    "paper":    "#F3F5F8",
}

# Paper plates (light) have an explicit gradient
PAPER_GRADIENT = ("#FFFFFF", "#DDE2EA")

C = DESIGN["canvas"] / 2   # centre
W = DESIGN["glyph_stroke"]


# --------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------
def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(max(0, min(255, round(c * 255))) for c in rgb)


def shade(color, dl=0.0, dh=0.0, ds=0.0):
    h, l, s = colorsys.rgb_to_hls(*_rgb(color))
    h = (h + dh / 360) % 1
    return _hex(colorsys.hls_to_rgb(h, max(0, min(1, l + dl)), max(0, min(1, s + ds))))


def mix(a, b, t):
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
def fmt(v):
    s = ("%.1f" % v).rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def squircle_d(cx, cy, half, n=None, points=None):
    """Closed superellipse |x|^n + |y|^n = half^n as a smooth cubic path."""
    n = n or DESIGN["squircle_n"]
    points = points or DESIGN["squircle_points"]
    pts = []
    for i in range(points):
        t = 2 * math.pi * i / points
        c, s = math.cos(t), math.sin(t)
        x = half * math.copysign(abs(c) ** (2 / n), c)
        y = half * math.copysign(abs(s) ** (2 / n), s)
        pts.append((cx + x, cy + y))
    # Catmull-Rom through the samples -> cubic Bézier segments
    d = ["M %s %s" % (fmt(pts[0][0]), fmt(pts[0][1]))]
    m = len(pts)
    for i in range(m):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[(i + 1) % m], pts[(i + 2) % m]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d.append("C %s %s %s %s %s %s" % tuple(fmt(v) for v in (*c1, *c2, *p2)))
    return " ".join(d) + " Z"


def polar(cx, cy, r, deg):
    """Point on a circle; 0 deg = 12 o'clock, clockwise."""
    a = math.radians(deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def arc_d(cx, cy, r, a0, a1):
    """Clockwise arc from a0 to a1 (degrees, 0 = 12 o'clock)."""
    x0, y0 = polar(cx, cy, r, a0)
    x1, y1 = polar(cx, cy, r, a1)
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return "M %s %s A %s %s 0 %d 1 %s %s" % (fmt(x0), fmt(y0), fmt(r), fmt(r), large, fmt(x1), fmt(y1))


def arrow_head_d(cx, cy, r, a, size, clockwise=True):
    """Chevron at angle a of a circle, pointing along the clockwise tangent."""
    x, y = polar(cx, cy, r, a)
    t = math.radians(a)
    tx, ty = (math.cos(t), math.sin(t)) if clockwise else (-math.cos(t), -math.sin(t))
    nx, ny = math.sin(t), -math.cos(t)          # outward normal
    bx, by = x - tx * size, y - ty * size
    p1 = (bx + nx * size, by + ny * size)
    p2 = (bx - nx * size, by - ny * size)
    return "M %s %s L %s %s L %s %s" % (fmt(p1[0]), fmt(p1[1]), fmt(x), fmt(y), fmt(p2[0]), fmt(p2[1]))


# --------------------------------------------------------------------------
# Glyph primitives.  Paints: "g" glyph colour, "p" plate gradient (cut-outs),
# "a"/"b" the app's accents, or a literal "#RRGGBB".
# --------------------------------------------------------------------------
def P(d, fill=None, stroke=None, sw=None, op=1.0, detail=False, cap="round", join="round", shadow=True):
    return {"tag": "path", "d": d, "fill": fill, "stroke": stroke, "sw": sw, "op": op,
            "detail": detail, "cap": cap, "join": join, "shadow": shadow}


def Circ(cx, cy, r, **kw):
    e = P("", **kw)
    e.update(tag="circle", cx=cx, cy=cy, r=r)
    return e


def Rect(x, y, w, h, rx=0, **kw):
    e = P("", **kw)
    e.update(tag="rect", x=x, y=y, w=w, h=h, rx=rx)
    return e


def L(*pts):
    """Polyline path through points."""
    return "M " + " L ".join("%s %s" % (fmt(x), fmt(y)) for x, y in pts)


# --------------------------------------------------------------------------
# The apps.  Each one: icon name, pt-BR label (previews), brief, plate colour,
# glyph and optional small glyph.  Glyph boxes sit roughly in 282..742.
# --------------------------------------------------------------------------
class App:
    def __init__(self, name, label, brief, plate, glyph, small=None, ink=None, accent=None,
                 accent2=None, gradient=None, paper=False, hicolor=True):
        self.name, self.label, self.brief = name, label, brief
        self.plate = PALETTE.get(plate, plate)
        self.glyph, self.small = glyph, small
        self.paper = paper
        self.ink = PALETTE.get(ink, ink) if ink else (PALETTE["ink"] if paper else "#FFFFFF")
        self.accent = PALETTE.get(accent, accent) if accent else None
        self.accent2 = PALETTE.get(accent2, accent2) if accent2 else None
        self.gradient = gradient
        self.hicolor = hicolor   # also install into hicolor (fallback for other themes)


def g_settings():
    out = []
    for y, kx in ((372, 432), (512, 604), (652, 476)):
        out.append(P("M 322 %d H 702" % y, stroke="g", op=0.45))
        out.append(Circ(kx, y, 62, fill="g"))
        out.append(Circ(kx, y, 22, fill="p", detail=True, shadow=False))
    return out


def s_settings():
    out = []
    for y, kx in ((402, 422), (622, 602)):
        out.append(P("M 292 %d H 732" % y, stroke="g", sw=70, op=0.5))
        out.append(Circ(kx, y, 96, fill="g"))
    return out


def g_files():
    return [
        P("M 292 362 Q 292 312 342 312 H 446 Q 470 312 486 330 L 516 362 H 682 Q 732 362 732 412 V 452 H 292 Z",
          fill="g", op=0.6),
        Rect(292, 412, 440, 300, 48, fill="g"),
        P("M 452 512 H 572", stroke="p", sw=40, detail=True, shadow=False),
    ]


def g_terminal():
    return [
        P(L((352, 392), (472, 512), (352, 632)), stroke="a", sw=68),
        P("M 522 642 H 692", stroke="g", sw=68),
    ]


def s_terminal():
    return [
        P(L((312, 352), (482, 512), (312, 672)), stroke="a", sw=110),
        P("M 542 672 H 732", stroke="g", sw=110),
    ]


def g_texteditor():
    out = [P("M 312 352 H 712", stroke="g", sw=52, op=0.85),
           P("M 312 462 H 652", stroke="g", sw=52, op=0.85),
           P("M 312 572 H 482", stroke="g", sw=52, op=0.85, detail=True)]
    # pencil, 45 degrees, tip at the lower left
    out.append(P(L((592, 672), (722, 542)), stroke="a", sw=90, cap="butt"))
    out.append(P("M 722 542 L 742 522", stroke="b", sw=90, cap="round", detail=True))
    out.append(P(L((532, 732), (560, 640), (624, 704)) + " Z", fill="#F6D7A7", stroke="#F6D7A7", sw=8))
    out.append(P(L((532, 732), (543, 696), (568, 721)) + " Z", fill="g"))
    return out


def s_texteditor():
    return [P("M 282 352 H 742", stroke="g", sw=84),
            P("M 282 512 H 652", stroke="g", sw=84),
            P(L((552, 702), (722, 532)), stroke="a", sw=120, cap="butt"),
            P(L((482, 772), (509, 659), (595, 745)) + " Z", fill="g")]


def g_notes():
    return [
        P("M 342 292 H 682 Q 732 292 732 342 V 592 L 592 732 H 342 Q 292 732 292 682 V 342 Q 292 292 342 292 Z",
          fill="g"),
        P("M 732 592 H 642 Q 592 592 592 642 V 732 Z", fill="a", shadow=False),
        P("M 362 412 H 662", stroke="b", sw=36, op=0.8, shadow=False),
        P("M 362 502 H 662", stroke="b", sw=36, op=0.8, shadow=False),
        P("M 362 592 H 522", stroke="b", sw=36, op=0.8, shadow=False, detail=True),
    ]


def g_calculator():
    return [
        P("M 402 322 V 482 M 322 402 H 482", stroke="g", sw=56),
        P("M 542 402 H 702", stroke="g", sw=56),
        P("M 349 569 L 455 675 M 455 569 L 349 675", stroke="g", sw=56),
        P("M 542 592 H 702 M 542 652 H 702", stroke="a", sw=56),
    ]


def s_calculator():
    return [
        P("M 372 272 V 472 M 272 372 H 472", stroke="g", sw=96),
        P("M 552 582 H 752 M 552 692 H 752", stroke="a", sw=96),
    ]


def g_video():
    return [
        Circ(C, C, 238, stroke="g", sw=40, op=0.45, detail=True),
        P(L((462, 402), (462, 622), (642, 512)) + " Z", fill="g", stroke="g", sw=60),
    ]


def s_video():
    return [P(L((412, 332), (412, 692), (712, 512)) + " Z", fill="g", stroke="g", sw=90)]


def g_camera():
    return [
        P("M 512 632 V 702 M 422 712 H 602", stroke="g", sw=52),
        Circ(C, 452, 186, fill="g"),
        Circ(C, 452, 108, fill="p", shadow=False),
        Circ(C, 452, 60, fill="a", shadow=False),
        Circ(482, 422, 20, fill="g", op=0.85, shadow=False, detail=True),
    ]


def g_screenshot():
    k = 112
    return [
        P("M 312 %d V 362 Q 312 312 362 312 H %d" % (312 + k, 312 + k), stroke="g"),
        P("M %d 312 H 662 Q 712 312 712 362 V %d" % (712 - k, 312 + k), stroke="g"),
        P("M 712 %d V 662 Q 712 712 662 712 H %d" % (712 - k, 712 - k), stroke="g"),
        P("M %d 712 H 362 Q 312 712 312 662 V %d" % (312 + k, 712 - k), stroke="g"),
        Circ(C, C, 74, fill="g"),
    ]


def s_screenshot():
    k = 150
    return [
        P("M 262 %d V 262 H %d" % (262 + k, 262 + k), stroke="g", sw=96),
        P("M %d 262 H 762 V %d" % (762 - k, 262 + k), stroke="g", sw=96),
        P("M 762 %d V 762 H %d" % (762 - k, 762 - k), stroke="g", sw=96),
        P("M %d 762 H 262 V %d" % (262 + k, 762 - k), stroke="g", sw=96),
        Circ(C, C, 100, fill="g"),
    ]


def g_lock():
    return [
        P("M 412 472 V 402 A 100 100 0 0 1 612 402 V 472", stroke="g", sw=60),
        Rect(332, 452, 360, 290, 60, fill="g"),
        Circ(C, 572, 38, fill="p", shadow=False),
        P("M 512 590 V 652", stroke="p", sw=34, shadow=False),
    ]


def s_lock():
    return [
        P("M 382 462 V 382 A 130 130 0 0 1 642 382 V 462", stroke="g", sw=96),
        Rect(272, 442, 480, 340, 70, fill="g"),
        Circ(C, 602, 60, fill="p", shadow=False),
    ]


def g_store():
    # a shopping bag: tapered body, wide shallow handle on two eyelets, a 'get' arrow
    return [
        P("M 412 452 V 410 A 100 84 0 0 1 612 410 V 452", stroke="g", sw=40),
        P("M 356 432 H 668 Q 700 432 704 464 L 728 694 Q 732 742 684 742 H 340 Q 292 742 296 694 "
          "L 320 464 Q 324 432 356 432 Z", fill="g"),
        Circ(412, 482, 18, fill="p", detail=True, shadow=False),
        Circ(612, 482, 18, fill="p", detail=True, shadow=False),
        P("M 512 522 V 666 M 456 614 L 512 670 L 568 614", stroke="p", sw=46, shadow=False),
    ]


def s_store():
    return [
        P("M 362 432 V 382 A 150 120 0 0 1 662 382 V 432", stroke="g", sw=64),
        P("M 300 412 H 724 L 772 782 H 252 Z", fill="g", stroke="g", sw=40),
        P("M 512 492 V 682 M 432 612 L 512 692 L 592 612", stroke="p", sw=80, shadow=False),
    ]


def _hexpt(deg, r=232):
    return polar(C, C, r, deg)


def g_winrun():
    top, v60, v120, v180, v240, v300 = (_hexpt(a) for a in (0, 60, 120, 180, 240, 300))
    return [
        P(L(top, v60, (C, C), v300) + " Z", fill="g", op=0.4, shadow=False),
        P(L(top, v60, v120, v180, v240, v300) + " Z", stroke="g", sw=52),
        P(L(v60, (C, C), v300) + " M 512 512 L %s %s" % (fmt(v180[0]), fmt(v180[1])), stroke="g", sw=52),
    ]


def g_clock():
    out = [Circ(C, C, 232, stroke="g", sw=38)]
    for a in (0, 90, 180, 270):
        x0, y0 = polar(C, C, 176, a)
        x1, y1 = polar(C, C, 200, a)
        out.append(P(L((x0, y0), (x1, y1)), stroke="g", sw=28, op=0.75, detail=True))
    hx, hy = polar(C, C, 118, 300)
    mx, my = polar(C, C, 172, 60)
    out.append(P(L((hx, hy), (C, C), (mx, my)), stroke="g", sw=50))
    sx, sy = polar(C, C, 186, 210)
    out.append(P(L((C, C), (sx, sy)), stroke="a", sw=18, detail=True, shadow=False))
    out.append(Circ(C, C, 32, fill="a", shadow=False))
    return out


def s_clock():
    hx, hy = polar(C, C, 150, 300)
    mx, my = polar(C, C, 200, 60)
    return [Circ(C, C, 262, stroke="g", sw=80),
            P(L((hx, hy), (C, C), (mx, my)), stroke="g", sw=80)]


def g_widgets():
    return [
        Rect(302, 302, 196, 196, 50, fill="g"),
        Circ(624, 400, 98, fill="g", op=0.75),
        Rect(302, 540, 420, 182, 50, fill="g", op=0.92),
        P("M 362 631 H 542", stroke="p", sw=36, detail=True, shadow=False),
    ]


def g_optimizer():
    cy = 566
    return [
        P(arc_d(C, cy, 224, 240, 480), stroke="g", sw=64, op=0.4, shadow=False),
        P(arc_d(C, cy, 224, 240, 400), stroke="g", sw=64),
        P(L((C, cy), polar(C, cy, 168, 40)), stroke="g", sw=48),
        Circ(C, cy, 50, fill="g"),
    ]


def g_imagewriter():
    return [
        Rect(442, 272, 140, 140, 22, fill="g", op=0.7),
        Rect(470, 312, 30, 36, 6, fill="p", detail=True, shadow=False),
        Rect(524, 312, 30, 36, 6, fill="p", detail=True, shadow=False),
        Rect(392, 392, 240, 360, 58, fill="g"),
        P("M 512 482 V 636 M 456 584 L 512 640 L 568 584", stroke="p", sw=44, shadow=False),
    ]


def g_automator():
    star = "M 672 462 C 676 500 684 508 722 512 C 684 516 676 524 672 562 C 668 524 660 516 622 512 C 660 508 668 500 672 462 Z"
    return [
        P("M 362 362 C 560 362 672 362 672 512 C 672 662 560 662 362 662", stroke="g", sw=44, op=0.7),
        Circ(362, 362, 66, fill="g"),
        Rect(604, 444, 136, 136, 40, fill="g"),
        P(star, fill="p", detail=True, shadow=False),
        Circ(362, 662, 66, fill="g"),
    ]


def g_mail():
    # an envelope with the letter half out of it
    return [
        Rect(352, 282, 320, 300, 30, fill="g", op=0.88),
        P("M 412 362 H 612 M 412 432 H 562", stroke="p", sw=30, op=0.5, detail=True, shadow=False),
        P("M 282 452 L 512 596 L 742 452 V 682 Q 742 732 692 732 H 332 Q 282 732 282 682 Z", fill="g"),
        P("M 300 716 L 452 600 M 724 716 L 572 600", stroke="p", sw=22, op=0.3, detail=True, shadow=False),
        Circ(722, 322, 74, fill="p", detail=True, shadow=False),
        Circ(722, 322, 54, fill="a", detail=True),
    ]


def s_mail():
    return [
        Rect(342, 232, 340, 300, 30, fill="g", op=0.8),
        P("M 252 432 L 512 602 L 772 432 V 732 Q 772 782 722 782 H 302 Q 252 782 252 732 Z", fill="g"),
    ]


def g_assistant():
    star = "M 512 362 C 524 444 532 460 622 472 C 532 484 524 500 512 582 C 500 500 492 484 402 472 C 492 460 500 444 512 362 Z"
    return [
        P("M 402 600 L 362 712 L 512 620 Z", fill="g", stroke="g", sw=24),
        Rect(282, 302, 460, 340, 130, fill="g"),
        P(star, fill="p", shadow=False),
        P("M 652 372 C 655 392 658 395 678 398 C 658 401 655 404 652 424 C 649 404 646 401 626 398 "
          "C 646 395 649 392 652 372 Z", fill="p", detail=True, shadow=False),
    ]


def g_photos():
    return [
        Circ(632, 392, 66, fill="g"),
        P(L((292, 682), (432, 482), (532, 606), (602, 530), (732, 682)) + " Z", fill="g", stroke="g", sw=44),
    ]


def g_reader():
    return [
        Rect(382, 282, 320, 420, 44, fill="g", op=0.5),
        Rect(322, 332, 340, 420, 44, fill="g"),
        P("M 552 332 V 482 L 592 450 L 632 482 V 332 Z", fill="p", shadow=False),
        P("M 382 552 H 602 M 382 622 H 602 M 382 692 H 522", stroke="p", sw=30, op=0.45, detail=True,
          shadow=False),
    ]


def s_reader():
    return [
        Rect(292, 262, 440, 520, 60, fill="g"),
        P("M 562 262 V 482 L 617 440 L 672 482 V 262 Z", fill="p", shadow=False),
    ]


def g_studio():
    return [
        Rect(292, 352, 300, 76, 38, fill="g"),
        Rect(412, 474, 320, 76, 38, fill="g", op=0.82),
        Rect(332, 596, 240, 76, 38, fill="g", op=0.64),
        P("M 542 322 V 712", stroke="a", sw=22),
        P("M 508 296 H 576 L 542 344 Z", fill="a", stroke="a", sw=12),
    ]


def g_spotlight():
    return [
        Circ(462, 462, 152, stroke="g", sw=64),
        P(arc_d(462, 462, 98, 280, 350), stroke="g", sw=26, op=0.6, detail=True, shadow=False),
        P("M 576 576 L 702 702", stroke="g", sw=86),
    ]


def g_updater():
    r = 214
    return [
        P(arc_d(C, C, r, 30, 150), stroke="g", sw=52),
        P(arrow_head_d(C, C, r, 150, 62), stroke="g", sw=52),
        P(arc_d(C, C, r, 210, 330), stroke="g", sw=52),
        P(arrow_head_d(C, C, r, 330, 62), stroke="g", sw=52),
        P("M 512 422 V 594 M 456 542 L 512 598 L 568 542", stroke="g", sw=48, detail=True),
    ]


def g_welcome():
    # the Lingmo ribbon loop (an alpha with a crossing tail), from the logo
    loop = ("M 742 680 C 650 640 600 600 560 540 C 520 470 510 352 418 346 C 330 340 292 420 300 500 "
            "C 308 590 390 652 470 642 C 550 632 600 560 640 500 C 668 458 700 428 742 414")
    return [P(loop, stroke="g", sw=72)]


LOOP = g_welcome()[0]["d"]


def start_here_svg(symbolic_color=None):
    """Lingmo's start-here (menus, 'about' pages): the logo mark, not WhiteSur's."""
    if symbolic_color:
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="232 232 560 560">'
                '<path d="%s" fill="none" stroke="%s" stroke-width="84" stroke-linecap="round"/></svg>\n'
                % (LOOP, symbolic_color))
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 1024 1024">'
            '<defs><linearGradient id="b" x1="0" y1="32" x2="0" y2="992" gradientUnits="userSpaceOnUse">'
            '<stop offset="0" stop-color="#3F95FF"/><stop offset="1" stop-color="#2160EE"/></linearGradient></defs>'
            '<circle cx="512" cy="512" r="480" fill="url(#b)"/>'
            '<path d="%s" fill="none" stroke="#FFFFFF" stroke-width="80" stroke-linecap="round"/></svg>\n' % LOOP)


def g_dock():
    return [
        Rect(282, 322, 460, 360, 52, stroke="g", sw=40),
        Rect(362, 568, 300, 64, 32, fill="a"),
        Circ(432, 600, 14, fill="#FFFFFF", detail=True, shadow=False),
        Circ(512, 600, 14, fill="#FFFFFF", detail=True, shadow=False),
        Circ(592, 600, 14, fill="#FFFFFF", detail=True, shadow=False),
    ]


LAUNCHER_DOTS = ["blue", "red", "green", "purple", "white", "orange", "magenta", "cyan", "yellow"]


def g_launcher():
    out = []
    for i, col in enumerate(LAUNCHER_DOTS):
        cx, cy = 362 + 150 * (i % 3), 362 + 150 * (i // 3)
        out.append(Circ(cx, cy, 54, fill=PALETTE[col]))
    return out


def s_launcher():
    out = []
    for i, col in enumerate(["blue", "red", "green", "orange"]):
        cx, cy = 382 + 260 * (i % 2), 382 + 260 * (i // 2)
        out.append(Circ(cx, cy, 104, fill=PALETTE[col]))
    return out


def g_statusbar():
    return [
        Rect(282, 342, 460, 150, 75, fill="a"),
        Circ(667, 417, 55, fill="#FFFFFF", shadow=False),
        Rect(282, 532, 460, 150, 75, fill="#D3D9E3"),
        Circ(357, 607, 55, fill="#FFFFFF", shadow=False),
    ]


def g_installer():
    return [
        Rect(322, 312, 380, 280, 40, stroke="g", sw=44),
        P("M 262 672 H 762", stroke="g", sw=56),
        P("M 512 372 V 520 M 456 470 L 512 526 L 568 470", stroke="a", sw=44),
    ]


def g_shutdown():
    return [
        P(arc_d(C, 532, 186, 40, 320), stroke="a", sw=64),
        P("M 512 302 V 512", stroke="a", sw=64),
    ]


def g_notifications():
    return [
        P("M 512 292 C 412 292 372 372 372 462 V 562 L 322 632 H 702 L 652 562 V 462 C 652 372 612 292 512 292 Z",
          fill="g", stroke="g", sw=28),
        Circ(C, 702, 50, fill="g"),
        Circ(672, 332, 64, fill="p", detail=True, shadow=False),
        Circ(672, 332, 44, fill="a", detail=True),
    ]


def g_power():
    return [
        Rect(282, 382, 420, 260, 60, stroke="g", sw=48),
        Rect(724, 462, 42, 100, 18, fill="g"),
        P("M 522 412 L 422 530 H 502 L 472 612 L 582 494 H 502 Z", fill="g", stroke="g", sw=16),
    ]


def s_power():
    return [
        Rect(222, 342, 520, 340, 80, stroke="g", sw=64),
        Rect(762, 452, 56, 120, 20, fill="g"),
        P("M 542 362 L 392 540 H 502 L 462 662 L 632 474 H 522 Z", fill="g", stroke="g", sw=24),
    ]


def s_automator():
    return [
        P("M 322 322 C 600 322 712 322 712 512 C 712 702 600 702 322 702", stroke="g", sw=70, op=0.7),
        Circ(322, 322, 100, fill="g"),
        Rect(612, 412, 200, 200, 56, fill="g"),
        Circ(322, 702, 100, fill="g"),
    ]


def g_clipboard():
    return [
        Rect(322, 312, 380, 440, 56, fill="g"),
        Rect(422, 272, 180, 96, 36, fill="a"),
        P("M 392 482 H 632 M 392 562 H 632 M 392 642 H 532", stroke="p", sw=34, op=0.6, detail=True,
          shadow=False),
    ]


def g_auth():
    return [
        P("M 512 282 L 712 350 V 512 C 712 632 622 712 512 752 C 402 712 312 632 312 512 V 350 Z",
          fill="g", stroke="g", sw=20),
        P(L((432, 512), (494, 574), (606, 454)), stroke="p", sw=56, shadow=False),
    ]


def g_help():
    return [
        P("M 412 432 C 412 362 462 316 522 316 C 588 316 622 362 622 414 C 622 476 572 496 540 520 "
          "C 516 538 512 552 512 590", stroke="a", sw=68),
        Circ(C, 690, 44, fill="a"),
    ]


APPS = [
    App("lingmo-filemanager", "Arquivos", "Lingmo blue folder: the back panel at 60 % white and a solid front panel; "
        "the first icon in the dock, so it carries the brand colour.", "blue", g_files),
    App("lingmo-settings", "Configurações", "Graphite plate with three sliders whose knobs sit at different stops: "
        "'tune your system' without the usual gear.", "slate", g_settings, s_settings),
    App("lingmo-terminal", "Terminal", "Charcoal plate, mint prompt chevron and a white cursor bar.", "charcoal",
        g_terminal, s_terminal, accent="#5BE3A6"),
    App("lingmo-texteditor", "Editor de Texto", "Paper plate with ink text lines and an orange pencil writing the "
        "last line.", "paper", g_texteditor, s_texteditor, paper=True, accent="orange", accent2="#FF7A8A"),
    App("lingmo-notes", "Notas", "Sunny yellow plate with a white sheet whose corner folds over.", "yellow",
        g_notes, accent="#FFE7A3", accent2="#F0B11E"),
    App("lingmo-calculator", "Calculadora", "Orange plate with the four operators; the equals sign is tinted.",
        "orange", g_calculator, s_calculator, accent="#FFE2BC"),
    App("lingmo-videoplayer", "Vídeos", "Coral-red plate, rounded play triangle inside a faint ring.", "red",
        g_video, s_video),
    App("lingmo-camera", "Câmera", "Teal plate with a webcam: a round lens on a small stand.", "teal",
        g_camera, accent="#0A3E45"),
    App("lingmo-screenshot", "Captura de Tela", "Violet plate, four viewfinder corners around a shutter dot.",
        "violet", g_screenshot, s_screenshot),
    App("lingmo-screenlocker", "Bloqueio de Tela", "Indigo plate with a padlock.", "indigo", g_lock, s_lock),
    App("lingmo-store", "Loja", "Sky to brand blue plate with a shopping bag and a 'get' arrow.", "sky",
        g_store, s_store, gradient=("#3FC1FF", "#2470F5")),
    App("lingmo-winrun", "Instalar Programas", "Green-teal plate, an isometric box: foreign programs run sealed "
        "inside a box.", "mint", g_winrun),
    App("lingmo-clock", "Relógio", "Night-blue plate with a dial at ten past ten and an orange second hand.",
        "navy", g_clock, s_clock, accent="orange"),
    App("lingmo-widgets", "Widgets", "Magenta plate with three tiles of different shapes.", "magenta", g_widgets),
    App("lingmo-optimizer", "Otimizador", "Green plate with a gauge whose needle is past the middle.", "green",
        g_optimizer),
    App("lingmo-imagewriter", "Criador de Mídia USB", "Steel-blue plate with a USB stick and a write arrow.",
        "steel", g_imagewriter),
    App("lingmo-automator", "Automatizador", "Coral plate, a flow linking two round steps through a square "
        "action node.", "coral", g_automator, s_automator),
    App("lingmo-mail", "E-mail", "Sky plate, an envelope with the letter half out and a coral unread dot.", "sky", g_mail,
        s_mail, accent="red", hicolor=False),
    App("lingmo-assistant", "Assistente", "Purple-to-blue aurora plate, a speech bubble with a spark cut out.",
        "purple", g_assistant, gradient=("#9B7BFF", "#4A63F0")),
    App("lingmo-photos", "Fotos", "Sunset plate (coral to rose) with hills and a sun.", "coral", g_photos,
        gradient=("#FFA24D", "#F2456E")),
    App("lingmo-reader", "Documentos", "Deep crimson plate, two stacked pages and a bookmark ribbon.", "crimson",
        g_reader, s_reader),
    App("lingmo-studio", "Lingmo Studio", "Plum plate, three timeline clips and a coral playhead.", "plum",
        g_studio, accent="#FF8A65", hicolor=False),
    App("lingmo-spotlight", "Busca", "Cyan plate with a magnifier.", "cyan", g_spotlight),
    App("lingmo-updater", "Atualizações", "Mint plate, two arrows turning round a download arrow.", "#16B37C",
        g_updater, gradient=("#2BD39A", "#0E9C78")),
    App("lingmo-welcome", "Boas-vindas", "The Lingmo ribbon loop from the logo on a brand blue to indigo plate.",
        "brand", g_welcome, gradient=("#3F95FF", "#4E46F0"), hicolor=False),
    App("lingmo-dock", "Dock", "Paper plate, a screen with a blue dock bar.", "paper", g_dock, paper=True,
        accent="brand"),
    App("lingmo-launcher", "Aplicativos", "Dark plate with a 3x3 grid of dots in the Lingmo accent colours.",
        "nightblue", g_launcher, s_launcher, gradient=("#3A4866", "#1C2438")),
    App("lingmo-statusbar", "Central de Controle", "Paper plate with two toggles, one on (blue) and one off.",
        "paper", g_statusbar, paper=True, accent="brand"),
    App("lingmo-installer", "Instalar o Sistema", "Slate-blue plate, a laptop with a blue install arrow.",
        "nightblue", g_installer, accent="#7FB2FF", gradient=("#41557F", "#24314D")),
    App("lingmo-shutdown", "Desligar", "Paper plate with a red power symbol.", "paper", g_shutdown, paper=True,
        accent="#F0435A"),
    App("lingmo-notifications", "Notificações", "Rose plate, a bell with a yellow badge.", "rose",
        g_notifications, accent="yellow"),
    App("lingmo-power", "Energia", "Lime plate, a battery with a charge bolt.", "lime", g_power, s_power),
    App("lingmo-clipboard", "Área de Transferência", "Sand plate, a clipboard with its clip.", "sand",
        g_clipboard, accent="#7A5230"),
    App("lingmo-auth", "Autenticação", "Deep-sea plate, a shield with a check mark cut out.", "deepsea", g_auth),
    App("lingmo-help", "Ajuda", "Paper plate with a brand-blue question mark.", "paper", g_help, paper=True,
        accent="brand"),
]


# --------------------------------------------------------------------------
# SVG assembly
# --------------------------------------------------------------------------
class Svg:
    def __init__(self):
        self.defs, self.body = [], []

    def text(self):
        size = DESIGN["canvas"]
        return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">\n'
                "<defs>\n%s\n</defs>\n%s\n</svg>\n" % (size, size, size, size, "\n".join(self.defs),
                                                       "\n".join(self.body)))


def linear(id_, stops, y0, y1):
    s = "".join('<stop offset="%s" stop-color="%s" stop-opacity="%s"/>' % (fmt(o), c, fmt(a) if a < 1 else "1")
                for o, c, a in stops)
    return ('<linearGradient id="%s" gradientUnits="userSpaceOnUse" x1="0" y1="%s" x2="0" y2="%s">%s'
            "</linearGradient>" % (id_, fmt(y0), fmt(y1), s))


def plate_colors(app):
    if app.gradient:
        return app.gradient
    if app.paper:
        return PAPER_GRADIENT
    g = DESIGN
    return (shade(app.plate, g["grad_lightness"], -g["grad_hue_turn"]),
            shade(app.plate, -g["grad_lightness"], g["grad_hue_turn"]))


def plate_layers(svg, top, bottom, small=False, paper=False, shadow=True):
    """Shadow, plate, and the light overlays (returned separately to go over the glyph)."""
    g = DESIGN
    inset = g["plate_inset_small"] if small else g["plate_inset"]
    half = C - inset
    plate = squircle_d(C, C, half)
    svg.defs.append(linear("plate", [(0, top, 1), (1, bottom, 1)], inset, g["canvas"] - inset))

    if shadow and not small:
        n = g["shadow_layers"]
        a = 1 - (1 - g["shadow_alpha"]) ** (1 / n)   # n layers stack to shadow_alpha
        for i in range(n, 0, -1):
            e = g["shadow_spread"] * i / n
            dy = g["shadow_offset"] * i / n
            svg.body.append('<path d="%s" fill="#000000" opacity="%s"/>'
                            % (squircle_d(C, C + dy, half + e), fmt(a) if a >= 0.05 else "%.3f" % a))
        svg.body.append('<path d="%s" fill="#000000" opacity="%s"/>'
                        % (squircle_d(C, C + 4, half + 1), fmt(g["contact_alpha"])))
    elif shadow:
        svg.body.append('<path d="%s" fill="#000000" opacity="0.18"/>' % squircle_d(C, C + 8, half + 2))
    svg.body.append('<path d="%s" fill="url(#plate)"/>' % plate)

    # overlays, drawn after the glyph
    over = []
    sx, sy = g["sheen_center"]
    svg.defs.append('<radialGradient id="sheen" gradientUnits="userSpaceOnUse" cx="%s" cy="%s" r="%s">'
                    '<stop offset="0" stop-color="#FFFFFF" stop-opacity="%s"/>'
                    '<stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></radialGradient>'
                    % (fmt(inset + sx * 2 * half), fmt(inset + sy * 2 * half), fmt(g["sheen_radius"] * 2 * half),
                       fmt(g["sheen_alpha"] * (0.6 if paper else 1))))
    over.append('<path d="%s" fill="url(#sheen)"/>' % plate)
    y0, y1 = inset, g["canvas"] - inset
    svg.defs.append(linear("hl", [(0, "#FFFFFF", g["highlight_alpha"]), (g["highlight_stop"], "#FFFFFF", 0),
                                  (0.62, "#000000", 0), (1, "#000000", g["base_shade_alpha"])], y0, y1))
    over.append('<path d="%s" fill="url(#hl)"/>' % plate)
    if not small:
        ew = g["edge_width"]
        svg.defs.append(linear("edge", [(0, "#FFFFFF", g["edge_top_alpha"]), (0.35, "#FFFFFF", 0.04),
                                        (0.75, "#000000", 0.02), (1, "#000000", g["edge_bottom_alpha"])], y0, y1))
        over.append('<path d="%s" fill="none" stroke="url(#edge)" stroke-width="%s"/>'
                    % (squircle_d(C, C, half - ew / 2), fmt(ew)))
    if paper:
        over.append('<path d="%s" fill="none" stroke="#000000" stroke-opacity="%s" stroke-width="3"/>'
                    % (squircle_d(C, C, half - 1.5), fmt(g["light_border_alpha"])))
    return over


def element_svg(e, paint, scale=1.0, opacity=None):
    def res(p):
        return "none" if p is None else paint(p)
    sw = (e["sw"] or W) * scale
    attrs = ['fill="%s"' % res(e["fill"])]
    if e["stroke"]:
        attrs.append('stroke="%s" stroke-width="%s" stroke-linecap="%s" stroke-linejoin="%s"'
                     % (res(e["stroke"]), fmt(sw), e["cap"], e["join"]))
    op = e["op"] if opacity is None else opacity * e["op"]
    if op < 1:
        attrs.append('opacity="%s"' % fmt(op) if op >= 0.05 else 'opacity="%.3f"' % op)
    a = " ".join(attrs)
    if e["tag"] == "circle":
        return '<circle cx="%s" cy="%s" r="%s" %s/>' % (fmt(e["cx"]), fmt(e["cy"]), fmt(e["r"]), a)
    if e["tag"] == "rect":
        return '<rect x="%s" y="%s" width="%s" height="%s" rx="%s" %s/>' % (
            fmt(e["x"]), fmt(e["y"]), fmt(e["w"]), fmt(e["h"]), fmt(e["rx"]), a)
    return '<path d="%s" %s/>' % (e["d"], a)


def app_svg(app, small=False):
    g = DESIGN
    svg = Svg()
    top, bottom = plate_colors(app)
    over = plate_layers(svg, top, bottom, small=small, paper=app.paper)

    if small:
        elems = app.small() if app.small else [dict(e) for e in app.glyph() if not e["detail"]]
        scale = 1.0 if app.small else g["glyph_small_scale"]
    else:
        elems, scale = app.glyph(), 1.0

    palette = {"g": app.ink, "p": "url(#plate)", "a": app.accent or app.ink, "b": app.accent2 or app.ink}

    def paint(p):
        return palette.get(p, p)

    # glyph shadow: same shapes, one dark tint, dropped a little
    if not small:
        tint = mix(bottom, "#000000", 0.55) if not app.paper else "#5A6478"
        alpha = g["glyph_shadow_alpha"] * (0.5 if app.paper else 1)
        shadow = [element_svg(e, lambda p: tint) for e in elems if e["shadow"] and e["fill"] != "p"
                  and e["stroke"] != "p"]
        if shadow:
            svg.body.append('<g transform="translate(0 %s)" opacity="%s">%s</g>'
                            % (fmt(g["glyph_shadow_offset"]), fmt(alpha), "".join(shadow)))
    if small:
        # small glyphs grow around the centre to use the bigger plate
        k = (C - g["plate_inset_small"]) / (C - g["plate_inset"])
        svg.body.append('<g transform="translate(%s %s) scale(%s)">' % (fmt(C - C * k), fmt(C - C * k), "%.4f" % k))
    for e in elems:
        svg.body.append(element_svg(e, paint, scale))
    if small:
        svg.body.append("</g>")
    svg.body.extend(over)
    return svg.text()


def neutral_plate_svg(small=False):
    """The plate third-party icons are put on (lingmo-icons-update)."""
    svg = Svg()
    over = plate_layers(svg, *PAPER_GRADIENT, small=small, paper=True)
    svg.body.extend(over)
    return svg.text()


# --------------------------------------------------------------------------
# Theme files
# --------------------------------------------------------------------------
def theme_dirs():
    g = DESIGN
    dirs = []
    for s in g["small_sizes"] + g["sizes"]:
        dirs.append(("apps/%d" % s, "Size=%d\nContext=Applications\nType=Fixed" % s))
    for s in g["hidpi_sizes"]:
        dirs.append(("apps@2x/%d" % s, "Size=%d\nScale=2\nContext=Applications\nType=Fixed" % s))
    dirs.append(("apps/scalable", "Size=128\nMinSize=32\nMaxSize=1024\nContext=Applications\nType=Scalable"))
    dirs.append(("places/scalable", "Size=64\nMinSize=8\nMaxSize=512\nContext=Places\nType=Scalable"))
    dirs.append(("places/symbolic", "Size=16\nMinSize=8\nMaxSize=512\nContext=Places\nType=Scalable"))
    return dirs


def index_theme(name, comment, inherits, dirs):
    head = ("[Icon Theme]\nName=%s\nComment=%s\nInherits=%s\nExample=lingmo-filemanager\n"
            "DisplayDepth=32\nDesktopDefault=48\nDesktopSizes=16,22,32,48,64,96,128,256\n"
            "PanelDefault=48\nPanelSizes=16,22,32,48,64,96,128,256\nDialogDefault=32\n"
            "DialogSizes=16,22,32,48,64,128,256\nFollowsColorScheme=false\n\nDirectories=%s\n"
            % (name, comment, inherits, ",".join(d for d, _ in dirs)))
    return head + "".join("\n[%s]\n%s\n" % (d, body) for d, body in dirs)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def find_renderer():
    if shutil.which("rsvg-convert"):
        return lambda src, dst, px: subprocess.run(
            ["rsvg-convert", "-w", str(px), "-h", str(px), "-o", dst, src], check=True)
    if shutil.which("inkscape"):
        return lambda src, dst, px: subprocess.run(
            ["inkscape", src, "--export-type=png", "--export-filename=" + dst, "-w", str(px), "-h", str(px)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--only", nargs="*", help="only these icon names")
    ap.add_argument("--no-png", action="store_true", help="SVGs only")
    ap.add_argument("--optimize", action="store_true", help="run optipng on the PNGs")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    icons = os.path.join(out, "icons")
    theme = os.path.join(icons, "Lingmo")
    apps = [a for a in APPS if not args.only or a.name in args.only]
    g = DESIGN

    render = None if args.no_png else find_renderer()
    if not args.no_png and not render:
        print("warning: neither rsvg-convert nor inkscape found, writing SVGs only", file=sys.stderr)

    tmp = tempfile.mkdtemp(prefix="lingmo-icons-")
    jobs = []   # (svg, png, px)
    for a in apps:
        full = app_svg(a)
        write(os.path.join(theme, "apps/scalable", a.name + ".svg"), full)
        small_path = os.path.join(tmp, a.name + "-small.svg")
        write(small_path, app_svg(a, small=True))
        full_path = os.path.join(theme, "apps/scalable", a.name + ".svg")
        for s in g["small_sizes"]:
            jobs.append((small_path, os.path.join(theme, "apps/%d" % s, a.name + ".png"), s))
        for s in g["sizes"]:
            jobs.append((full_path, os.path.join(theme, "apps/%d" % s, a.name + ".png"), s))
        for s in g["hidpi_sizes"]:
            src = small_path if s in g["small_sizes"] else full_path
            jobs.append((src, os.path.join(theme, "apps@2x/%d" % s, a.name + ".png"), 2 * s))
        if a.hicolor:
            write(os.path.join(icons, "hicolor/scalable/apps", a.name + ".svg"), full)
            for s in g["small_sizes"]:
                jobs.append((small_path, os.path.join(icons, "hicolor/%dx%d/apps" % (s, s), a.name + ".png"), s))

    # neutral plate for third-party icons
    plates = os.path.join(out, "lingmo-icons")
    write(os.path.join(plates, "plate.svg"), neutral_plate_svg())
    plate_px = sorted({s for s in g["sizes"]} | {2 * s for s in g["hidpi_sizes"] if s not in g["small_sizes"]})
    for px in plate_px:
        jobs.append((os.path.join(plates, "plate.svg"), os.path.join(plates, "plate-%d.png" % px), px))
    write(os.path.join(plates, "plate.json"), json.dumps({
        "canvas": g["canvas"], "plate_inset": g["plate_inset"], "content": g["plate_content"],
        "sizes": g["sizes"], "small_sizes": g["small_sizes"],
        "hidpi_sizes": [s for s in g["hidpi_sizes"]], "plate_px": plate_px,
    }, indent=2) + "\n")

    if render:
        for _, dst, _ in jobs:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        with ThreadPoolExecutor(args.jobs) as pool:
            list(pool.map(lambda j: render(*j), jobs))
        if args.optimize and shutil.which("optipng"):
            pngs = [dst for _, dst, _ in jobs]
            with ThreadPoolExecutor(args.jobs) as pool:
                list(pool.map(lambda p: subprocess.run(["optipng", "-quiet", "-o1", "-strip", "all", p],
                                                       check=False), pngs))
    shutil.rmtree(tmp, ignore_errors=True)

    if not args.only:
        dirs = theme_dirs()
        if not render:
            dirs = [d for d in dirs if not d[0][-1].isdigit()]
        write(os.path.join(theme, "places/scalable/start-here.svg"), start_here_svg())
        write(os.path.join(theme, "places/symbolic/start-here-symbolic.svg"), start_here_svg("#363636"))
        write(os.path.join(theme, "index.theme"), index_theme(
            "Lingmo", "Lingmo app icons on fluid plates, WhiteSur for the rest",
            "Lingmo-generated,WhiteSur-light,WhiteSur,hicolor", dirs))
        # Lingmo-dark: same app icons, dark-mode symbolic icons from WhiteSur-dark
        dark = os.path.join(icons, "Lingmo-dark")
        os.makedirs(dark, exist_ok=True)
        for link in ("apps", "apps@2x"):
            p = os.path.join(dark, link)
            if os.path.lexists(p):
                os.remove(p) if os.path.islink(p) else shutil.rmtree(p)
            if os.path.isdir(os.path.join(theme, link)):
                os.symlink("../Lingmo/" + link, p)
        write(os.path.join(dark, "places/scalable/start-here.svg"), start_here_svg())
        write(os.path.join(dark, "places/symbolic/start-here-symbolic.svg"), start_here_svg("#DEDEDE"))
        write(os.path.join(dark, "index.theme"), index_theme(
            "Lingmo-dark", "Lingmo app icons on fluid plates, WhiteSur dark for the rest",
            "Lingmo-generated,WhiteSur-dark,WhiteSur,hicolor", dirs))
    print("%d icons -> %s%s" % (len(apps), theme, "" if render else " (SVG only)"))


if __name__ == "__main__":
    main()
