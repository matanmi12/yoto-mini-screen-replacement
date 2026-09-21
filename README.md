# Yoto Mini — ST7789 screen replacement + firmware patch

Replace a **Yoto Mini**'s LCD with a generic 1.3" 240×240 **ST7789** panel, and
patch the Yoto's own firmware so the picture comes out right.

A common ST7789 panel is electrically compatible with the Yoto Mini's LCD FPC
and lights up on stock firmware — but the image is **mirrored left↔right** and
has **red and blue swapped** (Yoto's orange UI turns blue). Both problems come
from a single register value the firmware writes for its *original* panel. One
small, reversible firmware patch fixes them.

- **The whole "why"** — panels, `MADCTL`, `COLMOD`, color order:
  → [docs/DISPLAY_AND_COLORS.md](docs/DISPLAY_AND_COLORS.md)
- **The how** — dump your own firmware, patch it, write it back:
  → [docs/FIRMWARE_DUMP_AND_PATCH.md](docs/FIRMWARE_DUMP_AND_PATCH.md)

---

## The fix in one line

The stock firmware programs its panel with `MADCTL (0x36) = 0x48` = **MX**
(column mirror) + **BGR** (blue/red order). That's correct for the original
GC9306-class panel's wiring; a stock ST7789 interprets the same flags against a
different physical column order and subpixel order, hence mirror + swapped
colors. Patching that value to `0x00` (no mirror, RGB) makes a stock ST7789
look correct. Details and the other reachable orientations are in
[docs/DISPLAY_AND_COLORS.md](docs/DISPLAY_AND_COLORS.md).

## Workflow

You patch **your own** device's firmware — nothing pre-built is distributed
(see the privacy note below). All three steps and their safety caveats are in
[docs/FIRMWARE_DUMP_AND_PATCH.md](docs/FIRMWARE_DUMP_AND_PATCH.md):

1. **Dump** your Yoto's 8 MB flash with the ESP32-S3 helper
   ([`tools/yoto-flasher`](tools/yoto-flasher), `run_yoto_dump.bat`). Read-only;
   keep this image as your recovery backup.
2. **Patch** the dump with [`tools/yoto_patch.py`](tools/yoto_patch.py): it sets
   `MADCTL` (default `0x00`, configurable with `--madctl`) and re-signs the app
   image. It **verifies every byte before writing** and refuses on any mismatch.
3. **Write** the patched image back (`run_yoto_write.bat`); it erases, rewrites,
   and MD5-verifies the flash before resetting the Yoto.

```bash
python tools/yoto_patch.py dumps/yoto_dump.bin --dry-run   # preview only
python tools/yoto_patch.py dumps/yoto_dump.bin             # -> dumps/yoto_dump.patched.bin
```

---

## ⚠️ Privacy: firmware dumps contain your personal data

A full Yoto Mini flash dump is **not safe to share**. It contains:

- your home **Wi-Fi SSID and password** (in the NVS partition),
- the device's **account/API token** (a signed JWT for the Yoto cloud) and
  **device identifiers**,
- **MAC addresses**.

It is also Yoto's **proprietary firmware**. For both reasons **this repository
contains no firmware images**, and [.gitignore](.gitignore) blocks `*.bin`,
`dumps/` and serial `*.log` files so you cannot commit one by accident. Keep any
dump you make private. If a dump (or the token/credentials inside it) ever
leaks, rotate the affected Wi-Fi password and re-provision the device.

---

## Repository layout

```
tools/
  yoto_patch.py               patcher: MADCTL fix + image re-sign (Python 3, stdlib only)
  yoto-flasher/               ESP32-S3 helper firmware to DUMP / WRITE the Yoto's flash
    main/main.c               esp-serial-flasher based read+write tool
    run_yoto_dump.bat         host-side dump orchestration (resume + SHA256)
    run_yoto_write.bat        host-side flash-write orchestration (MD5 verified)
docs/
  DISPLAY_AND_COLORS.md       panels, MADCTL/COLMOD, color order, MADCTL value table
  FIRMWARE_DUMP_AND_PATCH.md  dump-your-own + patch + write-back guide
```

---

## Hardware

- **Target:** Yoto Mini (SoC: ESP32-D0WD-V3, 8 MB flash) with its LCD replaced
  by a 1.3" 240×240 ST7789 IPS panel on the existing LCD FPC.
- **Flash helper:** any ESP32-S3 dev board (used only to dump/write the Yoto's
  flash over UART — see the patch guide for the 6-wire hookup).

### Yoto Mini LCD connector pinout

For choosing/wiring a compatible replacement panel (straight, non-reversed FPC):

| Pin | Signal | Pin | Signal |
|---|---|---|---|
| 1 | GND | 7 | D/C |
| 2 | LEDK (backlight −) | 8 | CS (active low) |
| 3 | LEDA (backlight +) | 9 | SCL / CLK |
| 4 | VDD (3.3 V) | 10 | SDA / MOSI |
| 5 | GND | 11 | RESET (active low) |
| 6 | GND | 12 | GND |

The panel speaks 4-wire SPI, mode 0, and the firmware drives it with
`COLMOD = 0x06` (18-bit RGB666, 3 bytes/pixel) into a centered 192×192 window of
the 240×240 panel. See [docs/DISPLAY_AND_COLORS.md](docs/DISPLAY_AND_COLORS.md).

---

## Status

Reverse-engineered and applied against Yoto Mini firmware **v2.23.4**. The
firmware offsets/signatures used by the patcher are specific to that version and
are re-verified before every write, so the tool fails safely on any other
version rather than writing blind.

## License

[MIT](LICENSE) for the code and docs here. It does **not** cover Yoto's device
firmware, which is proprietary and not included. "Yoto" and "Yoto Mini" are
trademarks of their respective owner, used only for identification. This is an
independent interoperability/repair project, not affiliated with or endorsed by
Yoto.
