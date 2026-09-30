#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
make_calibration_chart.py - generate color/gamma calibration test images for
comparing a Yoto Mini's original panel against a replacement ST7789.

Why this exists
---------------
The Yoto Mini draws its UI art from a 16x16 RGB source that its firmware
upscales (nearest-neighbour, integer scale) into the centred 192x192 window of
the 240x240 panel, then streams as RGB666 (COLMOD 0x3A = 0x06, 3 bytes/pixel,
low 2 bits of each channel dropped). See docs/DISPLAY_AND_COLORS.md.

So there are two honest ways to get a *known* pattern onto the screen:

  * 16x16 source  -> upscales to a clean 192x192 of solid blocks. GUARANTEED
    exact: the firmware upscale is nearest-neighbour, so every on-screen pixel
    equals a source pixel with no resampling. This is what the normal track-art
    path shows. Use this for the solid-colour chart.

  * 192x192 (or 240x240) full image -> only reachable via a device debug path
    that accepts a full-frame image (e.g. the firmware's /display/preview
    endpoint). Use this for the fine gradients, which need more than 16 steps.
    If your firmware build does not expose such a path, fall back to the 16x16
    charts (a 16-step ramp still diagnoses gamma well).

Every colour written into the PNGs is pre-quantised to 6 bits per channel
(value & 0xFC, e.g. 255 -> 252, 102 -> 100). Those values are exactly
representable in RGB666, so it does not matter whether the Yoto pipeline
truncates or rounds when it converts to COLMOD 0x06: the panel receives the
same bytes either way. patches.json records both the requested RGB888 value
and the quantised value actually written/sent, for the photo-analysis step.

No device interaction, no firmware, stdlib + Pillow only:
    pip install pillow
    python make_calibration_chart.py --out charts/
"""

import argparse
import json
import os

try:
    from PIL import Image
except ImportError:
    raise SystemExit("Pillow is required: pip install pillow")


# --- The 4x4 solid-colour chart -------------------------------------------
# Row/col -> (name, R, G, B). Requested RGB888 values; they are quantised to
# RGB666 (& 0xFC) when drawn, see rgb666_truncate() and patches.json.
SOLID = [
    [("WHITE",       255, 255, 255), ("GRAY75",  192, 192, 192),
     ("GRAY50",      128, 128, 128), ("GRAY25",   64,  64,  64)],
    [("RED",         255,   0,   0), ("GREEN",     0, 255,   0),
     ("BLUE",          0,   0, 255), ("BLACK",     0,   0,   0)],
    [("CYAN",          0, 255, 255), ("MAGENTA", 255,   0, 255),
     ("YELLOW",      255, 255,   0), ("YOTO_ORANGE", 255, 102, 0)],
    [("DARK_RED",    128,   0,   0), ("BROWN_ORANGE", 160, 80, 32),
     ("WARM_MIDTONE", 224, 144, 112), ("DARK_GRAY", 32, 32, 32)],
]

# --- The gradient chart ----------------------------------------------------
# Each entry: (name, (R,G,B) end colour). Black -> end, left to right.
GRADIENTS = [
    ("black_to_white",  (255, 255, 255)),
    ("black_to_red",    (255,   0,   0)),
    ("black_to_green",  (  0, 255,   0)),
    ("black_to_blue",   (  0,   0, 255)),
    ("black_to_orange", (255, 102,   0)),
]
GRAY_RAMP_STEPS = 16


def rgb666_truncate(rgb):
    """Return the RGB value the panel actually receives (6 bits/channel)."""
    return tuple(c & 0xFC for c in rgb)


def make_solid_16(scale_preview=10):
    """16x16 source image: 4x4 blocks of 4x4 source pixels each."""
    img = Image.new("RGB", (16, 16))
    px = img.load()
    patches = []
    for r in range(4):
        for c in range(4):
            name, R, G, B = SOLID[r][c]
            sent = rgb666_truncate((R, G, B))
            for dy in range(4):
                for dx in range(4):
                    px[c * 4 + dx, r * 4 + dy] = sent
            patches.append({
                "chart": "solid",
                "name": name,
                "grid_row": r, "grid_col": c,
                "source_rgb888": [R, G, B],
                "png_and_panel_rgb": list(sent),
                # where it lands on the 192x192 upscaled screen (48px blocks):
                "screen_rect_192": [c * 48, r * 48, c * 48 + 48, r * 48 + 48],
                "sample_center_192": [c * 48 + 24, r * 48 + 24],
            })
    return img, patches


def make_solid_192():
    """192x192 nearest-neighbour upscale of the 16x16 solid chart."""
    img16, _ = make_solid_16()
    return img16.resize((192, 192), Image.NEAREST)


def make_gradients_192():
    """192x192 gradient chart: 5 colour ramps + one 16-step gray ramp."""
    W = 192
    bands = GRADIENTS
    n = len(bands) + 1
    band_h = W // n  # 32
    img = Image.new("RGB", (W, W), (0, 0, 0))
    px = img.load()
    meta = []
    for i, (name, (er, eg, eb)) in enumerate(bands):
        y0 = i * band_h
        for x in range(W):
            f = x / (W - 1)
            rgb = rgb666_truncate((round(er * f), round(eg * f), round(eb * f)))
            for y in range(y0, y0 + band_h):
                px[x, y] = rgb
        meta.append({"chart": "gradient", "name": name, "band_row": i,
                     "screen_y_192": [y0, y0 + band_h], "end_rgb888": [er, eg, eb],
                     "type": "smooth"})
    # 16-step gray ramp in the last band
    y0 = len(bands) * band_h
    step_w = W // GRAY_RAMP_STEPS
    ramp_levels = []
    for s in range(GRAY_RAMP_STEPS):
        v = round(255 * s / (GRAY_RAMP_STEPS - 1))
        rgb = rgb666_truncate((v, v, v))
        ramp_levels.append(list(rgb))
        for x in range(s * step_w, (s + 1) * step_w if s < GRAY_RAMP_STEPS - 1 else W):
            for y in range(y0, W):
                px[x, y] = rgb
    meta.append({"chart": "gradient", "name": "gray_ramp_16", "band_row": len(bands),
                 "screen_y_192": [y0, W], "type": "stepped", "levels": ramp_levels})
    return img, meta


def center_on_240(img192):
    """Place a 192x192 image in the centre of a 240x240 black frame, matching
    the Yoto's centred draw window (x=24..215, y=27..218 was observed; we use a
    symmetric 24,24 offset for a standalone full-frame test)."""
    canvas = Image.new("RGB", (240, 240), (0, 0, 0))
    canvas.paste(img192, (24, 24))
    return canvas


def save_preview(img, path, scale):
    img.resize((img.width * scale, img.height * scale), Image.NEAREST).save(path)


def main():
    ap = argparse.ArgumentParser(description="Generate Yoto ST7789 colour/gamma calibration charts.")
    ap.add_argument("--out", default="charts", help="output directory (default: charts/)")
    ap.add_argument("--preview-scale", type=int, default=12,
                    help="nearest-neighbour preview magnification (default 12 -> 192px from 16px)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    solid16, solid_patches = make_solid_16()
    solid16.save(os.path.join(args.out, "solid_16x16.png"))
    save_preview(solid16, os.path.join(args.out, "solid_16x16_preview.png"), args.preview_scale)

    solid192 = make_solid_192()
    solid192.save(os.path.join(args.out, "solid_192x192.png"))
    center_on_240(solid192).save(os.path.join(args.out, "solid_240x240.png"))

    grad192, grad_meta = make_gradients_192()
    grad192.save(os.path.join(args.out, "gradients_192x192.png"))
    center_on_240(grad192).save(os.path.join(args.out, "gradients_240x240.png"))

    with open(os.path.join(args.out, "patches.json"), "w") as f:
        json.dump({
            "note": "source_rgb888 is the requested colour; png_and_panel_rgb is the "
                    "6-bit-quantised value stored in the PNG and sent to the panel (COLMOD=0x06). "
                    "Sample the sample_center_192 point (scaled to your photo) for each "
                    "solid patch; compare original vs replacement panel in the same photo.",
            "solid": solid_patches,
            "gradients": grad_meta,
        }, f, indent=2)

    print(f"Wrote to {args.out}/:")
    print("  solid_16x16.png          <- load as a Yoto track/art icon (guaranteed exact, upscales to 192x192)")
    print("  solid_16x16_preview.png  <- human preview")
    print("  solid_192x192.png        <- for a full-frame debug display path")
    print("  solid_240x240.png        <- full panel, chart centred like the Yoto UI")
    print("  gradients_192x192.png    <- gamma/shadow/highlight diagnosis (needs full-frame path)")
    print("  gradients_240x240.png")
    print("  patches.json             <- exact colours + sample points for photo analysis")


if __name__ == "__main__":
    raise SystemExit(main())
