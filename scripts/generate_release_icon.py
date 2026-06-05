"""Generate the CVFR Route Master application icon assets.

Three outputs from a shared render pipeline (one big icon for the
window/app brand, one small toolbar glyph). All three are pure-Pillow
procedural renders so the artwork is reproducible from source and
tweakable in diffs:

  1. ``release/icon.ico`` — multi-resolution Windows .ico baked into
     the .exe by PyInstaller. Windows file explorer, the taskbar,
     Alt-Tab, and the title bar all pick the size that best matches
     the rendering context, so shipping every common size avoids
     the blurry-bilinear-rescale look that single-resolution icons
     get on hi-DPI monitors.

  2. ``cvfr_routemaster/resources/app_icon.png`` — single 256×256
     PNG bundled with the Python package. The running app loads
     this at startup and pushes it onto ``QApplication.setWindowIcon``
     so the title bar / taskbar / Alt-Tab show the same artwork as
     the .exe icon — including in the dev-mode ``py -m cvfr_routemaster``
     workflow where there's no .exe to host the .ico.

  3. ``cvfr_routemaster/resources/airplane_mode_icon.png`` — a small
     256×256 PNG containing only the classic "phone airplane mode"
     glyph (white tilted-airplane silhouette on transparent
     background). The MainWindow toolbar uses this on its
     airplane-mode toggle button so the action reads at a glance.
     Same procedural strategy as the app icon — defined alongside it
     so the two never drift apart visually.

The main app icon is a stylised compass rose with a red route
line, a top-down Cessna-172 silhouette following the route, and
yellow waypoint dots. The airplane-mode glyph is a stand-alone
silhouette using the SAME ``_draw_cessna_172`` helper so the
"airplane" the user sees on the toggle is the same C172 they see
flying the route in the main icon — a small cross-icon
consistency cue.

Run from the repo root::

    python scripts/generate_release_icon.py

Outputs:
  * ``release/icon.ico`` (multi-image, transparent background)
  * ``cvfr_routemaster/resources/app_icon.png`` (256×256, RGBA)
  * ``cvfr_routemaster/resources/airplane_mode_icon.png`` (256×256, RGBA)
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw


# Canonical Windows icon sizes — Explorer, taskbar, Alt-Tab and high-DPI
# title bars all pick the closest match, so shipping every common size
# avoids the blurry-bilinear-rescale look that single-size .ico files get
# on hi-DPI displays. 256 is the modern standard; 16/24/32/48 cover legacy
# title bar / file explorer; 64/128 cover mid-DPI.
_ICON_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)

# Size of the runtime PNG bundled with the Python package. 256 is the
# largest of the .ico sub-sizes and what Qt picks on hi-DPI title bars
# and Alt-Tab thumbnails; bundling the largest available image lets Qt
# down-sample as needed without ever needing to up-sample.
_RUNTIME_PNG_SIZE = 256

# Visual palette — pulled from the app's own dark-theme constants so the
# icon reads as part of the same brand (cyan compass + magenta route +
# bright white tick marks against a dark teal disc).
_DISC_COLOR: tuple[int, int, int, int] = (15, 35, 55, 255)        # dark teal
_DISC_RING_COLOR: tuple[int, int, int, int] = (90, 120, 150, 255)  # light teal
_TICK_COLOR: tuple[int, int, int, int] = (220, 230, 240, 255)     # near-white
_NEEDLE_NORTH_COLOR: tuple[int, int, int, int] = (0, 200, 220, 255)  # cyan (matches table CTR cyan)
_NEEDLE_SOUTH_COLOR: tuple[int, int, int, int] = (160, 175, 190, 255)  # muted grey
_ROUTE_COLOR: tuple[int, int, int, int] = (255, 80, 80, 255)      # bright red (matches override colour)
_ROUTE_DOT_COLOR: tuple[int, int, int, int] = (255, 200, 80, 255) # warm yellow waypoint dots

# Top-down C172 silhouette palette. The aircraft sits *on top of* the
# red route so it reads as "the plane following the planned route" —
# the central narrative of the app. The fuselage stays bright white so
# it pops against the route's red and the disc's dark teal both; the
# wing has a faint grey tint so it doesn't visually merge into a single
# tick mark at small sizes; the propeller arc is a translucent ring
# that suggests motion without dominating the silhouette.
_PLANE_FUSELAGE_COLOR: tuple[int, int, int, int] = (250, 250, 252, 255)
_PLANE_WING_COLOR: tuple[int, int, int, int] = (235, 240, 245, 255)
_PLANE_OUTLINE_COLOR: tuple[int, int, int, int] = (20, 30, 45, 255)
_PLANE_PROP_COLOR: tuple[int, int, int, int] = (230, 235, 245, 140)


def _draw_cessna_172(
    draw: ImageDraw.ImageDraw,
    *,
    cx: float,
    cy: float,
    length: float,
    heading_rad: float,
    outline_w: float,
) -> None:
    """Render a top-down Cessna-172 silhouette centred at
    ``(cx, cy)``, sized so the fuselage spans ``length`` pixels
    from nose to tail tip, pointing in the direction
    ``heading_rad`` (radians; 0 = +x, -π/2 = up — same convention
    as the compass-tick loop).

    The C172 is a high-wing single-engine four-seater; its
    top-down silhouette is recognisable from three landmarks:

      1. A long, narrow fuselage with a slightly rounded nose
         (the cowling housing the Lycoming O-360) and a fin-and-
         rudder tail.
      2. A pair of very wide, straight, constant-chord wings
         mounted ABOVE the fuselage (so in a top-down view they
         visually occlude the cabin area).
      3. A horizontal stabiliser pair near the tail, much shorter
         than the wings.
      4. A propeller disc at the nose (rendered translucent so
         it suggests rotation rather than a stalled prop).

    Pillow has no polygon-rotation primitive, so we compute each
    vertex's rotated position by hand. Coordinates are first
    expressed in a "plane-local" frame (x along nose-tail axis,
    y across the wingspan), then rotated to world space via the
    standard 2-D rotation matrix.

    Args:
        draw: Pillow drawing context.
        cx, cy: World-space centre of the aircraft (the point on
            the fuselage that the rotation pivots around — roughly
            the wing centre, which is where the C172's centre of
            gravity lives).
        length: Pixel length of the fuselage from nose to tail tip.
            Other dimensions scale off this value.
        heading_rad: Direction the nose points, in radians. Same
            convention as ``math.cos``/``sin`` (0 = +x, π/2 = +y).
        outline_w: Outline stroke width in pixels.
    """
    # ----- local-frame proportions (scaled off ``length``) --------------
    #
    # All numbers below are fractions of ``length`` so the silhouette
    # scales linearly with the icon size — important because we render
    # at 4× and downscale, so the wings need to stay readable at the
    # final 256-px output.
    half_len = length / 2
    nose_x = half_len            # +x is "forward" in local frame
    tail_x = -half_len
    # Cabin / fuselage half-width — narrow body typical of light singles.
    fus_hw = length * 0.075
    # Wings: ~67% of length on a real 172 (10.97 m wingspan vs 8.28 m
    # length). 0.66 is close enough at icon resolutions and keeps the
    # wing tip from clipping past the disc edge at 16-px renders.
    wing_hw = length * 0.55
    wing_chord_half = length * 0.080
    wing_centre_x = length * 0.04  # wings sit slightly forward of CG
    # Horizontal stabiliser: roughly 1/3 of the wingspan, sits near tail.
    stab_hw = length * 0.22
    stab_chord_half = length * 0.045
    stab_centre_x = tail_x + length * 0.10
    # Propeller disc — a thin translucent ellipse at the nose suggests
    # rotation without resorting to a stop-motion two-blade rendering
    # (which reads badly at icon sizes).
    prop_offset = length * 0.05
    prop_w = length * 0.015      # along nose-tail axis (very thin)
    prop_hw = length * 0.13      # perpendicular to nose-tail axis

    # ----- local → world rotation ---------------------------------------
    cos_h = math.cos(heading_rad)
    sin_h = math.sin(heading_rad)

    def to_world(lx: float, ly: float) -> tuple[float, float]:
        # Standard 2-D rotation, then translate by the aircraft centre.
        wx = cx + lx * cos_h - ly * sin_h
        wy = cy + lx * sin_h + ly * cos_h
        return wx, wy

    # ----- fuselage (rounded rectangle drawn as a single polygon) ------
    #
    # Six vertices give the nose a hint of curvature without doing a
    # full ellipse: nose centre slightly forward, two shoulders at
    # ~85% of nose offset, then the cabin sides, then the tail point.
    fuselage = [
        to_world(nose_x, 0),
        to_world(nose_x - length * 0.04, fus_hw * 0.6),
        to_world(length * 0.15, fus_hw),
        to_world(tail_x + length * 0.03, fus_hw * 0.55),
        to_world(tail_x, 0),
        to_world(tail_x + length * 0.03, -fus_hw * 0.55),
        to_world(length * 0.15, -fus_hw),
        to_world(nose_x - length * 0.04, -fus_hw * 0.6),
    ]

    # ----- propeller disc (translucent ellipse at nose) -----------------
    #
    # Pillow draws ellipses axis-aligned; we approximate a rotated
    # ellipse with a thin oriented polygon (8-pt). At icon sizes the
    # subtle rendering loss vs. a true ellipse is invisible.
    prop_cx = nose_x + prop_offset
    prop_segments = 16
    prop_pts: list[tuple[float, float]] = []
    for i in range(prop_segments):
        theta = 2 * math.pi * i / prop_segments
        lx = prop_cx + prop_w * math.cos(theta)
        ly = prop_hw * math.sin(theta)
        prop_pts.append(to_world(lx, ly))
    draw.polygon(prop_pts, fill=_PLANE_PROP_COLOR)

    # ----- main wings (single high-aspect-ratio rectangle) -------------
    wing = [
        to_world(wing_centre_x - wing_chord_half, -wing_hw),
        to_world(wing_centre_x + wing_chord_half, -wing_hw),
        to_world(wing_centre_x + wing_chord_half * 0.65, wing_hw * 0),  # subtle taper
        to_world(wing_centre_x + wing_chord_half, wing_hw),
        to_world(wing_centre_x - wing_chord_half, wing_hw),
        to_world(wing_centre_x - wing_chord_half * 0.65, wing_hw * 0),
    ]
    # Wings drawn first so the fuselage sits ON TOP — that's the
    # correct top-down occlusion for a high-wing aircraft (the wing
    # root attaches above the cabin, so in a strict top-down view the
    # wing is what you see most prominently, with the fuselage poking
    # out the front and back).
    draw.polygon(
        wing,
        fill=_PLANE_WING_COLOR,
        outline=_PLANE_OUTLINE_COLOR,
        width=max(1, int(outline_w)),
    )

    # ----- horizontal stabiliser ---------------------------------------
    stab = [
        to_world(stab_centre_x - stab_chord_half, -stab_hw),
        to_world(stab_centre_x + stab_chord_half, -stab_hw),
        to_world(stab_centre_x + stab_chord_half, stab_hw),
        to_world(stab_centre_x - stab_chord_half, stab_hw),
    ]
    draw.polygon(
        stab,
        fill=_PLANE_WING_COLOR,
        outline=_PLANE_OUTLINE_COLOR,
        width=max(1, int(outline_w)),
    )

    # ----- vertical fin tail (small triangle near the tail) ------------
    # A thin triangle above the rear of the fuselage gives the C172
    # silhouette its instantly-recognisable tail profile.
    fin = [
        to_world(tail_x + length * 0.05, fus_hw * 0.4),
        to_world(tail_x + length * 0.12, fus_hw * 0.05),
        to_world(tail_x - length * 0.01, fus_hw * 0.05),
    ]
    draw.polygon(
        fin,
        fill=_PLANE_WING_COLOR,
        outline=_PLANE_OUTLINE_COLOR,
        width=max(1, int(outline_w)),
    )

    # ----- fuselage on top of wings ------------------------------------
    draw.polygon(
        fuselage,
        fill=_PLANE_FUSELAGE_COLOR,
        outline=_PLANE_OUTLINE_COLOR,
        width=max(1, int(outline_w)),
    )


# ---------------------------------------------------------------------------
# Airplane-mode glyph palette + renderer.
# ---------------------------------------------------------------------------
#
# The toolbar toggle is a small button — usually rendered at 16, 20,
# or 24 px depending on the user's DPI scale and the platform's
# default toolbar size — so the glyph has to read as "airplane" even
# when individual pixels are barely a wing-chord wide. Two design
# choices fall out of that constraint:
#
#  1. Solid white silhouette, no outline. At icon sizes an outline
#     adds noise (1-px outlines anti-alias to grey halos that wash
#     out the silhouette against the dark toolbar). The toolbar
#     background is dark teal so white reads with high contrast and
#     stays legible in both pressed and unpressed states (Qt darkens
#     the icon slightly when the button is in the "checked" state,
#     but a pure-white starting point degrades gracefully).
#
#  2. We re-use ``_draw_cessna_172``. It's already tuned for icon-
#     scale rendering (the 4× supersample below + the LANCZOS
#     downscale below smooth out the wing edges), and it gives the
#     airplane mode toggle the SAME silhouette as the C172 flying
#     the route in the main app icon — a small visual cue that ties
#     the two icons together as parts of one app brand. Recolouring
#     all four "fuselage / wing / outline / prop" constants to the
#     same white means the multi-poly C172 reads as a single flat
#     silhouette (which is the airplane-mode look) without changing
#     any of the shape geometry.
_AIRPLANE_MODE_FILL: tuple[int, int, int, int] = (250, 250, 252, 255)


def _render_airplane_mode_icon(size: int) -> Image.Image:
    """Render the airplane-mode toggle icon at ``size``×``size``.

    Transparent background + a single white tilted-airplane
    silhouette centred in the canvas. Heading is -3π/8 (≈ 67.5°
    above the +x axis) so the nose points to the upper-right at
    the same angle as the iOS / Android / Windows airplane-mode
    glyph — instantly recognisable to anyone who's used a
    smartphone in the last decade.

    Args:
        size: Target square pixel size (e.g. 256).

    Returns:
        RGBA Pillow image with the glyph drawn in white on a fully
        transparent background.
    """
    # 4× supersample then LANCZOS-downscale — same trick the main
    # app icon uses, gives clean wing edges at the toolbar's
    # typical 16/20/24 px display size.
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = cy = s / 2
    # ``length`` is the nose-to-tail span. 0.78 of the canvas leaves
    # a small margin so the wing tips don't kiss the icon edge after
    # rotation — at a typical toolbar render size that margin
    # collapses to ~1 px which is enough to keep the wings clear of
    # the button's border without making the silhouette look small.
    length = s * 0.78
    heading = -math.radians(67.5)

    # Recolour the four C172 palette knobs to a single flat white
    # for the duration of this render, then restore. The function
    # uses module-level constants for its colour scheme — flipping
    # them in-place is the lightest-touch way to repurpose the
    # silhouette as a flat icon glyph without forking the helper.
    global _PLANE_FUSELAGE_COLOR, _PLANE_WING_COLOR, _PLANE_OUTLINE_COLOR, _PLANE_PROP_COLOR
    saved = (
        _PLANE_FUSELAGE_COLOR,
        _PLANE_WING_COLOR,
        _PLANE_OUTLINE_COLOR,
        _PLANE_PROP_COLOR,
    )
    try:
        _PLANE_FUSELAGE_COLOR = _AIRPLANE_MODE_FILL
        _PLANE_WING_COLOR = _AIRPLANE_MODE_FILL
        _PLANE_OUTLINE_COLOR = _AIRPLANE_MODE_FILL
        # Hide the propeller disc entirely — at icon scale the
        # translucent ring reads as "smudge near the nose", not as
        # rotation. Setting alpha=0 keeps the polygon call cheap
        # without forking the helper.
        _PLANE_PROP_COLOR = (0, 0, 0, 0)
        _draw_cessna_172(
            draw,
            cx=cx,
            cy=cy,
            length=length,
            heading_rad=heading,
            outline_w=max(1, s * 0.004),
        )
    finally:
        (
            _PLANE_FUSELAGE_COLOR,
            _PLANE_WING_COLOR,
            _PLANE_OUTLINE_COLOR,
            _PLANE_PROP_COLOR,
        ) = saved

    return img.resize((size, size), Image.Resampling.LANCZOS)


def _render_icon(size: int) -> Image.Image:
    """Render a single icon at the requested square pixel size.

    Strategy: render at 4× and downscale with ``LANCZOS``. Pillow's
    line + arc primitives anti-alias poorly at small target sizes
    (especially 16/24 px), and supersampling fixes the worst of it
    without resorting to per-size hand-tuning.
    """
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = cy = s / 2
    margin = s * 0.06
    radius = s / 2 - margin

    # 1. Disc + outer ring — gives the icon a solid silhouette against
    # any background colour (Explorer dark mode, taskbar dark, Alt-Tab
    # blur, etc.) and grounds the compass marks.
    draw.ellipse(
        [(cx - radius, cy - radius), (cx + radius, cy + radius)],
        fill=_DISC_COLOR,
        outline=_DISC_RING_COLOR,
        width=max(2, int(s * 0.012)),
    )

    # 2. Compass tick marks — 16 around the disc (cardinals + ordinals
    # + half-ordinals). North gets a longer tick so the orientation
    # is unambiguous even at 16 px where the needle is hard to see.
    tick_r_inner = radius * 0.86
    tick_r_outer_short = radius * 0.94
    tick_r_outer_long = radius * 0.98
    tick_w = max(1, int(s * 0.008))
    for i in range(16):
        angle = math.radians(i * (360 / 16) - 90)  # -90 so 0 is up
        outer = tick_r_outer_long if i == 0 else tick_r_outer_short
        x0 = cx + tick_r_inner * math.cos(angle)
        y0 = cy + tick_r_inner * math.sin(angle)
        x1 = cx + outer * math.cos(angle)
        y1 = cy + outer * math.sin(angle)
        draw.line([(x0, y0), (x1, y1)], fill=_TICK_COLOR, width=tick_w)

    # 3. Compass needle — two triangles (N cyan, S grey) sharing the
    # centre. Slightly off-vertical (~10°) so the icon pairs visually
    # with the route line above it without overlapping.
    needle_angle_deg = -10  # tilt slightly W of N
    needle_angle = math.radians(needle_angle_deg - 90)
    needle_back = math.radians(needle_angle_deg - 90 + 180)
    needle_len = radius * 0.62
    needle_half_w = radius * 0.10
    perp = needle_angle + math.pi / 2

    nx_tip = cx + needle_len * math.cos(needle_angle)
    ny_tip = cy + needle_len * math.sin(needle_angle)
    nx_l = cx + needle_half_w * math.cos(perp)
    ny_l = cy + needle_half_w * math.sin(perp)
    nx_r = cx - needle_half_w * math.cos(perp)
    ny_r = cy - needle_half_w * math.sin(perp)
    draw.polygon(
        [(nx_tip, ny_tip), (nx_l, ny_l), (nx_r, ny_r)],
        fill=_NEEDLE_NORTH_COLOR,
    )

    sx_tip = cx + needle_len * math.cos(needle_back)
    sy_tip = cy + needle_len * math.sin(needle_back)
    draw.polygon(
        [(sx_tip, sy_tip), (nx_l, ny_l), (nx_r, ny_r)],
        fill=_NEEDLE_SOUTH_COLOR,
    )

    # Centre hub — small cyan disc anchors the needle and reads as a
    # waypoint marker at any size.
    hub_r = radius * 0.10
    draw.ellipse(
        [(cx - hub_r, cy - hub_r), (cx + hub_r, cy + hub_r)],
        fill=_NEEDLE_NORTH_COLOR,
        outline=_TICK_COLOR,
        width=max(1, int(s * 0.005)),
    )

    # 4. Route polyline — three waypoints + two segments running across
    # the disc, sloping NE→SW so it doesn't fight the compass needle.
    # Bright red matches the app's override-cell colour, tying the icon
    # to the most distinctive on-screen UI element.
    route_w = max(2, int(s * 0.030))
    pts = [
        (cx - radius * 0.62, cy + radius * 0.50),
        (cx - radius * 0.05, cy - radius * 0.10),
        (cx + radius * 0.55, cy - radius * 0.45),
    ]
    draw.line(pts, fill=_ROUTE_COLOR, width=route_w, joint="curve")
    # Waypoint dots — yellow over a thin dark halo so they stay
    # distinct from the red route line at every resolution.
    dot_r = radius * 0.07
    halo_r = dot_r + max(1, int(s * 0.008))
    for px, py in pts:
        draw.ellipse(
            [(px - halo_r, py - halo_r), (px + halo_r, py + halo_r)],
            fill=(0, 0, 0, 255),
        )
        draw.ellipse(
            [(px - dot_r, py - dot_r), (px + dot_r, py + dot_r)],
            fill=_ROUTE_DOT_COLOR,
        )

    # 5. Cessna-172 silhouette riding the route. We place the aircraft
    # mid-way along the second (NE) segment so it visually reads as
    # "currently flying from the middle waypoint toward the
    # destination" — same narrative every flight-planning briefing
    # tells. The heading is the direction vector of that segment.
    #
    # 0.55 picks a point past the segment midpoint so the plane sits
    # closer to the third waypoint (destination) than the second; it
    # also keeps the wings clear of the centre-disc waypoint dot at
    # very small icon sizes (the dot would otherwise clip a wing tip
    # at 16 / 24 px).
    seg_t = 0.55
    seg_p0 = pts[1]
    seg_p1 = pts[2]
    plane_cx = seg_p0[0] + (seg_p1[0] - seg_p0[0]) * seg_t
    plane_cy = seg_p0[1] + (seg_p1[1] - seg_p0[1]) * seg_t
    heading = math.atan2(seg_p1[1] - seg_p0[1], seg_p1[0] - seg_p0[0])
    plane_len = radius * 0.78
    _draw_cessna_172(
        draw,
        cx=plane_cx,
        cy=plane_cy,
        length=plane_len,
        heading_rad=heading,
        outline_w=max(1, s * 0.004),
    )

    # Downscale to target.
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    out_dir = repo_root / "release"
    out_dir.mkdir(exist_ok=True)
    ico_path = out_dir / "icon.ico"
    # Runtime-bundled PNG: lives inside the package directory so the
    # spec file's ``datas`` clause (and ``importlib.resources``
    # fallbacks) can find it without a parallel ``release/`` path
    # query at app launch.
    runtime_png_dir = repo_root / "cvfr_routemaster" / "resources"
    runtime_png_dir.mkdir(parents=True, exist_ok=True)
    runtime_png_path = runtime_png_dir / "app_icon.png"

    images = [_render_icon(s) for s in _ICON_SIZES]
    # Pillow's .ico writer takes the largest image and a ``sizes=`` list
    # of (w, h) tuples it should pack into the file. Passing already-
    # rendered per-size images via ``append_images`` is the way to keep
    # full control over the appearance at every resolution (without
    # this, Pillow downscales the largest internally and the small
    # sizes lose detail).
    largest = images[-1]
    largest.save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in _ICON_SIZES],
        append_images=images[:-1],
    )
    print(
        f"Wrote {ico_path} ({len(_ICON_SIZES)} sizes, "
        f"{ico_path.stat().st_size:,} bytes)"
    )

    # Render (or pick) the 256-px image for the runtime bundle. The
    # ``_ICON_SIZES`` tuple is ordered ascending so ``images[-1]`` is
    # already the largest available — if 256 isn't in the tuple,
    # ``_render_icon`` happily produces it on demand.
    if _RUNTIME_PNG_SIZE in _ICON_SIZES:
        runtime_img = images[_ICON_SIZES.index(_RUNTIME_PNG_SIZE)]
    else:
        runtime_img = _render_icon(_RUNTIME_PNG_SIZE)
    runtime_img.save(runtime_png_path, format="PNG", optimize=True)
    print(
        f"Wrote {runtime_png_path} ({_RUNTIME_PNG_SIZE}×{_RUNTIME_PNG_SIZE}, "
        f"{runtime_png_path.stat().st_size:,} bytes)"
    )

    # Airplane-mode toolbar glyph. Same target dir as the app icon
    # so the package's resources/ folder is the single source of
    # truth for "PNG assets the running app loads"; spec files
    # already include this folder verbatim in their ``datas``
    # clause so the toolbar icon ships in both Windows and Linux
    # frozen builds without further changes.
    airplane_png_path = runtime_png_dir / "airplane_mode_icon.png"
    airplane_img = _render_airplane_mode_icon(_RUNTIME_PNG_SIZE)
    airplane_img.save(airplane_png_path, format="PNG", optimize=True)
    print(
        f"Wrote {airplane_png_path} "
        f"({_RUNTIME_PNG_SIZE}×{_RUNTIME_PNG_SIZE}, "
        f"{airplane_png_path.stat().st_size:,} bytes)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
