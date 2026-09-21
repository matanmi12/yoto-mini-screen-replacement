@echo off
setlocal

cd /d "%~dp0"

set "ESP_PORT=%~1"
set "CAPTURE_MODE=%~2"
if "%ESP_PORT%"=="" (
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports = [System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object; if ($ports.Count -gt 0) { $ports[0] }"`) do set "ESP_PORT=%%P"
)

if "%ESP_PORT%"=="" (
    echo ERROR: Could not auto-detect a COM port.
    echo.
    echo Available ports, if any:
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[System.IO.Ports.SerialPort]::GetPortNames() | Sort-Object"
    echo.
    set /p ESP_PORT=Type the ESP32-S3 COM port, for example COM18: 
)

echo.
echo Using ESP32-S3 port: %ESP_PORT%
echo Project: %CD%
echo.
echo Capturing Yoto flash dump with resume, progress, and final SHA256 check.
echo Keep the Yoto wiring connected. This reads only; it does not erase or write the Yoto.
echo.

set "CAPTURE_SCRIPT=%TEMP%\yoto_capture_%RANDOM%%RANDOM%.py"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$lines = Get-Content -LiteralPath '%~f0'; $s = [Array]::IndexOf($lines, '### PYTHON_CAPTURE_BEGIN'); $e = [Array]::IndexOf($lines, '### PYTHON_CAPTURE_END'); if ($s -lt 0 -or $e -le $s) { exit 1 }; Set-Content -LiteralPath $env:CAPTURE_SCRIPT -Value ($lines[($s + 1)..($e - 1)] -join [Environment]::NewLine) -Encoding UTF8"
if errorlevel 1 goto :fail

set /a CAPTURE_ATTEMPT=1
set /a MAX_CAPTURE_ATTEMPTS=100

:capture_retry
echo.
echo Capture attempt %CAPTURE_ATTEMPT% of %MAX_CAPTURE_ATTEMPTS%
set "USE_FRESH="
if /I "%CAPTURE_MODE%"=="fresh" if "%CAPTURE_ATTEMPT%"=="1" set "USE_FRESH=1"
if defined USE_FRESH (
    python "%CAPTURE_SCRIPT%" --port "%ESP_PORT%" --fresh
) else (
    python "%CAPTURE_SCRIPT%" --port "%ESP_PORT%"
)
set "CAPTURE_EXIT=%ERRORLEVEL%"
if "%CAPTURE_EXIT%"=="0" goto :capture_done

if exist "dumps\yoto_dump.bin" goto :capture_done
if not exist "dumps\yoto_dump.bin.part" goto :capture_done
if %CAPTURE_ATTEMPT% GEQ %MAX_CAPTURE_ATTEMPTS% goto :capture_done

set /a CAPTURE_ATTEMPT+=1
echo.
echo Capture failed; waiting 3 seconds and resuming from the partial dump.
timeout /t 3 /nobreak >nul
goto :capture_retry

:capture_done
del "%CAPTURE_SCRIPT%" >nul 2>nul
if not "%CAPTURE_EXIT%"=="0" goto :fail

echo.
echo Done.
pause
exit /b 0

:fail
echo.
echo FAILED. Partial dump was kept if any progress was written.
pause
exit /b 1

### PYTHON_CAPTURE_BEGIN
import argparse
import base64
import binascii
import hashlib
import os
import re
import sys
import time

import serial

DEFAULT_BAUDS = (921600,)
DEFAULT_SIZE = 8 * 1024 * 1024
CHUNK = 1024
IDLE_TIMEOUT_S = 30


def progress(done, total, started_at):
    pct = min(100.0, done * 100.0 / total) if total else 0.0
    elapsed = max(0.001, time.time() - started_at)
    rate = max(0.001, done / elapsed)
    remaining = max(0, total - done)
    eta = remaining / rate
    width = 32
    filled = int(width * pct / 100.0)
    bar = "#" * filled + "-" * (width - filled)
    mib_done = done / (1024 * 1024)
    mib_total = total / (1024 * 1024)
    sys.stdout.write(
        f"\r[{bar}] {pct:6.2f}%  {mib_done:5.2f}/{mib_total:.2f} MiB  "
        f"{rate / 1024:6.1f} KiB/s  ETA {eta:5.0f}s"
    )
    sys.stdout.flush()


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


def safe_resume_size(path):
    if not os.path.exists(path):
        return 0
    size = os.path.getsize(path)
    aligned = size - (size % CHUNK)
    if aligned != size:
        print(f"Truncating partial dump from {size} to {aligned} bytes for chunk alignment.")
        with open(path, "r+b") as f:
            f.truncate(aligned)
    return aligned


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def wait_for_begin(ser, baud, log, resume_at):
    begin_re = re.compile(rb"DUMP_BEGIN size=(\d+) chunk=(\d+) start=0x([0-9a-fA-F]+)")
    ready_re = re.compile(rb"READY size=(\d+) chunk=(\d+)")

    deadline = time.time() + 50
    noise_bytes = 0
    sent_resume = False
    next_resume_send = 0.0

    while time.time() < deadline:
        if time.time() >= next_resume_send:
            cmd = f"START_ADDR 0x{resume_at:06x}\n".encode("ascii")
            ser.write(cmd)
            ser.flush()
            if not sent_resume:
                print(f"Resume request sent: 0x{resume_at:06x}")
            sent_resume = True
            next_resume_send = time.time() + 0.5

        raw = ser.readline()
        if not raw:
            continue
        log.write(raw)
        line = raw.strip()
        if not line:
            continue

        ready = ready_re.search(line)
        if ready:
            continue

        begin = begin_re.search(line)
        if begin:
            total = int(begin.group(1))
            chunk = int(begin.group(2))
            actual_start = int(begin.group(3), 16)
            text = line.decode("ascii", errors="replace")
            print(text)
            if chunk != CHUNK:
                raise RuntimeError(f"Unexpected chunk size {chunk}; expected {CHUNK}")
            if actual_start != resume_at:
                raise RuntimeError(f"Firmware started at 0x{actual_start:06x}, expected 0x{resume_at:06x}")
            return total

        try:
            text = line.decode("ascii")
        except UnicodeDecodeError:
            noise_bytes += len(raw)
            continue
        if text.startswith(("I ", "W ", "E ", "READY", "RESUME", "ESP-ROM")):
            print(text)

    if noise_bytes:
        print(f"Ignored {noise_bytes} non-text bytes while waiting at {baud} baud.")
    raise TimeoutError(f"No DUMP_BEGIN at {baud} baud.")


def capture(args):
    os.makedirs("dumps", exist_ok=True)
    final_path = os.path.join("dumps", "yoto_dump.bin")
    part_path = final_path + ".part"
    log_path = final_path + ".log"
    sha_path = final_path + ".sha256"

    if args.fresh:
        print("Fresh mode: removing previous yoto_dump output files.")
        for path in (part_path, final_path, log_path, sha_path):
            if os.path.exists(path):
                os.remove(path)

    if os.path.exists(final_path) and os.path.getsize(final_path) == args.expected_size:
        digest = sha256_file(final_path)
        with open(sha_path, "w", encoding="ascii") as f:
            f.write(f"{digest}  {os.path.basename(final_path)}\n")
        print(f"Already complete: {final_path}")
        print(f"SHA256: {digest}")
        return 0

    resume_at = safe_resume_size(part_path)
    if resume_at:
        print(f"Resuming partial dump at 0x{resume_at:06x} ({resume_at / (1024 * 1024):.2f} MiB).")
    else:
        print("Starting a new dump.")

    baud_rates = (args.baud,) if args.baud else DEFAULT_BAUDS
    b64_re = re.compile(rb"^[A-Za-z0-9+/]+={0,2}$")
    end_re = re.compile(rb"DUMP_END errors=(\d+)")
    abort_re = re.compile(rb"DUMP_ABORT addr=0x([0-9a-fA-F]+) retries=(\d+)")

    last_error = None
    for baud in baud_rates:
        ser = None
        try:
            print(f"Opening {args.port} at {baud} baud")
            ser = serial.Serial(args.port, baud, timeout=1, write_timeout=3)
            if not args.no_reset:
                print("Resetting ESP32-S3 helper...")
                reset_esp32_to_app(ser)

            with open(log_path, "ab") as log:
                expected_size = wait_for_begin(ser, baud, log, resume_at)
                if os.path.exists(part_path) and os.path.getsize(part_path) != resume_at:
                    raise RuntimeError("Partial dump changed before capture started; run again.")

            with open(log_path, "ab") as log, open(part_path, "ab") as out:
                bytes_written = resume_at
                started_at = time.time() - (resume_at / 75000.0 if resume_at else 0)
                last_progress = 0.0
                last_data = time.time()
                end_errors = None

                while True:
                    if time.time() - last_data > IDLE_TIMEOUT_S:
                        raise TimeoutError(f"No dump data for {IDLE_TIMEOUT_S}s. Kept partial file for resume.")

                    raw = ser.readline()
                    if not raw:
                        continue
                    log.write(raw)
                    line = raw.strip()
                    if not line:
                        continue

                    end_match = end_re.search(line)
                    if end_match:
                        print()
                        print(line.decode("ascii", errors="replace"))
                        end_errors = int(end_match.group(1))
                        break

                    abort_match = abort_re.search(line)
                    if abort_match:
                        print()
                        print(line.decode("ascii", errors="replace"))
                        raise RuntimeError(line.decode("ascii", errors="replace"))

                    try:
                        text = line.decode("ascii")
                    except UnicodeDecodeError:
                        continue
                    if text.startswith(("RETRY", "RECONNECT", "SLOW_LINK")):
                        last_data = time.time()
                        print()
                        print(text)
                        continue

                    if not b64_re.match(line):
                        continue

                    try:
                        data = base64.b64decode(line, validate=True)
                    except binascii.Error:
                        continue

                    if len(data) != CHUNK:
                        raise RuntimeError(f"Decoded chunk is {len(data)} bytes, expected {CHUNK}")

                    out.write(data)
                    out.flush()
                    bytes_written += len(data)
                    last_data = time.time()

                    now = time.time()
                    if now - last_progress >= 0.25 or bytes_written >= expected_size:
                        progress(bytes_written, expected_size, started_at)
                        last_progress = now

                if end_errors != 0:
                    raise RuntimeError(f"Firmware reported errors={end_errors}")
                if bytes_written != expected_size:
                    raise RuntimeError(f"Dump size is {bytes_written}, expected {expected_size}")

            os.replace(part_path, final_path)
            digest = sha256_file(final_path)
            with open(sha_path, "w", encoding="ascii") as f:
                f.write(f"{digest}  {os.path.basename(final_path)}\n")
            print(f"Saved binary: {final_path}")
            print(f"Saved serial log: {log_path}")
            print(f"Saved SHA256: {sha_path}")
            print(f"SHA256: {digest}")
            return 0

        except Exception as exc:
            last_error = exc
            print(f"Attempt at {baud} baud failed: {exc}")
        finally:
            if ser:
                ser.close()

    print(f"ERROR: {last_error}")
    print(f"Partial dump kept at: {part_path}")
    return 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=0)
    parser.add_argument("--expected-size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--no-reset", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    return capture(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
### PYTHON_CAPTURE_END
