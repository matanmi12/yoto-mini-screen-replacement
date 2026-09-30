# Calibrating a replacement ST7789 to the original Yoto panel

After the `MADCTL 0x00` fix (see [DISPLAY_AND_COLORS.md](DISPLAY_AND_COLORS.md))
the geometry and channel order are correct, but a replacement ST7789 still
looks **warmer / more red-brown** than the original Yoto panel. This document
explains why, and gives a safe, one-variable-at-a-time workflow to close the
gap.

All the firmware facts below were read directly out of the app image's display
init routine (`gc9306_display_init`) by disassembly, not assumed.

> ## TL;DR — the fix (confirmed on hardware)
>
> The replacement ST7789 was running its **factory-default gamma** (~2.2), far
> darker in the midtones than the original Yoto panel's tuned-bright curve —
> that is the whole "orange looks brown" problem. `GAMSET` presets and the
> analog registers (VCOM, VRHS, PWCTRL2) could not reach it; **injecting full
> `PVGAMCTRL`/`NVGAMCTRL` (E0/E1) gamma curves did.** One command applies the
> whole confirmed calibration:
>
> ```bash
> python tools/yoto_patch.py dumps/yoto_dump.bin --experiment recommended --slots all
> ```
>
> That is `MADCTL 0x00` + `INVON` + `COLMOD 0x06` (kept) + `VRHS 0x12` + E8 left
> at the ST7789 default + the ST7789 `tft` gamma curves. Everything below is how
> that conclusion was reached; skip to §7 for the result.

---

## 1. Why the colours differ (root cause)

The stock init programs the panel like this (confirmed in the binary; the
`0x2A/0x2B/0x2C` pixel path and `MADCTL/COLMOD` also match the live capture):

```
FE, EF,                      # GC9306 inter-command unlock
36 = 0x48,                   # MADCTL  (patched to 0x00 for ST7789)
3A = 0x06,                   # COLMOD  = RGB666, 3 bytes/pixel
A4 A5 AA AE E8 E3 FF AC AD AF A6 A7 A8 A9,   # GC9306 vendor power/timing
F0 F1 F2 F3 F4 F5,           # GC9306 vendor GAMMA (6 bytes each)
35=0x00 (TEON), 44 (STE),
21 (INVON),                  # inversion ON
11 (SLPOUT), 29 (DISPON)
```

The key fact: **most of these are GC9306 vendor commands that a stock ST7789
does not implement and ignores**: `A4`–`AF`, `EF`, `FF` and `F0`–`F5`. They
are exactly the registers that set gamma, VCOM and the analog drive voltages
on the original panel.

Three of them, checked against the ST7789V command table, **are** real ST7789
commands:

| Byte | On ST7789 | What the Yoto's write does there |
|---|---|---|
| `E8` | PWCTRL2, source / AVDD-AVCL booster clocks | sets `0x11` instead of the default `0x93`; clears a bit the datasheet fixes at 1. **Can affect the image** |
| `E3` | DGMLUTB, digital gamma LUT (blue) | no effect: digital gamma is off by default (`DGMEN` = 0) |
| `FE` | PROMACT, NVM program action | no effect without program mode enabled first |

So on the original panel those registers are tuned by the Yoto; on a
replacement ST7789 they never take effect and the panel runs on its **own
power-on defaults** for:

| What | GC9306 (original) | ST7789 replacement |
|---|---|---|
| Gamma curve | firmware-tuned (`F0`–`F5`) | ST7789 factory default (≈2.2) |
| VCOM | firmware-tuned | ST7789 default `0x20` (0.90 V) |
| Gate / source drive | firmware-tuned | ST7789 defaults |

That difference — a **gamma + analog-drive mismatch** — is the warm/red-brown
cast. It is a nonlinear (midtone) effect, which is why solid whites can look
almost right while oranges and browns look off.

What is **not** the cause, and must not be "fixed" again:

- **Channel order** — already correct (`MADCTL` BGR bit cleared). Captured
  orange pixel bytes were `F8 5C 3C` = true R,G,B.
- **Inversion** — the firmware already sends `INVON`; both panels invert. An
  `inversion off` build is only a diagnostic (it should look like a photo
  negative).
- **Colour format** — `COLMOD 0x06` (RGB666) is confirmed; do not "revert" to
  RGB565.

---

## 2. What is safely reachable, and what is not

The patcher changes only instruction *immediates* in the init routine, located
by verified code signatures. That reaches:

- **`MADCTL`** immediate — geometry / channel order (already used).
- **`INVON` ⇄ `INVOFF`** immediate — inversion.
- **Single-parameter ST7789 registers**, by *repurposing* a GC9306 vendor
  write the ST7789 ignores. Four such one-byte writes (`AF`, `AD`, `AE`, `AC`)
  are "spare slots": their command byte and data byte are both immediates, so
  each can be re-pointed at one real ST7789 register with no code relocation.
  Allowed registers (one parameter each):

  | Reg | Name | Effect | ST7789 default |
  |---|---|---|---|
  | `0x26` | GAMSET | pick 1 of 4 built-in gamma curves (2.2/1.8/2.5/1.0) | 2.2 (`0x01`) |
  | `0xBB` | VCOMS | VCOM voltage, 0.1 V + 0.025 V/step | `0x20` = 0.90 V |
  | `0xB7` | GCTRL | gate VGH/VGL | `0x35` |
  | `0xC3` | VRHS | GVDD/VAP gamma-reference voltage | `0x0B` |
  | `0xC4` | VDVS | VDV | `0x20` |
  | `0xC5` | VCMOFSET | VCOM offset | `0x20` |
  | `0xC6` | FRCTRL2 | frame rate (normal mode) | `0x0F` |

**Not reachable this way:** the full ST7789 per-channel gamma curves
`PVGAMCTRL (0xE0)` and `NVGAMCTRL (0xE1)` are 14 bytes each, and the digital
gamma LUTs are 64 bytes. Those cannot be injected by single-immediate edits;
they would need a larger code patch that relocates part of the init to make
room. `GAMSET` (one of four preset curves) is the reachable gamma knob, and it
is often enough to correct a global 2.2-vs-other mismatch.

> Note on VRHS/VDVS: on ST7789 these only take effect if `VDVVRHEN (0xC2)` is
> also set. `VCOMS`, `GCTRL` and `GAMSET` take effect on their own, which is
> why they are the first-round knobs.

---

## 3. The calibration charts

`tools/make_calibration_chart.py` (needs `pip install pillow`) writes both
kinds of chart:

```
python tools/make_calibration_chart.py --out charts/
```

- **`solid_16x16.png`** — the 4×4 solid-colour chart, as a 16×16 source image.
  This is the format the Yoto's UI art path uses: the firmware upscales a 16×16
  RGB source by an integer factor (nearest-neighbour) into the centred 192×192
  window. Because it is nearest-neighbour, **every on-screen pixel equals a
  source pixel with no resampling** — the colours are exact. Use this chart.
- **`gradients_192x192.png`** — five black→colour ramps plus a 16-step gray
  ramp, for gamma / shadow / highlight diagnosis. Needs a full-frame display
  path (see below); a 16×16 chart cannot show smooth gradients.
- `*_240x240.png` — the same, centred in a black 240×240 frame (full panel).
- **`patches.json`** — the exact RGB each patch is drawn with (pre-quantised to
  6 bits/channel so RGB666 truncation changes nothing) and the pixel to sample
  for photo analysis.

### Getting a chart onto the device

In order of preference / least invasiveness:

1. **As a track icon (guaranteed, exact).** The Yoto renders a track's 16×16
   pixel-art icon upscaled to the screen. Upload `solid_16x16.png` as a custom
   icon on a Make-Your-Own card (the same path the YotoBackupTool uses:
   `card-content.yotoplayer.com/icons/`), or set it as a track icon and play
   that track. Both panels show the identical source image.
2. **Via a device debug display path (if your build exposes it).** The firmware
   has `/display/preview` and `/system/icon_preview` HTTP handlers and a
   `display_from_rgba` path. If reachable on your LAN, one of these may accept a
   full 192×192/240×240 image — that is how to show the gradient chart. This
   depends on the build and is not documented by Yoto; treat it as best-effort.

Show the **same source file** on both devices. If you can only use the 16×16
path, the 16-step gray ramp inside the solid chart still diagnoses gamma.

---

## 4. One-variable experiment builds

Build a labelled candidate with a single display parameter changed. `MADCTL`
stays `0x00` in every one; anything you do not set is left at stock.

```bash
# individual (each writes <dump>.<name>.bin + a .manifest.json)
python tools/yoto_patch.py dump.bin --experiment baseline
python tools/yoto_patch.py dump.bin --experiment inversion_off
python tools/yoto_patch.py dump.bin --experiment gamma_A       # GAMSET 1.8
python tools/yoto_patch.py dump.bin --vcom 0x1A                # VCOMS 0.75 V
python tools/yoto_patch.py dump.bin --gamma-profile 2.5
python tools/yoto_patch.py dump.bin --display-param 0xC3:0x12  # VRHS

# or build the whole predefined round at once
python tools/yoto_patch.py dump.bin --experiment all --out-dir round1/
```

Predefined experiments (`--experiment`):

| Name | Change | Diagnostic purpose |
|---|---|---|
| `baseline` | MADCTL 0x00 only | the reference every photo is compared against |
| `inversion_on` | explicit INVON | identical to baseline (proves the tool is a no-op here) |
| `inversion_off` | INVOFF | should look like a negative; rules inversion in/out |
| `gamma_A` | GAMSET G1.8 | lighter midtones — the likely fix if orange looks brown |
| `gamma_B` | GAMSET G2.5 | darker midtones — control, should look worse |
| `gamma_C` | GAMSET G1.0 | near-linear — strong midtone lift bracket |
| `vcom_A` | VCOMS 0.75 V | shadow tint / contrast / flicker |
| `vcom_B` | VCOMS 1.10 V | common vendor VCOM |
| `power_A` | VRHS 4.45 V | gamma-reference voltage / contrast |
| `e8_default` | skip the Yoto's `E8` write | ST7789 PWCTRL2 keeps its default `0x93` (see §1) |

`e8_default` does not delete the write. It re-points it at `STE (44h)` with
the Yoto's own later STE parameters (`0x00, 0x0A`), a documented command that
the init sets to the same value again a few writes later. Its only net effect
is that PWCTRL2 stays at the ST7789 default. Manual form: `--pwctrl2 default`.

The patcher refuses to change more than one colour parameter at once unless you
pass `--allow-multi`, so each photo isolates one cause. Every build:

- verifies each site's bytes before writing and re-signs the app image
  (checksum + SHA-256), so the device still boots;
- restores any slot you did **not** set back to its stock GC9306 value, so a
  build always equals *stock + exactly the options you passed*;
- writes a `.manifest.json` recording the exact register, value, ST7789
  default, and how to revert.

**Reverting:** flash your untouched dump, or run `--experiment baseline`, which
restores every managed site to stock.

### Results so far (replacement 1.3" ST7789 panel, Yoto v2.23.4)

Midtones relative to each panel's own black and white, from side-by-side photos:

| Build | GRAY75 | GRAY50 | Verdict |
|---|---|---|---|
| original panel | 0.89–0.92 | 0.68–0.74 | reference |
| baseline (GAMSET default 2.2) | 0.41 | 0.14 | far too dark below ~60 % |
| `gamma_C` (GAMSET 1.0) | ~0 | ~0 | much darker: only saturated colours stay lit |
| `gamma_B` (GAMSET 2.5) | 0.27 | 0.10 | darker than baseline |
| `e8_default` (PWCTRL2 left at 0x93) | 0.39 | 0.24 | small lift in shadows / midtones |

The same builds as an **in-frame ratio** replacement/original for the same
patch in the same photo (exposure cancels to first order, so this is the
number to compare across photos; 1.00 = identical):

| Build | GRAY75 | GRAY50 | GRAY25 |
|---|---|---|---|
| baseline | 0.46 | 0.29 | 0.37 |
| `gamma_B` | 0.32 | 0.23 | 0.25 |
| `e8_default` | 0.41 (original clipped, reads low) | 0.33 | 0.50 |

So on this panel the built-in GAMSET curves are **not** the fix: the default
curve is the best of them. The midtone crush is larger than any gamma-preset
difference, which points at the analog settings (PWCTRL2, VRHS, VCOM) or at the
full gamma curves. `e8_default` helps a little and is kept; the next tests are stacked on top
of it: `power_A` (VRHS), then VCOM.

Recommended order: **Round 0** baseline → **Round 1** inversion_off (sanity) →
**Round 2** gamma_A/B/C → **Round 3** vcom_A/B and power_A only if the neutrals
(white/gray) are still off after gamma.

---

## 5. Photographing the two panels

One photo containing **both** displays, side by side, so lighting and camera
processing are identical and only *relative* differences matter.

- both Yotos side by side, same firmware brightness, same screen content;
- same room light, no reflections, camera perpendicular to the panels, on a
  tripod / propped, equal distance;
- **lock exposure and white balance** (tap-and-hold on most phones);
- **turn off** HDR, Night mode, auto "scene"/"enhance"/beauty processing;
- put a neutral gray or white card in the frame if you have one;
- take two photos: one of the **solid** chart, one of the **gradient** chart.

Do not compare absolute phone-RGB to the intended colour — the camera is not a
colorimeter. Compare the **original vs replacement patch in the same frame**.

---

## 6. Reading the photos

`tools/analyze_calibration_photo.py` samples both charts from one photo and
prints a per-patch original-vs-replacement table (see its `--help`). Sample the
centre of each patch, average a small region, avoid edges.

Interpretation:

| Observation | Likely cause | First knob |
|---|---|---|
| White **and** gray are warm/reddish | backlight spectrum or global analog balance | VCOMS, then accept some backlight limit |
| White/gray neutral, but orange/brown midtones too warm | **gamma** (nonlinear) | GAMSET (`gamma_A` = 1.8) |
| Shadows go reddish, highlights ok | low-end gamma / VCOM | VCOMS |
| Highlights ok, midtones wrong | gamma, not a linear channel gain | GAMSET |
| All of red raised roughly linearly | panel/channel response | (not reachable via 1-byte regs; note it) |
| Colour shift depends strongly on brightness | gamma / analog drive | GAMSET, then VRHS |
| Dark gradient bands / banding | RGB666 quantisation + panel gamma | GAMSET; expect some banding at 18-bit |

Because the reachable knob for the nonlinear part is `GAMSET` (a choice of four
fixed curves, not an arbitrary curve), the realistic outcome is a **close**
match, not a bit-exact one. If after `GAMSET` + `VCOMS` the residual is only a
slight backlight-spectrum warmth in the neutrals, that is the panel's own
backlight and is the limit of what firmware register tweaks can do.

---

## 7. Result: full E0/E1 gamma curves

The one-variable rounds settled it. Every `GAMSET` preset and every analog
register (VCOM, VRHS, PWCTRL2) left the midtones far below the original:

| Build | GRAY50 vs own white | verdict |
|---|---|---|
| original panel | 0.68–0.78 | reference |
| baseline (ST7789 default gamma ~2.2) | 0.14–0.24 | far too dark |
| `gamma_A/B/C` (GAMSET presets) | 0.10–0.24 | none better than default |
| `e8_default` (+ PWCTRL2 default) | 0.24–0.33 | small lift, kept |
| `power_A` (+ VRHS 0x12) | ~0.31 | lifts highlights, not midtones |

The replacement is simply a normal gamma-2.2 panel; the original runs a
tuned-**bright** curve via its GC9306 gamma registers. To match it the ST7789
needs its own `PVGAMCTRL`/`NVGAMCTRL` programmed — which the stock firmware
never does.

**Why it needs a code rewrite, not an immediate edit.** `E0`/`E1` take 14
parameters each and must go out as one CS-framed transaction. In the init every
command is CS-framed separately, so the ignored GC9306 `F0`..`F5` writes cannot
simply be relabelled. Instead the whole 510-byte `F0`..`F5` region is rewritten
in place into:

```
CS pulse ; E0 (PVGAMCTRL) + 14 params      # one transaction
CS pulse ; E1 (NVGAMCTRL) + 14 params      # one transaction
j <end>                                    # skip the unused tail (zero-filled)
```

`yoto_patch.py --gamma-curve tft` does this: it finds the region by its six
`F0`..`F5` markers, reads the `write_cmd`/`write_data`/gpio call targets out of
the existing code (so it stays version-agnostic), rebuilds the two transactions
with correct call/jump displacements, and re-signs. Verified by disassembly and
by reproducing the hand-built, hardware-confirmed image byte-for-byte.

The `tft` curve is Bodmer/TFT_eSPI's default ST7789 gamma:

```
E0 (PVGAMCTRL): D0 00 02 07 0A 28 32 44 42 06 0E 12 14 17
E1 (NVGAMCTRL): D0 00 02 07 0A 28 31 54 47 0E 1C 17 1B 1E
```

To try a different curve, add it to `GAMMA_CURVES` in `tools/yoto_patch.py` and
pass `--gamma-curve <name>`. Everything else about the flow (dump → patch →
write, 3.3 V gate, both slots) is unchanged.
