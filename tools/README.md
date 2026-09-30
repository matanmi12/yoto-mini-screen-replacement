# tools/

Utilities for backing up and patching a Yoto Mini's **own** firmware so it
drives a replacement ST7789 panel correctly.

Full workflow and safety notes:
[../docs/FIRMWARE_DUMP_AND_PATCH.md](../docs/FIRMWARE_DUMP_AND_PATCH.md).

| Item | What it is |
|---|---|
| [`yoto_patch.py`](yoto_patch.py) | Verify-before-write firmware patcher: MADCTL `0x48`→`0x00` (display fix), inversion, and injected single-parameter ST7789 colour registers (GAMSET/VCOMS/…) for calibration, plus image re-sign (checksum + SHA-256). Locates every site by code signature, so it is **not tied to one firmware version** (verified on v2.23.2 and v2.23.4); it refuses on a build whose display-init code differs. Operates on a dump *you* made; stdlib only. See [../docs/COLOR_CALIBRATION.md](../docs/COLOR_CALIBRATION.md). |
| [`make_calibration_chart.py`](make_calibration_chart.py) | Generates the solid-colour and gradient calibration charts (16×16 source + 192×192/240×240) and a `patches.json` with exact colours and sample points. Needs Pillow. |
| [`analyze_calibration_photo.py`](analyze_calibration_photo.py) | From one photo of both panels side by side, samples each chart patch and prints an original-vs-replacement table plus a plain-language read of the difference. Needs Pillow + numpy. |
| [`s3-flasher/`](s3-flasher) | ESP32-S3-DevKitC-1 firmware that turns the S3 into a USB-to-UART bridge for **macOS/Linux**: `esptool` dumps/writes the Yoto's flash with automatic boot-mode reset (no buttons). The cross-platform alternative to the Windows `yoto-flasher` `.bat` scripts. Build with PlatformIO. |
| [`yoto_write_checked.py`](yoto_write_checked.py) | Wraps `esptool write-flash` for the Yoto: first checks that esptool reports a **3.3 V** flash supply and 8MB, and refuses otherwise. After the Yoto app has run, GPIO12 can read high at reset, which makes the ESP32 power its flash and PSRAM at 1.8 V: writes stop mid-way and the app boot-loops with "Failed to init external RAM". A full power cycle of the Yoto fixes it. Needs esptool. |
| [`yoto-flasher/`](yoto-flasher) | ESP32-S3 helper firmware (esp-serial-flasher) that dumps or writes a Yoto's 8 MB flash over UART, plus the Windows `run_yoto_dump.bat` / `run_yoto_write.bat` host scripts. |

Quick calibration loop (details in [../docs/COLOR_CALIBRATION.md](../docs/COLOR_CALIBRATION.md)):

```bash
python tools/make_calibration_chart.py --out charts/          # once
python tools/yoto_patch.py dump.bin --experiment all --out-dir round1/
# flash one candidate, show charts/solid_16x16.png on both Yotos, photograph both together
python tools/analyze_calibration_photo.py photo.jpg --original ... --replacement ... --patches charts/patches.json
```

## ⚠️ Never share a firmware dump

A full dump contains your **Wi-Fi password**, the device's **account token**,
and **MAC addresses**, and is Yoto's proprietary code. Keep every `*.bin`,
`dumps/` and serial `*.log` on your own machine. The repo's
[`.gitignore`](../.gitignore) blocks those patterns so you can't commit one by
accident — don't override it.

## Requirements

- **`yoto_patch.py`**: Python 3.8+ (standard library only).
- **`make_calibration_chart.py`**: Python 3 + Pillow (`pip install pillow`).
- **`analyze_calibration_photo.py`**: Python 3 + Pillow + numpy.
- **`run_yoto_*.bat`**: Windows, Python 3 with `pyserial`, and the
  `yoto-flasher` firmware flashed onto an ESP32-S3.
- **`yoto-flasher` build**: ESP-IDF v5.1.x. `idf.py` pulls
  `espressif/esp-serial-flasher` automatically (see `main/idf_component.yml`).
