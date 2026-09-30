#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
analyze_calibration_photo.py - compare the original Yoto panel against a
replacement ST7789 from a single photo of the solid-colour calibration chart.

You take ONE photo with both Yotos side by side, both showing solid_16x16.png
(see docs/COLOR_CALIBRATION.md). Then, for each display, you give this tool the
four corner points of the 192x192 chart area (top-left, top-right, bottom-right,
bottom-left, in pixel coordinates). It samples the centre of each of the 16
patches on each panel, averages a small region, and prints an
original-vs-replacement table plus a plain-language read of what differs.

It does NOT treat phone RGB as absolute colour: it reports the *relative*
difference between the two panels in the same photo, which is the only valid
comparison. Requires Pillow + numpy:  pip install pillow numpy

Getting corner coordinates: open the photo in any image viewer that shows the
pixel coordinate under the cursor (macOS Preview's rectangular selection shows
size+origin; most editors show x,y in a corner). Read off the four corners of
the bright chart square on each display.

Example:
    python analyze_calibration_photo.py photo.jpg \\
        --original 120,80 300,84 298,262 118,258 \\
        --replacement 520,78 700,82 698,260 518,256 \\
        --patches charts/patches.json
"""

import argparse
import json
import sys

try:
    import numpy as np
    from PIL import Image
except ImportError:
    raise SystemExit("Pillow and numpy are required: pip install pillow numpy")


def parse_corners(s, what):
    pts = []
    for tok in s.split():
        x, y = tok.split(",")
        pts.append((float(x), float(y)))
    if len(pts) != 4:
        raise SystemExit(f"--{what} needs 4 points 'x,y x,y x,y x,y' (TL TR BR BL), got {len(pts)}")
    return pts


def bilinear(corners, u, v):
    """Map (u,v) in [0,1]^2 to a photo pixel using the 4 corners TL,TR,BR,BL."""
    tl, tr, br, bl = corners
    top = (tl[0] + (tr[0] - tl[0]) * u, tl[1] + (tr[1] - tl[1]) * u)
    bot = (bl[0] + (br[0] - bl[0]) * u, bl[1] + (br[1] - bl[1]) * u)
    return (top[0] + (bot[0] - top[0]) * v, top[1] + (bot[1] - top[1]) * v)


def sample(img, cx, cy, half=3):
    x0, y0 = int(round(cx)) - half, int(round(cy)) - half
    box = img.crop((x0, y0, x0 + 2 * half + 1, y0 + 2 * half + 1))
    return np.asarray(box, dtype=float).reshape(-1, 3).mean(axis=0)


def load_patches(path):
    if path:
        data = json.load(open(path))
        return [(p["name"], p["sample_center_192"], p["source_rgb888"]) for p in data["solid"]]
    # fall back to the built-in 4x4 layout / centres if no patches.json given
    names = ["WHITE", "GRAY75", "GRAY50", "GRAY25", "RED", "GREEN", "BLUE", "BLACK",
             "CYAN", "MAGENTA", "YELLOW", "YOTO_ORANGE", "DARK_RED", "BROWN_ORANGE",
             "WARM_MIDTONE", "DARK_GRAY"]
    out = []
    for i, n in enumerate(names):
        r, c = divmod(i, 4)
        out.append((n, [c * 48 + 24, r * 48 + 24], None))
    return out


def main():
    ap = argparse.ArgumentParser(description="Compare original vs replacement Yoto panel from one photo.")
    ap.add_argument("photo")
    ap.add_argument("--original", required=True, metavar="TL TR BR BL",
                    help="4 corner pixels of the chart on the ORIGINAL panel, 'x,y x,y x,y x,y'")
    ap.add_argument("--replacement", required=True, metavar="TL TR BR BL",
                    help="4 corner pixels of the chart on the REPLACEMENT panel")
    ap.add_argument("--patches", help="charts/patches.json (for names + sample points)")
    ap.add_argument("--half", type=int, default=3, help="half-size of the sample square in px (default 3)")
    args = ap.parse_args()

    img = Image.open(args.photo).convert("RGB")
    oc = parse_corners(args.original, "original")
    rc = parse_corners(args.replacement, "replacement")
    patches = load_patches(args.patches)

    print(f"{'patch':13s} {'original R  G  B':>18s} {'replacement R  G  B':>21s} "
          f"{'dR':>5s} {'dG':>5s} {'dB':>5s}  note")
    rows = []
    for name, (px, py), _src in patches:
        u, v = px / 192.0, py / 192.0
        o = sample(img, *bilinear(oc, u, v), args.half)
        r = sample(img, *bilinear(rc, u, v), args.half)
        d = r - o
        note = ""
        if max(abs(d)) > 8:
            ch = "RGB"[int(np.argmax(np.abs(d)))]
            note = f"replacement {'+' if d[np.argmax(np.abs(d))] > 0 else '-'}{ch}"
        print(f"{name:13s} {o[0]:5.0f}{o[1]:5.0f}{o[2]:5.0f}      "
              f"{r[0]:6.0f}{r[1]:6.0f}{r[2]:6.0f}     "
              f"{d[0]:+5.0f}{d[1]:+5.0f}{d[2]:+5.0f}  {note}")
        rows.append((name, o, r, d))

    # --- exposure check: clipped or floored patches cannot be compared -------
    CLIP, FLOOR = 250, None
    # Only neutral patches matter for the tone curve; saturated primaries are
    # EXPECTED to hit max on their dominant channel, so they are not flagged.
    NEUTRALS = {"WHITE", "GRAY75", "GRAY50", "GRAY25", "DARK_GRAY", "BLACK"}
    clipped = [(n, side) for n, o, r, _ in rows if n in NEUTRALS
               for side, v in (("original", o), ("replacement", r)) if v.max() >= CLIP]
    black = {n: (o, r) for n, o, r, _ in rows}.get("BLACK")

    print("\nExposure check:")
    if clipped:
        by_side = {}
        for n, side in clipped:
            by_side.setdefault(side, []).append(n)
        for side, names in by_side.items():
            print(f"  - {side}: neutral patch(es) clipped (>= {CLIP}): {', '.join(names)}")
        if all(n == "WHITE" for n, _ in clipped):
            print("    Only WHITE is clipped: usable, but that panel's ratios below read slightly high.")
        else:
            print("    Gray patches are clipped and hide the real difference. Retake with the exposure")
            print("    lowered until the BRIGHTER panel's WHITE patch is just below clipping.")
    else:
        print("  - no neutral patch clipped: exposure OK")
    if black is not None:
        print(f"  - black level: original {black[0].mean():.0f}, replacement {black[1].mean():.0f}"
              " (higher = lifted blacks / glow / flare)")

    # --- tone curve, each panel normalised to ITS OWN white and black --------
    # This separates gamma (curve shape) from backlight brightness (a scale),
    # so it is valid even when one panel is much brighter than the other.
    def tone(v, white, blk):
        span = white.mean() - blk.mean()
        return (v.mean() - blk.mean()) / span if span > 1 else float("nan")

    vals = {n: (o, r) for n, o, r, _ in rows}
    if {"WHITE", "BLACK"} <= vals.keys():
        ow, rw = vals["WHITE"]
        ob, rb = vals["BLACK"]
        white_ok = ow.max() < CLIP and rw.max() < CLIP
        print("\nTone curve (patch level relative to that panel's own black..white):")
        print(f"  {'patch':8s} {'input':>6s} {'original':>9s} {'replacement':>12s}")
        for n, lvl in (("GRAY75", 192), ("GRAY50", 128), ("GRAY25", 64), ("DARK_GRAY", 32)):
            if n in vals:
                o, r = vals[n]
                print(f"  {n:8s} {lvl/255:6.2f} {tone(o, ow, ob):9.2f} {tone(r, rw, rb):12.2f}")
        if not white_ok:
            print("  (a WHITE patch is clipped, so these ratios are only indicative)")

        # replacement's own curve: how dark are its midtones vs its white?
        r50 = tone(vals["GRAY50"][1], rw, rb) if "GRAY50" in vals else float("nan")
        o50 = tone(vals["GRAY50"][0], ow, ob) if "GRAY50" in vals else float("nan")

    # --- colour cast of the neutrals (normalised, so brightness cancels) -----
    def cast(v, white):
        w = np.maximum(white, 1)
        n = v / w
        return n - n.mean()

    if {"WHITE", "GRAY75", "GRAY50"} <= vals.keys():
        print("\nNeutral tint (GRAY75+GRAY50 divided by that panel's own WHITE, R/G/B):")
        for side, i in (("original", 0), ("replacement", 1)):
            w = np.maximum(vals["WHITE"][i], 1)
            g = (vals["GRAY75"][i] / w + vals["GRAY50"][i] / w) / 2
            lean = "blue" if g[2] - g[0] > 0.06 else "red" if g[0] - g[2] > 0.06 else "neutral"
            print(f"  {side:12s} {g[0]:.2f} {g[1]:.2f} {g[2]:.2f}  -> {lean}")

    # In-frame ratio replacement/original per neutral patch: exposure cancels to
    # first order, so this is the number to compare ACROSS photos (valid only
    # where the original's patch is not clipped).
    if {"GRAY75", "GRAY50", "GRAY25"} <= vals.keys():
        print("\nIn-frame ratio replacement/original (mean of R,G,B; compare this across photos):")
        for n in ("GRAY75", "GRAY50", "GRAY25"):
            o, r = vals[n]
            ratio = float(np.mean(r / np.maximum(o, 1)))
            flag = "  (original clipped, reads low)" if o.max() >= CLIP else ""
            print(f"  {n:8s} {ratio:5.2f}{flag}")

    print("\nRead (see docs/COLOR_CALIBRATION.md section 6):")
    said = False
    if {"WHITE", "GRAY50"} <= vals.keys():
        if r50 == r50 and o50 == o50 and (o50 - r50) > 0.10:
            print(f"  - Replacement midtones are much darker than the original's, relative to")
            print(f"    each panel's own white (GRAY50: {r50:.2f} vs {o50:.2f}). That is a steeper")
            print(f"    tone curve = gamma, and it is what turns orange into brown.")
            print(f"    GAMSET presets did not fix this on the tested panel; see the results table")
            print(f"    in docs/COLOR_CALIBRATION.md for the analog knobs (PWCTRL2, VRHS, VCOM).")
            said = True
        elif r50 == r50 and o50 == o50 and (r50 - o50) > 0.10:
            print("  - Replacement midtones are brighter than the original's (curve too shallow).")
            said = True
    rw_ = vals.get("WHITE", (None, None))[1]
    ow_ = vals.get("WHITE", (None, None))[0]
    if rw_ is not None and ow_ is not None and ow_.max() < CLIP and rw_.max() < CLIP:
        rg = vals["GRAY50"][1] if "GRAY50" in vals else None
        if rg is not None:
            c = cast(rg, rw_)
            if c[0] - c[2] > 0.08:
                print("  - Replacement neutrals lean red vs its own white: consider --vcom.")
                said = True
    if not said:
        print("  - No confident verdict from this photo" + (" (fix exposure first)." if clipped else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
