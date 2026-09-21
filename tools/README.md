# tools/

Utilities for backing up and patching a Yoto Mini's **own** firmware so it
drives a replacement ST7789 panel correctly.

Full workflow and safety notes:
[../docs/FIRMWARE_DUMP_AND_PATCH.md](../docs/FIRMWARE_DUMP_AND_PATCH.md).

| Item | What it is |
|---|---|
| [`yoto_patch.py`](yoto_patch.py) | Verify-before-write firmware patcher: MADCTL `0x48`→`0x00` (display fix), optional OTA version bump, and image re-sign (checksum + SHA-256). Operates on a dump *you* made; needs only Python 3. |
| [`yoto-flasher/`](yoto-flasher) | ESP32-S3 helper firmware (esp-serial-flasher) that dumps or writes a Yoto's 8 MB flash over UART, plus the Windows `run_yoto_dump.bat` / `run_yoto_write.bat` host scripts. |

## ⚠️ Never share a firmware dump

A full dump contains your **Wi-Fi password**, the device's **account token**,
and **MAC addresses**, and is Yoto's proprietary code. Keep every `*.bin`,
`dumps/` and serial `*.log` on your own machine. The repo's
[`.gitignore`](../.gitignore) blocks those patterns so you can't commit one by
accident — don't override it.

## Requirements

- **`yoto_patch.py`**: Python 3.8+ (standard library only).
- **`run_yoto_*.bat`**: Windows, Python 3 with `pyserial`, and the
  `yoto-flasher` firmware flashed onto an ESP32-S3.
- **`yoto-flasher` build**: ESP-IDF v5.1.x. `idf.py` pulls
  `espressif/esp-serial-flasher` automatically (see `main/idf_component.yml`).
