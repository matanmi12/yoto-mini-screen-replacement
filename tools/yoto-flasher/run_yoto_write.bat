@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

REM  Usage:  run_yoto_write.bat [COMxx] [path\to\image.bin] [target_baud]
REM  Defaults: auto-detect first COM port, .\yoto_patched.bin, 115200 baud.
REM  Writes the WHOLE 8 MB image to the Yoto flash and verifies it by MD5 before
REM  resetting the Yoto to run. Requires the WRITE-capable yoto-flasher firmware on the S3.

set "ESP_PORT=%~1"
set "BIN=%~2"
set "BAUD=%~3"

if "%ESP_PORT%"=="" (
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object; if ($ports.Count -gt 0) { $ports[0] }"`) do set "ESP_PORT=%%P"
)
if "%ESP_PORT%"=="" (
    echo ERROR: Could not auto-detect a COM port.
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object"
    set /p ESP_PORT=Type the ESP32-S3 COM port, for example COM18:
)
if "%BIN%"=="" set "BIN=%~dp0.\yoto_patched.bin"
if "%BAUD%"=="" set "BAUD=115200"

if not exist "%BIN%" (
    echo ERROR: image not found: %BIN%
    pause
    exit /b 1
)

echo.
echo   ESP32-S3 port : %ESP_PORT%
echo   Image         : %BIN%
echo   Target baud   : %BAUD%
echo.
echo   *** This ERASES and REWRITES the entire 8 MB Yoto flash. ***
echo   Keep the 6 cables connected and power stable for the whole write.
echo   Once erase starts, the Yoto will not boot until a successful, MD5-verified write finishes.
echo.
pause

set "WRITE_SCRIPT=%TEMP%\yoto_write_%RANDOM%%RANDOM%.py"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$lines = Get-Content -LiteralPath '%~f0'; $s = [Array]::IndexOf($lines, '### PYTHON_WRITE_BEGIN'); $e = [Array]::IndexOf($lines, '### PYTHON_WRITE_END'); if ($s -lt 0 -or $e -le $s) { exit 1 }; Set-Content -LiteralPath $env:WRITE_SCRIPT -Value ($lines[($s + 1)..($e - 1)] -join [Environment]::NewLine) -Encoding UTF8"
if errorlevel 1 goto :fail

set /a ATTEMPT=1
set /a MAX_ATTEMPTS=5

:retry
echo.
echo Write attempt %ATTEMPT% of %MAX_ATTEMPTS%
python "%WRITE_SCRIPT%" --port "%ESP_PORT%" --bin "%BIN%" --baud %BAUD%
set "EXIT=%ERRORLEVEL%"
if "%EXIT%"=="0" goto :done
if %ATTEMPT% GEQ %MAX_ATTEMPTS% goto :done
set /a ATTEMPT+=1
echo.
echo Write failed; waiting 3 seconds and retrying (full re-erase + rewrite)...
timeout /t 3 /nobreak >nul
goto :retry

:done
del "%WRITE_SCRIPT%" >nul 2>nul
if not "%EXIT%"=="0" goto :fail
echo.
echo SUCCESS: flash written and MD5-verified. The Yoto has been reset to run the new firmware.
pause
exit /b 0

:fail
echo.
echo FAILED. The Yoto flash may be partially written -- rerun this tool (do not power-cycle the Yoto first).
pause
exit /b 1

### PYTHON_WRITE_BEGIN
import argparse
import base64
import hashlib
import os
import re
import sys
import time
import zlib

import serial

BLOCK = 4096
EXPECT_SIZE = 8 * 1024 * 1024


def reset_esp32_to_app(ser):
    try:
        ser.dtr = False
        ser.rts = True
        time.sleep(0.12)
        ser.rts = False
        time.sleep(1.2)
        ser.reset_input_buffer()
    except Exception as exc:
        print(f"Warning: could not toggle reset lines: {exc}")


def progress(done, total, started_at):
    pct = min(100.0, done * 100.0 / total) if total else 0.0
    elapsed = max(0.001, time.time() - started_at)
    rate = max(0.001, done / elapsed)
    eta = max(0, total - done) / rate
    width = 32
    filled = int(width * pct / 100.0)
    bar = "#" * filled + "-" * (width - filled)
    sys.stdout.write(
        f"\r[{bar}] {pct:6.2f}%  {done/(1024*1024):5.2f}/{total/(1024*1024):.2f} MiB  "
        f"{rate/1024:6.1f} KiB/s  ETA {eta:5.0f}s"
    )
    sys.stdout.flush()


def run(args):
    with open(args.bin, "rb") as f:
        image = f.read()
    if len(image) != EXPECT_SIZE:
        print(f"ERROR: image is {len(image)} bytes, expected {EXPECT_SIZE}")
        return 2
    if len(image) % BLOCK != 0:
        print("ERROR: image size is not a multiple of the block size")
        return 2
    if image[0x1000] != 0xE9:
        print(f"ERROR: byte @0x1000 = 0x{image[0x1000]:02x}, expected 0xe9 (not a valid image?)")
        return 2

    md5 = hashlib.md5(image).hexdigest()
    sha = hashlib.sha256(image).hexdigest()
    nblocks = len(image) // BLOCK
    print(f"Image OK: {len(image)} bytes, {nblocks} blocks")
    print(f"  MD5   : {md5}")
    print(f"  SHA256: {sha}")

    nblocks = len(image) // BLOCK
    write_cmd = f"WRITE size={len(image)} block={BLOCK} baud={args.baud} md5={md5}\n".encode("ascii")

    ser = serial.Serial(args.port, 921600, timeout=1, write_timeout=10)
    try:
        print(f"Opening {args.port} at 921600; resetting S3 helper...")
        reset_esp32_to_app(ser)

        # Send WRITE until acknowledged.
        acked = False
        deadline = time.time() + 60
        next_send = 0.0
        while time.time() < deadline and not acked:
            if time.time() >= next_send:
                ser.write(write_cmd)
                ser.flush()
                next_send = time.time() + 0.5
            raw = ser.readline()
            if not raw:
                continue
            line = raw.strip()
            if line.startswith(b"WRITE_ACK"):
                print(line.decode("ascii", "replace"))
                acked = True
            elif line.startswith((b"E ", b"W ", b"WRITE_FAILED")):
                print(line.decode("ascii", "replace"))
        if not acked:
            print("ERROR: no WRITE_ACK from firmware (is the WRITE-capable firmware flashed?)")
            return 3

        next_re = re.compile(rb"^NEXT idx=(\d+)")
        started_at = None
        # Erase can take a while; allow a long gap until WRITE_BEGIN, then per-line idle.
        idle_deadline = time.time() + 180
        while True:
            if time.time() > idle_deadline:
                print("\nERROR: timed out waiting for firmware")
                return 4
            raw = ser.readline()
            if not raw:
                continue
            line = raw.strip()
            if not line:
                continue

            m = next_re.match(line)
            if m:
                idx = int(m.group(1))
                if idx >= nblocks:
                    print(f"\nERROR: firmware requested idx {idx} >= {nblocks}")
                    return 5
                blk = image[idx * BLOCK:(idx + 1) * BLOCK]
                crc = zlib.crc32(blk) & 0xFFFFFFFF
                payload = b"D " + str(idx).encode() + b" " + base64.b64encode(blk) + b" " + format(crc, "x").encode() + b"\n"
                ser.write(payload)
                ser.flush()
                if started_at is None:
                    started_at = time.time()
                if idx % 32 == 0 or idx == nblocks - 1:
                    progress((idx + 1) * BLOCK, len(image), started_at or time.time())
                idle_deadline = time.time() + 30
                continue

            if line.startswith(b"WRITE_BEGIN"):
                print("\n" + line.decode("ascii", "replace"))
                idle_deadline = time.time() + 30
                continue
            if line.startswith(b"WRITE_ERASE"):
                print("Erasing 8 MB (this can take ~10-30 s)...")
                idle_deadline = time.time() + 180
                continue
            if line.startswith((b"WRITE_STUB", b"WRITE_BAUD")):
                print("\n" + line.decode("ascii", "replace"))
                idle_deadline = time.time() + 30
                continue
            if line.startswith(b"RESEND"):
                sys.stdout.write("  (resend block " + line.split(b" ", 1)[-1].decode("ascii", "replace") + ")\n")
                idle_deadline = time.time() + 30
                continue
            if line.startswith(b"PROG"):
                idle_deadline = time.time() + 30
                continue
            if line.startswith(b"WRITE_VERIFY_OK"):
                print("\nWRITE_VERIFY_OK (target flash matches image MD5)")
                idle_deadline = time.time() + 30
                continue
            if line.startswith(b"WRITE_DONE"):
                print("WRITE_DONE - Yoto reset to run the new firmware.")
                return 0
            if line.startswith((b"WRITE_VERIFY_FAIL", b"WRITE_FAILED")):
                print("\n" + line.decode("ascii", "replace"))
                return 6
            # otherwise: firmware log noise, ignore
    finally:
        ser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--bin", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
### PYTHON_WRITE_END
