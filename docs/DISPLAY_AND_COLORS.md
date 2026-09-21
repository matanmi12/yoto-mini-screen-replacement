# The screen and the colors

Why a generic ST7789 panel, dropped into a Yoto Mini in place of the original
LCD, comes out **mirrored with red and blue swapped** on stock firmware — and
exactly which firmware byte to patch to fix it.

All values here were confirmed on hardware against Yoto Mini firmware
**v2.23.4** (by logging the live LCD command stream and capturing raw pixel
bytes during the reverse-engineering).

---

## 1. Two different panels

| | Original Yoto panel | Replacement |
|---|---|---|
| Part | F13RG30 / 13RG30 family | generic 1.3" 240×240 IPS (e.g. NFP133H-18AF) |
| Controller | driven as a **GC9306** | **ST7789** |
| Interface | 4-wire SPI, mode 0 | 4-wire SPI, mode 0 |
| Pixel format | RGB666 (18-bit) | RGB565 / RGB666 |

The two controllers share the ST77xx-style command set (CASET, RASET, RAMWR,
MADCTL, COLMOD…), so a replacement ST7789 lights up and draws on **unmodified**
Yoto firmware. But they are not identical: they assume a different **physical
column order** and a different **RGB subpixel order** for the same
memory-access flags. That single difference is the whole problem below.

---

## 2. What the stock firmware programs

Captured init sequence (abbreviated):

```
FE, EF,               # GC9306 inter-command unlock
36 = 0x48,            # MADCTL — memory access control
3A = 0x06,            # COLMOD — 18-bit / RGB666  (NOT 0x55)
<vendor init writes>,
TEON, 44, SLPOUT, DISPON
```

Two registers decide the picture.

### MADCTL (0x36) = `0x48`

`0x48` = `0b0100_1000`:

| Bit | Name | Meaning | Set in 0x48? |
|---|---|---|---|
| 7 | MY | row (page) address order | no |
| 6 | **MX** | **column address order (mirror X)** | **yes** |
| 5 | MV | row/column exchange (swap X/Y) | no |
| 4 | ML | vertical refresh order | no |
| 3 | **BGR** | **RGB↔BGR color order** | **yes** |
| 2 | MH | horizontal refresh order | no |

So the firmware asks for **column mirroring (MX)** and **BGR color order**.
Those are correct for the original panel's wiring. Feed the same flags to a
stock ST7789 and you get exactly the two symptoms:

- **MX** → the image is **mirrored left↔right**.
- **BGR** → red and blue are **swapped** (Yoto's orange UI shows blue).

### COLMOD (0x3A) = `0x06`

`0x06` selects **18-bit color, RGB666, 3 bytes per pixel**. This trips up a lot
of ST7789 code that assumes `0x55` (16-bit / RGB565). Because the Yoto drives a
compatible ST7789 with this same init, COLMOD needs no change — the point here
is only that the stream is 3 bytes/pixel. Captured RAMWR bytes for an orange
pixel came out `F8 5C 3C` = true **R, G, B** order, which is why setting MADCTL
to RGB (clearing BGR) — rather than any software channel swap — is the correct
fix. The Yoto draws into a centered **192×192** window of the 240×240 panel.

---

## 3. The fix: patch MADCTL `0x48` → `0x00`

Clearing MX and BGR gives `MADCTL = 0x00` = no mirror, RGB order — a stock
ST7789 then renders upright with correct colors. The firmware cannot be told to
do this at run time, so the value is patched directly in the firmware image and
the app partition is re-signed. Full procedure, safety, and the tool are in
[FIRMWARE_DUMP_AND_PATCH.md](FIRMWARE_DUMP_AND_PATCH.md); in short:

```bash
python tools/yoto_patch.py dumps/yoto_dump.bin            # MADCTL -> 0x00 (default)
```

### If it's still not right — other orientations

The MADCTL value is the immediate of a single `movi.n` instruction (see §4),
which can encode `0x00`–`0x5F`. That covers every **MX / MV / BGR** combination,
so if your particular panel is rotated or still mirrored, pick another value
with `--madctl`:

| `--madctl` | MX (mirror X) | MV (swap X/Y) | BGR | Typical use |
|---|:--:|:--:|:--:|---|
| `0x00` | · | · | · | default: fixes the mirror + color |
| `0x08` | · | · | ✓ | colors still swapped only |
| `0x20` | · | ✓ | · | rotated 90° |
| `0x28` | · | ✓ | ✓ | rotated 90° + color |
| `0x40` | ✓ | · | · | mirror only |
| `0x48` | ✓ | · | ✓ | stock (no-op) |
| `0x60` | ✓ | ✓ | · | mirror + swap |
| `0x68` | ✓ | ✓ | ✓ | mirror + swap + color |

Combine MX and MV to reach 90/180/270° rotations. A **vertical** flip needs MY
(`0x80`), which is `>0x5F` and does **not** fit this single-instruction patch —
it would require a larger code change and is intentionally not offered.

---

## 4. How the value lives in the firmware

MADCTL `0x48` is **not** stored as a data byte you can grep for — the driver is
hand-written (not `esp_lcd`) and sets it inline in code, as the immediate of a
`movi.n a11, 0x48` instruction (bytes `4c 8b`). It is the **only** `0x36` write
in the image (there is no separate rotation routine), sitting in the init
cluster `FE, EF, 36→48, 3A→06, …` that matches the captured stream.

The patcher therefore matches a unique 16-byte **code signature** around that
instruction and rewrites only the immediate (`4c 8b` → `0c 0b` for `0x00`),
after verifying the surrounding bytes — so it cannot silently mis-patch, and it
refuses outright on any firmware whose code differs. Because editing code bytes
invalidates the ESP-IDF app image's checksum and appended SHA-256, the tool
recomputes both ("re-signs") so the bootloader still accepts the slot. That
re-sign logic was validated by reproducing a known-good hand-patched image
byte-for-byte. See [FIRMWARE_DUMP_AND_PATCH.md](FIRMWARE_DUMP_AND_PATCH.md).
