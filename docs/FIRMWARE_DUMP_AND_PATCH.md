# Dumping and patching your own Yoto firmware

After swapping in a replacement ST7789 panel, this is how you make the picture
correct: patch the Yoto's *own* firmware so it drives the new panel with no
mirror and the right colors (the `MADCTL` fix explained in
[DISPLAY_AND_COLORS.md](DISPLAY_AND_COLORS.md)).

The workflow is: **dump your device → patch the dump → write it back**. The
same ESP32-S3 helper does the dump and the write; a Python tool does the patch.

> ## ⚠️ Read this first
>
> - **A dump contains your personal data.** The 8 MB image holds your Wi-Fi
>   SSID and password (NVS), the device's signed cloud **account token**, and
>   MAC addresses. **Never upload or share a dump.** This repo ships no images,
>   and `.gitignore` blocks `*.bin`/`dumps/`/`*.log` so you can't commit one by
>   accident.
> - **It's proprietary firmware.** Only ever operate on your own device, for
>   your own repair/interoperability.
> - **Brick risk is real.** Writing modifies the boot slot. The writer erases
>   before it writes and won't reset the device until an MD5 verify passes, but
>   a power loss mid-write, or a bad patch, can leave the Yoto unbootable until
>   you re-flash a good image. Keep your **unmodified** dump as your recovery
>   image.
> - **Version-specific.** Everything here was derived from firmware **v2.23.4**.
>   The patcher verifies every byte and refuses on mismatch rather than writing
>   blind — do not force it onto another version.

---

## Hardware: the ESP32-S3 flash helper

The Yoto Mini's SoC is an ESP32 (ESP32-D0WD-V3, 8 MB flash). The
[`tools/yoto-flasher`](../tools/yoto-flasher) firmware runs on a **second
ESP32-S3** and uses [esp-serial-flasher](https://github.com/espressif/esp-serial-flasher)
to talk to the Yoto's ROM bootloader over UART, reading or writing the whole
flash. Wiring (6 wires) — from the flasher's `main/main.c`:

| ESP32-S3 (helper) | → Yoto | Wire |
|---|---|---|
| GPIO6 (RX) | ← Yoto TXD0 | yellow |
| GPIO5 (TX) | → Yoto RXD0 | green |
| GPIO7 | → Yoto EN (reset) | black |
| GPIO4 | → Yoto IO0 (boot) | red |
| GND | ↔ Yoto GND | — |
| 3V3 | ↔ Yoto 3V3 (or power the Yoto normally) | — |

Build and flash the helper onto the S3:

```bash
cd tools/yoto-flasher
idf.py set-target esp32s3
idf.py build
idf.py -p PORT flash monitor      # PORT = the S3's CH343 serial port
```

The console runs at **921600** baud. The same firmware does both READ (dump,
the default) and WRITE (only when the host explicitly sends a `WRITE` command),
so nothing is ever written unless you run the write step.

---

## Step 1 — Dump your device

On Windows, the batch script drives the whole capture (auto-detects the COM
port, resumes if interrupted, and writes a SHA-256):

```bat
cd tools\yoto-flasher
run_yoto_dump.bat COM_PORT
```

It produces `dumps\yoto_dump.bin` (+ `.sha256` and a serial `.log`). This is a
**read-only** operation — it does not erase or modify the Yoto. Keep this file
private and keep a copy as your recovery image.

*(The script extracts and runs an embedded Python capture tool — it needs
Python with `pyserial`. Under the hood the S3 streams the flash base64-encoded
over its console; the host reassembles, checks each 1 KB chunk, and verifies
the final size + SHA-256.)*

---

## Step 2 — Patch the dump

[`tools/yoto_patch.py`](../tools/yoto_patch.py) edits the active app partition
and re-signs it. It needs only Python 3 (standard library), and it **verifies
every original byte before writing** and refuses if anything doesn't match.

```bash
python tools/yoto_patch.py dumps/yoto_dump.bin --dry-run   # preview, writes nothing
python tools/yoto_patch.py dumps/yoto_dump.bin             # writes dumps/yoto_dump.patched.bin
```

What it does by default:

1. **MADCTL `0x48` → `0x00`** — clears MX (mirror) and BGR (color swap) so the
   firmware drives a stock ST7789 with no mirror and RGB order. The value is an
   immediate inside a `movi.n` instruction, so the tool matches a 16-byte code
   signature and rewrites the immediate (`4c 8b` → `0c 0b`). If your panel needs
   a different orientation, pick another value with `--madctl` (the reachable
   MX/MV/BGR combinations are tabulated in
   [DISPLAY_AND_COLORS.md](DISPLAY_AND_COLORS.md)).
2. **Re-sign** — recomputes the ESP-IDF image's 1-byte checksum and 32-byte
   appended SHA-256 so the bootloader accepts the modified slot.

It does **not** touch the version string by default (see "Keeping the patch"
below for why a version spoof is a bad idea here).

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` | report the changes and the resulting MD5; write nothing |
| `--slots active` | patch only the active boot slot (default) |
| `--slots all` | patch every app slot it recognises (`factory`/`ota_0`/`ota_1`); slots that are a different firmware build are skipped with a warning |
| `--madctl HEX` | target MADCTL value (default `0x00`); one of `0x00 0x08 0x20 0x28 0x40 0x48 0x60 0x68` |
| `--no-madctl` | skip the display fix |
| `--spoof-version` | bump the reported version — **not recommended**, backfires on the Yoto cloud (see below); kept only to reproduce the historical v1 image |
| `--major N` | major digit for `--spoof-version` (default `9`) |
| `-o FILE` | output path (default `<input>.patched.bin`) |

### Keeping the patch (don't spoof the version)

The obvious idea — report a huge version so the cloud thinks you're up to date —
**does not work on the Yoto backend and actively backfires.** The server does
not do "update only if newer"; it force-pushes the firmware it *expects* for
the device. Reporting `v9.23.4` when it expected `v2.23.4` looked like
corruption, so it pushed a "repair" to the inactive slot and switched the boot
slot — reverting the display fix after about a day. So:

- **Report the real version** (the default here does).
- **Patch every slot you can** (`--slots all`) so that even if the cloud
  switches slots, the booted firmware still has the MADCTL fix. On a device
  whose slots are different builds, the tool patches the ones it recognises and
  warns about the rest — those need their own signature derived.
- **Advanced / most reliable:** neuter the OTA *write path* itself (make
  `esp_ota_write` fail on the first chunk) so an update can't complete. That is
  build- and offset-specific, higher risk, and not automated here; it is noted
  for completeness, not as a turnkey step.

### How the patch was validated

The offsets and signatures were derived by diffing a known-good hand-patched
image against its source dump, then encoded as **verified signatures**. The
tool is regression-checked: run with `--spoof-version` on the reference v2.23.4
dump it reproduces the original hand-patched image **byte-for-byte** (identical
MD5). Because it re-derives the partition table and active slot from the image
and checks every byte before writing, it degrades safely on anything it doesn't
recognise (it refuses, or in `--slots all` skips the unmatched slot).

---

## Step 3 — Write it back

```bat
cd tools\yoto-flasher
run_yoto_write.bat COM_PORT path\to\yoto_dump.patched.bin
```

This **erases and rewrites the entire 8 MB flash**, then verifies the target
against the image's MD5 before resetting the Yoto to boot. Keep the 6 wires
connected and power stable for the whole write. If a write fails, **re-run it
before power-cycling the Yoto** — the flash may be partially written, and the
tool will re-erase and rewrite cleanly.

To recover a bad flash, write your **unmodified** `yoto_dump.bin` back the same
way.

---

## Restoring / undoing

- **Undo the patch:** write your original `yoto_dump.bin` back. Because you kept
  it (right?), this is a full restore.
- **Go back to stock updates:** restore the original image and let the device
  come online; the cloud will bring it to the current release on its own.
