#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
yoto_write_checked.py - write flash to a Yoto Mini with esptool, but only after
checking that the ESP32 is actually powering its flash at 3.3 V.

Why: the ESP32 picks the flash/PSRAM supply (VDD_SDIO) from the GPIO12 (MTDI)
strap at every reset. On the Yoto Mini, GPIO12 can be held HIGH by the board
itself after the Yoto app has run (most likely the SD card's pull-ups while it
is powered; a reset through EN does not cut that power). The ESP32 then
supplies the flash and PSRAM with 1.8 V instead of 3.3 V:

  * flash erase/write stops mid-way ("Serial data stream stopped"),
  * the app aborts at boot with "Failed to init external RAM" and reboots in a
    loop (ROM line shows boot:0x3f instead of the normal boot:0x13).

Nothing is damaged. The fix is a FULL power cycle of the Yoto (battery out, and
any 3V3 feed from the flasher unplugged, ~10 s), after which the strap reads low
again.

This script:
  1. resets the Yoto into download mode and runs esptool flash-id,
  2. refuses to write unless it reports "Flash voltage ... 3.3V" and the
     expected flash size (default 8MB), leaving the Yoto parked in download
     mode so it cannot boot-loop,
  3. otherwise runs esptool write-flash with your arguments, without another
     reset, so the verified state is the one that gets written.

Usage (esptool must be installed in the Python that runs this script):
    python tools/yoto_write_checked.py -p /dev/cu.usbmodemXXXX --check-only
    python tools/yoto_write_checked.py -p /dev/cu.usbmodemXXXX -b 460800 -- \\
        0x2c0000 ota_0.bin 0x540000 ota_1.bin --diff-with old_ota_0.bin old_ota_1.bin

Everything after "--" is passed to `esptool write-flash` unchanged.
"""

import argparse
import re
import subprocess
import sys


def esptool(args, timeout=600):
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32"] + args
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def main():
    ap = argparse.ArgumentParser(description="esptool write-flash for a Yoto Mini, gated on a 3.3 V flash supply.")
    ap.add_argument("-p", "--port", required=True, help="serial port of the flasher / bridge")
    ap.add_argument("-b", "--baud", default="460800", help="baud for the write (default 460800)")
    ap.add_argument("--flash-size", default="8MB", help="expected flash size (default 8MB)")
    ap.add_argument("--after", default="hard-reset", choices=["hard-reset", "no-reset"],
                    help="what esptool does after the write (default hard-reset = boot the Yoto)")
    ap.add_argument("--check-only", action="store_true",
                    help="only run the check; on success reset the Yoto back to normal run")
    ap.add_argument("write_args", nargs=argparse.REMAINDER,
                    help="after '--': arguments for esptool write-flash")
    args = ap.parse_args()
    wargs = args.write_args[1:] if args.write_args[:1] == ["--"] else args.write_args
    if not args.check_only and not wargs:
        ap.error("nothing to write: pass write-flash arguments after '--', or use --check-only")

    print("[check] resetting the Yoto into download mode and reading the flash ID ...")
    rc, out = esptool(["-p", args.port, "-b", "115200", "--after", "no-reset", "flash-id"], timeout=120)
    volt = re.search(r"Flash voltage[^\n]*?(\d\.\dV)", out)
    size = re.search(r"Detected flash size:\s*(\S+)", out)
    volt_s = volt.group(1) if volt else "not reported"
    size_s = size.group(1) if size else "not reported"
    print(f"[check] flash voltage: {volt_s}   flash size: {size_s}")

    if rc != 0 or volt_s != "3.3V" or size_s != args.flash_size:
        print("\nREFUSED: the Yoto's flash is not in a safe state to write.", file=sys.stderr)
        if volt_s == "1.8V":
            print("  The ESP32 is supplying its flash at 1.8 V (GPIO12 strap read high).\n"
                  "  Power-cycle the Yoto completely: battery out and any 3V3 feed unplugged,\n"
                  "  wait ~10 s, reconnect, then run this again.", file=sys.stderr)
        elif rc != 0:
            print("  esptool could not talk to the Yoto:\n" + "\n".join(
                "    " + l for l in out.strip().splitlines()[-4:]), file=sys.stderr)
        print("  Nothing was written. The Yoto is left in download mode.", file=sys.stderr)
        return 3

    if args.check_only:
        rc, out = esptool(["-p", args.port, "--before", "no-reset", "--after", "hard-reset", "chip-id"], timeout=60)
        print("[check] OK. Yoto reset back to normal run." if rc == 0 else
              "[check] OK, but the final reset failed; press the bridge's BOOT button (long press).")
        return 0

    print(f"[write] esptool write-flash {' '.join(wargs)}")
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32", "-p", args.port, "-b", args.baud,
           "--before", "no-reset", "--after", args.after, "write-flash"] + wargs
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
