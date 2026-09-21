#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
yoto_patch.py - patch a *self-dumped* Yoto Mini firmware image.

This operates ONLY on a full 8 MB flash image that you dumped from your own
device (see tools/yoto-flasher). It never touches a device directly, and it is
never distributed with anyone else's firmware: Yoto's firmware is proprietary,
and a real dump contains YOUR device's Wi-Fi credentials and account token.
Keep your dumps private.

Two functional patches, both applied inside the app partition:

  1. MADCTL 0x48 -> 0x00  (target value configurable via --madctl)
     The stock firmware initialises its panel with MADCTL (memory access
     control register 0x36) = 0x48 = MX(column mirror) | BGR. That is correct
     for the original GC9306-class panel's wiring, but a generic ST7789
     replacement panel then shows a mirrored image with red/blue swapped.
     Setting MADCTL to 0x00 (no mirror, RGB order) makes a stock ST7789 look
     correct; other MX/MV/BGR orientations are available via --madctl if your
     panel needs them. The value is not stored as a data byte; it is an
     immediate in a `movi.n` instruction, so we match a code signature and
     rewrite the immediate (e.g. bytes 4c 8b -> 0c 0b for 0x48 -> 0x00).

  2. Version string vX.Y.Z -> v9.Y.Z (OPTIONAL, OFF by default, NOT recommended)
     The intuition is that reporting a huge version stops OTA updates. In
     practice on the Yoto cloud it BACKFIRES: the server does not do
     "update only if newer" - it force-pushes the firmware it expects when the
     device reports an unexpected version, so a spoofed v9.x.x triggered a
     "repair" push that reverted the patch after ~1 day. Reporting the REAL
     version is safer. See docs/FIRMWARE_DUMP_AND_PATCH.md for the reliable way
     to keep a patch (patch every slot; advanced: disable the OTA write path).
     This flag is kept only to reproduce the historical v1 image.

After editing bytes inside an ESP-IDF app image we must "re-sign" it, i.e.
recompute the 1-byte image checksum and the 32-byte SHA-256 that the ROM/
bootloader verifies, or the device refuses to boot the slot.

IMPORTANT - these signatures were derived from firmware v2.23.4. The tool
VERIFIES every byte before writing and refuses if anything does not match, so
running it against a different firmware version fails safely (it will not
blindly write to an offset). If it refuses, your firmware differs and the
signatures need re-deriving; do not force it.
"""

import argparse
import hashlib
import re
import struct
import sys

FLASH_SIZE = 8 * 1024 * 1024
PT_OFFSET = 0x8000          # standard ESP-IDF partition table location
PT_MAGIC = b"\xaa\x50"
ESP_IMAGE_MAGIC = 0xE9
ESP_CHECKSUM_MAGIC = 0xEF

# --- Patch signatures (firmware v2.23.4). Verified before every write. --------
# 16-byte code signature around the MADCTL init; the two immediate bytes to
# rewrite sit at MADCTL_PATCH_OFF within the match.
MADCTL_SIG = bytes.fromhex("960d3c6bad0225a6ffa033204c8bad02")
MADCTL_PATCH_OFF = 12
MADCTL_FROM = b"\x4c\x8b"    # movi.n a11, 0x48  (stock value the signature matches)

# The MADCTL value is the immediate of a `movi.n a11, <imm>` instruction. That
# encoding can only hold 0x00-0x5F, so the reachable MADCTL values are the
# MX/MV/BGR combinations below (byte pairs empirically confirmed). A vertical
# flip (MY, 0x80/0xC0) does NOT fit a movi.n immediate and would need a larger
# code patch, so it is intentionally not offered here.
#   bit6 MX (mirror X) | bit5 MV (swap X/Y) | bit3 BGR (red<->blue)
MADCTL_ENCODINGS = {
    0x00: b"\x0c\x0b",   # no mirror, RGB              (default: fixes MX+BGR panel)
    0x08: b"\x0c\x8b",   # BGR only
    0x20: b"\x2c\x0b",   # MV (swap X/Y)
    0x28: b"\x2c\x8b",   # MV + BGR
    0x40: b"\x4c\x0b",   # MX (mirror X)
    0x48: b"\x4c\x8b",   # MX + BGR                    (stock; no-op)
    0x60: b"\x6c\x0b",   # MX + MV
    0x68: b"\x6c\x8b",   # MX + MV + BGR
}

# The version string as stored (NUL-terminated); the major digit is at +1.
VERSION_SIG = b"v2.23.4\x00"
VERSION_MAJOR_OFF = 1


class PatchError(Exception):
    pass


def parse_partitions(img):
    parts = []
    for i in range(0, 0x1000, 32):
        e = img[PT_OFFSET + i:PT_OFFSET + i + 32]
        if e[:2] != PT_MAGIC:
            break
        off, size = struct.unpack("<II", e[4:12])
        label = e[12:28].split(b"\x00")[0].decode("ascii", "replace")
        parts.append(dict(label=label, type=e[2], subtype=e[3], off=off, size=size))
    if not parts:
        raise PatchError("no partition table found at 0x8000 - not a Yoto flash image?")
    return parts


def app_partitions(parts):
    # type 0x00 = app. factory (sub 0x00) + ota_0 (0x10) + ota_1 (0x11) ...
    return [p for p in parts if p["type"] == 0x00]


def active_ota_index(img, parts):
    """Return the active OTA slot index (0-based), or None if undetermined."""
    otadata = next((p for p in parts if p["type"] == 1 and p["subtype"] == 0), None)
    n_ota = len([p for p in parts if p["type"] == 0 and p["subtype"] >= 0x10])
    if not otadata or n_ota == 0:
        return None
    best = -1
    for i in range(2):  # otadata holds two 4 KB sectors, each starting with ota_seq
        seq = struct.unpack("<I", img[otadata["off"] + i * 0x1000:otadata["off"] + i * 0x1000 + 4])[0]
        if seq != 0xFFFFFFFF and seq > best:
            best = seq
    if best < 0:
        return None
    return (best - 1) % n_ota


def image_regions(img, base):
    """Return (checksum_byte_pos, sha256_pos) for the ESP-IDF app image at base."""
    if img[base] != ESP_IMAGE_MAGIC:
        raise PatchError(f"partition @{base:#08x} is not an ESP image (magic {img[base]:#04x})")
    hash_appended = img[base + 23]
    seg_count = img[base + 1]
    off = base + 24
    for _ in range(seg_count):
        _, data_len = struct.unpack("<II", img[off:off + 8])
        off += 8 + data_len
    length = off - base
    pad = (16 - ((length + 1) % 16)) % 16
    checksum_pos = off + pad
    sha_pos = checksum_pos + 1 if hash_appended else None
    return checksum_pos, sha_pos


def resign(img, base):
    """Recompute the 1-byte image checksum and appended SHA-256 in place."""
    seg_count = img[base + 1]
    off = base + 24
    checksum = ESP_CHECKSUM_MAGIC
    for _ in range(seg_count):
        _, data_len = struct.unpack("<II", img[off:off + 8])
        for b in img[off + 8:off + 8 + data_len]:
            checksum ^= b
        off += 8 + data_len
    checksum_pos, sha_pos = image_regions(img, base)
    img[checksum_pos] = checksum
    if sha_pos is not None:
        img[sha_pos:sha_pos + 32] = hashlib.sha256(bytes(img[base:sha_pos])).digest()
    return checksum_pos, sha_pos


def find_unique(haystack, needle, what):
    hits = [m.start() for m in re.finditer(re.escape(needle), haystack)]
    if len(hits) == 0:
        raise PatchError(f"{what}: signature not found (firmware version mismatch?)")
    if len(hits) > 1:
        raise PatchError(f"{what}: signature matched {len(hits)}x, expected exactly 1 (ambiguous)")
    return hits[0]


def patch_partition(img, part, do_madctl, madctl_value, do_version, major):
    base, size = part["off"], part["size"]
    seg = bytes(img[base:base + size])
    changes = []

    if do_madctl:
        rel = find_unique(seg, MADCTL_SIG, "MADCTL")
        p = base + rel + MADCTL_PATCH_OFF
        if bytes(img[p:p + 2]) != MADCTL_FROM:
            raise PatchError(f"MADCTL: bytes at {p:#08x} are {img[p:p+2].hex()}, expected {MADCTL_FROM.hex()}")
        img[p:p + 2] = MADCTL_ENCODINGS[madctl_value]
        changes.append((f"MADCTL 0x48->{madctl_value:#04x}", p))

    if do_version:
        rel = find_unique(seg, VERSION_SIG, "version string")
        p = base + rel + VERSION_MAJOR_OFF
        old = chr(img[p])
        img[p:p + 1] = major.encode("ascii")
        changes.append((f"version v{old}.x.x -> v{major}.x.x", p))

    if changes:
        cpos, spos = resign(img, base)
        changes.append(("checksum recomputed", cpos))
        if spos is not None:
            changes.append(("sha-256 recomputed", spos))
    return changes


def main():
    ap = argparse.ArgumentParser(description="Patch a self-dumped Yoto Mini firmware image (MADCTL + OTA version).")
    ap.add_argument("input", help="8 MB flash image dumped from YOUR device")
    ap.add_argument("-o", "--output", help="output image (default: <input>.patched.bin)")
    ap.add_argument("--slots", choices=["active", "all"], default="active",
                    help="which app slot(s) to patch (default: active boot slot only; "
                         "use 'all' so a slot switch still shows correct colors)")
    ap.add_argument("--no-madctl", action="store_true", help="do not apply the MADCTL display fix")
    ap.add_argument("--madctl", default="0x00",
                    help="target MADCTL value (default 0x00 = no mirror, RGB). Reachable: "
                         "0x00,0x08,0x20,0x28,0x40,0x48,0x60,0x68 (MX/MV/BGR combos). "
                         "A vertical flip (MY) can't be done with this single-instruction patch.")
    ap.add_argument("--spoof-version", action="store_true",
                    help="bump the reported version to v<major>.x.x. NOT recommended: on the Yoto "
                         "cloud this backfires and triggers a repair push (see docstring). Off by default.")
    ap.add_argument("--major", default="9", help="major version digit for --spoof-version (default: 9)")
    ap.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = ap.parse_args()

    if len(args.major) != 1 or not args.major.isdigit():
        ap.error("--major must be a single digit 0-9")
    try:
        madctl_value = int(args.madctl, 16)
    except ValueError:
        ap.error("--madctl must be a hex value like 0x00")
    if madctl_value not in MADCTL_ENCODINGS:
        ap.error("--madctl must be one of: " + ", ".join(f"{v:#04x}" for v in sorted(MADCTL_ENCODINGS)))

    with open(args.input, "rb") as f:
        img = bytearray(f.read())
    if len(img) != FLASH_SIZE:
        print(f"ERROR: input is {len(img)} bytes, expected {FLASH_SIZE} (a full 8 MB dump).", file=sys.stderr)
        return 2
    if img[0x1000] != ESP_IMAGE_MAGIC:
        print(f"ERROR: byte @0x1000 = {img[0x1000]:#04x}, expected 0xe9 (bootloader magic).", file=sys.stderr)
        return 2

    try:
        parts = parse_partitions(img)
    except PatchError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    apps = app_partitions(parts)
    print(f"MD5 in : {hashlib.md5(img).hexdigest()}")
    print("Partitions:")
    for p in parts:
        print(f"  {p['label']:12s} type={p['type']:#04x} sub={p['subtype']:#04x} "
              f"off={p['off']:#08x} size={p['size']:#08x}")

    if args.slots == "all":
        targets = apps
    else:
        idx = active_ota_index(img, parts)
        ota = [p for p in apps if p["subtype"] >= 0x10]
        if idx is None or idx >= len(ota):
            print("ERROR: could not determine the active OTA slot; use --slots all to patch every slot.",
                  file=sys.stderr)
            return 3
        targets = [ota[idx]]
    print(f"\nTarget slot(s): {', '.join(p['label'] for p in targets)}")

    # 'active' mode is strict (the booted slot must patch). 'all' mode is
    # best-effort: slots that are a different firmware build won't match the
    # v2.23.4 signature, so warn and skip them rather than failing outright.
    patched = 0
    for p in targets:
        try:
            changes = patch_partition(img, p, not args.no_madctl, madctl_value,
                                       args.spoof_version, args.major)
            for desc, at in changes:
                print(f"  [{p['label']}] {desc} @ {at:#08x}")
            patched += 1
        except PatchError as e:
            if args.slots == "all":
                print(f"  [{p['label']}] SKIPPED: {e}", file=sys.stderr)
                continue
            print(f"\nREFUSED: {e}", file=sys.stderr)
            print("Nothing was written. This usually means your firmware version differs from\n"
                  "v2.23.4; re-derive the signatures rather than forcing the patch.", file=sys.stderr)
            return 4
    if patched == 0:
        print("\nREFUSED: no slot matched the known signatures (firmware version mismatch?).",
              file=sys.stderr)
        return 4

    print(f"\nMD5 out: {hashlib.md5(img).hexdigest()}")
    if args.dry_run:
        print("(dry run - no file written)")
        return 0

    out = args.output or (args.input.rsplit(".", 1)[0] + ".patched.bin")
    with open(out, "wb") as f:
        f.write(img)
    print(f"Wrote {out}")
    print("Flash it back with tools/yoto-flasher (run_yoto_write.bat). Keep the dump/patched images private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
