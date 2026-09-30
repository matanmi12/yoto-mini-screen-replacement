#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
yoto_patch.py - patch a *self-dumped* Yoto Mini firmware image.

This operates ONLY on a full 8 MB flash image that you dumped from your own
device (see tools/yoto-flasher). It never touches a device directly, and it is
never distributed with anyone else's firmware: Yoto's firmware is proprietary,
and a real dump contains YOUR device's Wi-Fi credentials and account token.
Keep your dumps private.

What it can change (all inside the display init routine of the app slot,
gc9306_display_init @ 0x4010b1f8 in v2.23.4; see docs/COLOR_CALIBRATION.md):

  1. MADCTL (0x36) value, default 0x48 -> 0x00      (--madctl)
     Stock 0x48 = MX | BGR, correct for the original GC9306-class panel; a
     stock ST7789 then shows a mirrored image with red/blue swapped. The value
     is the immediate of a `movi.n` instruction, so only 0x00-0x5F (the
     MX/MV/BGR combinations) are reachable.

  2. Display inversion INVON (0x21) <-> INVOFF (0x20)   (--inversion)
     The stock init sends INVON. Also a movi.n immediate.

  3. ST7789 analog/gamma registers, injected into "spare" init writes
     (--gamma-profile, --vcom, --display-param)
     The stock init sends many GC9306-only vendor registers (A4..AF, E3, E8,
     FF, F0..F5). A replacement ST7789 does not implement those commands and
     ignores them, so it runs on its OWN power-on defaults for gamma, VCOM and
     drive voltages - the Yoto never programs them. Four of those ignored
     one-parameter writes (0xAF, 0xAD, 0xAE, 0xAC) are "spare slots": their
     command byte and data byte are both instruction immediates, so each can be
     re-pointed at ONE single-parameter ST7789 register (GAMSET, VCOMS, VRHS,
     ...) without moving any code. Only an allowlist of registers is accepted.

  4. Full ST7789 gamma curves PVGAMCTRL(0xE0)+NVGAMCTRL(0xE1)   (--gamma-curve)
     The stock init never programs the ST7789 gamma curves, so a replacement
     panel runs its factory-default gamma (~2.2), much darker in the midtones
     than the original Yoto panel. The GC9306 F0..F5 gamma writes (ignored by an
     ST7789) occupy a 510-byte code region that is rewritten IN PLACE into two
     real E0/E1 transactions (14 params each) plus a jump over the tail. This is
     the confirmed fix; `--experiment recommended` applies it with the rest.

  5. Version string vX.Y.Z -> v9.Y.Z  (--spoof-version; OFF by default, NOT
     recommended: the Yoto cloud treats it as corruption and force-pushes a
     "repair" that reverts the patch. Kept only to reproduce the historical
     v1 image.)

Every managed site is located by a code signature whose fixed bytes must match
exactly and whose variable bytes must decode to a known value (stock, or a
value this tool writes). So the tool accepts a stock v2.23.4 slot OR one it
patched before, and each output is always:

    stock v2.23.4 display init  +  exactly the options you passed.

Options you do NOT pass are restored to stock (spare slots back to their
GC9306 values, inversion back to INVON); MADCTL defaults to 0x00 as before.
Anything outside the managed sites (e.g. an OTA-write block you applied by
hand) is left untouched. Anything it does not recognise makes it refuse.

After editing bytes inside an ESP-IDF app image we must "re-sign" it, i.e.
recompute the 1-byte image checksum and the 32-byte SHA-256 that the ROM/
bootloader verifies, or the device refuses to boot the slot.

IMPORTANT - these signatures were derived from firmware v2.23.4. On any other
version the signatures do not match and the tool refuses (or, with
--slots all, skips that slot). Do not force it.
"""

import argparse
import hashlib
import json
import os
import re
import struct
import sys

FLASH_SIZE = 8 * 1024 * 1024
PT_OFFSET = 0x8000          # standard ESP-IDF partition table location
PT_MAGIC = b"\xaa\x50"
ESP_IMAGE_MAGIC = 0xE9
ESP_CHECKSUM_MAGIC = 0xEF


# --- Xtensa immediate encodings ---------------------------------------------
# `movi.n a11, imm` (2 bytes) : [ (imm>>4)<<4 | 0xC , (imm&0xF)<<4 | 0xB ],
#                               positive range 0x00-0x5F only.
# `movi   a11, imm` (3 bytes) : [ 0xB2, 0xA0, imm ] for imm 0x00-0xFF.
def enc_movin(imm):
    if not 0 <= imm <= 0x5F:
        raise ValueError(f"{imm:#04x} does not fit a movi.n immediate (0x00-0x5F)")
    return bytes([((imm >> 4) << 4) | 0x0C, ((imm & 0x0F) << 4) | 0x0B])


def dec_movin(b):
    if len(b) != 2 or (b[0] & 0x0F) != 0x0C or (b[1] & 0x0F) != 0x0B or (b[0] >> 4) > 5:
        return None
    return ((b[0] >> 4) << 4) | (b[1] >> 4)


# --- Managed sites -----------------------------------------------------------
# Each site is located by a code signature: fixed bytes that must match exactly,
# plus masked "variable" bytes (the immediates this tool reads/writes). The
# pattern must be unique in the app partition.
#
# The signatures are NOT tied to one firmware version number. They contain only
# the register-write instruction bytes and the *nearby* call displacements to
# the SPI write_cmd/write_data helpers - all of which sit in the same compiled
# object as the init routine, so they stay byte-identical when the firmware is
# merely rebuilt/relinked at a different address (verified identical across
# v2.23.2 and v2.23.4). Far calls (e.g. to the GPIO/DC helper), whose
# displacement does shift between builds, are deliberately excluded. If a build
# actually changes the display-init CODE, a signature stops matching and the
# tool refuses rather than writing blind - which is the intended safety net.

# MADCTL: movi.n a11,0x36 ; mov.n a10,a2 ; call8 write_cmd ; or a3,a3,a10 ;
#         movi.n a11,<MADCTL> ; mov.n a10,a2
MADCTL_PREFIX = bytes.fromhex("3c6bad0225a6ffa03320")
MADCTL_SUFFIX = bytes.fromhex("ad02")
# bit6 MX (mirror X) | bit5 MV (swap X/Y) | bit3 BGR (red<->blue). MY (0x80)
# does not fit a movi.n immediate, so a vertical flip is not offered.
MADCTL_VALUES = (0x00, 0x08, 0x20, 0x28, 0x40, 0x48, 0x60, 0x68)
MADCTL_STOCK = 0x48

# INVON/INVOFF: movi.n a11,<0x21|0x20> ; mov.n a10,a2 ; call8 write_cmd ; or a3..
INV_PREFIX = b""
INV_SUFFIX = bytes.fromhex("ad02a558ffa03320")
INV_VALUES = {"on": 0x21, "off": 0x20}      # INVON / INVOFF command bytes
INV_STOCK = "on"

# Spare slots: movi a11,<cmd> ; mov.n a10,a2 ; call8 write_cmd ; or a3,a3,a10 ;
#              <data movi> ; mov.n a10,a2 ; call8 write_data ; or a3,a3,a10
# Each: (name, stock_cmd, stock_data, data_encoding, wc_call, wd_call)
#   pattern = b2 a0 <cmd> ad 02 <wc_call> a03320 <DATA> ad 02 <wd_call> a03320
#   DATA    = b2 a0 <byte>   (wide, any 0x00-0xFF)  |  <movi.n 2 bytes> (<=0x5F)
SPARE_SLOTS = [
    ("AF", 0xAF, 0x77, "wide",  bytes.fromhex("e58aff"), bytes.fromhex("6583ff")),
    ("AD", 0xAD, 0x33, "movin", bytes.fromhex("258dff"), bytes.fromhex("a585ff")),
    ("AE", 0xAE, 0x2B, "movin", bytes.fromhex("6599ff"), bytes.fromhex("e591ff")),
    ("AC", 0xAC, 0x00, "movin", bytes.fromhex("258fff"), bytes.fromhex("e587ff")),
]
_OR_A3 = bytes.fromhex("a03320")

# PWCTRL2 (E8h). Unlike the other GC9306 vendor writes, E8 IS a real ST7789
# command: Power Control 2 (source / AVDD-AVCL booster clocks, one parameter,
# default 0x93). The Yoto writes E8 = 0x11, 0x0B, which on an ST7789 sets
# PWCTRL2 = 0x11: it clears bit 7 (fixed 1 in the datasheet) and changes the
# AVDD/AVCL booster clock. "default" re-points this write at STE (44h) with
# the Yoto's own later STE parameters (0x00, 0x0A): a documented two-parameter
# command whose value the init rewrites identically a few writes later, so the
# net effect is only that PWCTRL2 keeps its ST7789 default.
#   pattern = b2 a0 <cmd> ad 02 <call write_cmd> a03320 <movi.n d1> ad 02
#             <call write_data> a03320 <movi.n d2> ad 02 <call write_data> a03320
E8_WC, E8_WD1, E8_WD2 = bytes.fromhex("2597ff"), bytes.fromhex("a58fff"), bytes.fromhex("258fff")
PWCTRL2_STATES = {"yoto": (0xE8, 0x11, 0x0B), "default": (0x44, 0x00, 0x0A)}
PWCTRL2_STOCK = "yoto"

# ST7789 registers that take exactly ONE parameter and are safe to inject.
# (name, meaning, validator, ST7789 power-on default)  - ST7789VW datasheet.
# LCMCTRL (C0h) is deliberately excluded: its XBGR/XINV/XMX bits XOR the
# MADCTL/inversion settings. Multi-parameter registers (PVGAMCTRL E0h /
# NVGAMCTRL E1h, 14 bytes each; digital-gamma LUTs E2h/E3h, 64 bytes) cannot
# be reached by this immediate-only mechanism.
ST7789_REGS = {
    0x26: ("GAMSET",   "gamma curve select (01=G2.2 02=G1.8 04=G2.5 08=G1.0)",
           lambda v: v in (0x01, 0x02, 0x04, 0x08), 0x01),
    0xB7: ("GCTRL",    "gate VGH/VGL voltages (0VVV0LLL)",
           lambda v: (v & 0x88) == 0, 0x35),
    0xBB: ("VCOMS",    "VCOM voltage, 0.1 V + 0.025 V/step (00-3F)",
           lambda v: 0x00 <= v <= 0x3F, 0x20),
    0xC3: ("VRHS",     "VRH: GVDD/VAP gamma reference voltage (00-27)",
           lambda v: 0x00 <= v <= 0x27, 0x0B),
    0xC4: ("VDVS",     "VDV voltage (00-3F)",
           lambda v: 0x00 <= v <= 0x3F, 0x20),
    0xC5: ("VCMOFSET", "VCOM offset (00-3F)",
           lambda v: 0x00 <= v <= 0x3F, 0x20),
    0xC6: ("FRCTRL2",  "frame rate in normal mode, RTNA only (00-1F)",
           lambda v: 0x00 <= v <= 0x1F, 0x0F),
}

GAMMA_PROFILES = {"2.2": 0x01, "1.8": 0x02, "2.5": 0x04, "1.0": 0x08}

# Full ST7789 gamma curves for --gamma-curve. The stock firmware never programs
# PVGAMCTRL(0xE0)/NVGAMCTRL(0xE1), so a replacement ST7789 runs its own default
# gamma (~2.2), which is much darker in the midtones than the original panel.
# Injecting a real curve is the fix. The GC9306 vendor gamma writes F0..F5 are
# ignored by an ST7789, so their code region is rewritten in place into two
# proper E0/E1 transactions (see rewrite_gamma_region). 14 params each.
GAMMA_CURVES = {
    # Widely-used ST7789 curve (Bodmer/TFT_eSPI default); the one confirmed on
    # hardware to match the original Yoto panel closely.
    "tft": ([0xd0, 0x00, 0x02, 0x07, 0x0a, 0x28, 0x32, 0x44, 0x42, 0x06, 0x0e, 0x12, 0x14, 0x17],
            [0xd0, 0x00, 0x02, 0x07, 0x0a, 0x28, 0x31, 0x54, 0x47, 0x0e, 0x1c, 0x17, 0x1b, 0x1e]),
}

# --- Gamma region layout (the six GC9306 F0..F5 writes) ----------------------
# Each F-write is: [CS-pulse 14B][cmd write 11B][6x data write 10B] = 85 bytes,
# and all six are consecutive, so the region is 6*85 = 510 bytes. Each F command
# byte 0xF0..0xF5 sits 14 bytes into its block. Located by those six markers so
# it is not tied to a fixed address (version-agnostic, same as the other sites).
GAMMA_FBLOCK = 85
GAMMA_NBLOCKS = 6
GAMMA_REGION_LEN = GAMMA_FBLOCK * GAMMA_NBLOCKS      # 510
GAMMA_CMD_IN_BLOCK = 14
CS_PULSE = bytes.fromhex("a842")                     # l32i.n a10,[a2+0x10]  (start of a CS pulse)
MOV_A10_A2 = bytes.fromhex("ad02")
OR_A3_A3_A10 = bytes.fromhex("a03320")

# First-round, one-variable-at-a-time experiments. MADCTL stays 0x00 in all.
EXPERIMENTS = {
    "baseline":      dict(inversion=None, regs=[],
                          purpose="current known-good build (MADCTL 0x00 only); the reference for every photo"),
    "inversion_on":  dict(inversion="on", regs=[],
                          purpose="explicit INVON control; byte-identical to baseline because stock already sends INVON"),
    "inversion_off": dict(inversion="off", regs=[],
                          purpose="diagnostic: IPS ST7789 should show a colour negative; proves inversion is not the colour cause"),
    "gamma_A":       dict(inversion=None, regs=[(0x26, 0x02)],
                          purpose="GAMSET G1.8: lighter midtones; tests 'orange looks brown = midtones too dark'"),
    "gamma_B":       dict(inversion=None, regs=[(0x26, 0x04)],
                          purpose="GAMSET G2.5: darker midtones; control, should make brown worse"),
    "gamma_C":       dict(inversion=None, regs=[(0x26, 0x08)],
                          purpose="GAMSET G1.0: near-linear; bracket, strong midtone lift"),
    "vcom_A":        dict(inversion=None, regs=[(0xBB, 0x1A)],
                          purpose="VCOMS 0.75 V (default 0.90 V): shadow tint / flicker / contrast"),
    "vcom_B":        dict(inversion=None, regs=[(0xBB, 0x28)],
                          purpose="VCOMS 1.10 V (common vendor value)"),
    "power_A":       dict(inversion=None, regs=[(0xC3, 0x12)],
                          purpose="VRHS 0x12 = GVDD 4.45 V (default 0x0B = 4.10 V): gamma reference / contrast"),
    "e8_default":    dict(inversion=None, regs=[], pwctrl2="default",
                          purpose="stop the Yoto's E8 write so ST7789 PWCTRL2 (source booster clocks) keeps its default 0x93"),
    "gamma_tft":     dict(inversion=None, regs=[], gamma="tft",
                          purpose="inject full ST7789 E0/E1 gamma curves: the fix for the dark midtones"),
    "recommended":   dict(inversion=None, regs=[(0xC3, 0x12)], pwctrl2="default", gamma="tft",
                          purpose="the full confirmed calibration: MADCTL 0x00 + E8 default + VRHS 0x12 + E0/E1 gamma"),
}

# The reported version is a plain "vMAJOR.MINOR.PATCH\0" string in the slot's
# read-only data (the ESP app-descriptor version field is empty on these
# builds). Detected generically; not hard-coded to any release.
VERSION_RE = re.compile(rb"v(\d+)\.(\d+)\.(\d+)\x00")
BOOTLOADER_VERSION = "1.0.1"   # second-stage bootloader string also present in each slot; skip it


class PatchError(Exception):
    pass


# --- Image structure -----------------------------------------------------------
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


def find_unique_masked(haystack, prefix, var_len, suffix, what):
    pat = re.escape(prefix) + b"(.{%d})" % var_len + re.escape(suffix)
    hits = [m.start() for m in re.finditer(pat, haystack, re.DOTALL)]
    if len(hits) == 0:
        raise PatchError(f"{what}: signature not found (firmware version mismatch?)")
    if len(hits) > 1:
        raise PatchError(f"{what}: signature matched {len(hits)}x, expected exactly 1 (ambiguous)")
    return hits[0] + len(prefix)          # offset of the variable bytes


# --- Reading the current display config of a slot ----------------------------
def locate_sites(seg):
    """Find every managed site in an app partition and decode its current value.
    Raises PatchError if any site is missing, ambiguous, or holds an unknown value."""
    sites = {}

    rel = find_unique_masked(seg, MADCTL_PREFIX, 2, MADCTL_SUFFIX, "MADCTL")
    val = dec_movin(seg[rel:rel + 2])
    if val not in MADCTL_VALUES:
        raise PatchError(f"MADCTL: immediate {seg[rel:rel+2].hex()} is not a known value")
    sites["madctl"] = dict(rel=rel, len=2, value=val)

    rel = find_unique_masked(seg, INV_PREFIX, 2, INV_SUFFIX, "INVON/INVOFF")
    val = dec_movin(seg[rel:rel + 2])
    mode = {v: k for k, v in INV_VALUES.items()}.get(val)
    if mode is None:
        raise PatchError(f"inversion: immediate {seg[rel:rel+2].hex()} is neither INVON nor INVOFF")
    sites["inversion"] = dict(rel=rel, len=2, value=mode)

    for name, stock_cmd, stock_data, enc, wc, wd in SPARE_SLOTS:
        data_grp = b"\xb2\xa0(.)" if enc == "wide" else b"(.{2})"
        pat = (b"\xb2\xa0(.)\xad\x02" + re.escape(wc) + _OR_A3
               + data_grp + b"\xad\x02" + re.escape(wd) + _OR_A3)
        hits = [m for m in re.finditer(pat, seg, re.DOTALL)]
        if len(hits) != 1:
            raise PatchError(f"spare slot {name}: signature matched {len(hits)}x, expected exactly 1")
        m = hits[0]
        cmd = m.group(1)[0]
        data = m.group(2)[0] if enc == "wide" else dec_movin(m.group(2))
        if data is None:
            raise PatchError(f"spare slot {name}: data immediate {m.group(2).hex()} is not a movi.n")
        if cmd != stock_cmd and cmd not in ST7789_REGS:
            raise PatchError(f"spare slot {name}: command {cmd:#04x} is neither stock nor a known ST7789 register")
        sites[f"slot_{name}"] = dict(cmd_rel=m.start(1), data_rel=m.start(2), enc=enc,
                                     stock_cmd=stock_cmd, stock_data=stock_data,
                                     cmd=cmd, data=data)

    pat = (b"\xb2\xa0(.)\xad\x02" + re.escape(E8_WC) + _OR_A3
           + b"(.{2})\xad\x02" + re.escape(E8_WD1) + _OR_A3
           + b"(.{2})\xad\x02" + re.escape(E8_WD2) + _OR_A3)
    hits = list(re.finditer(pat, seg, re.DOTALL))
    if len(hits) != 1:
        raise PatchError(f"PWCTRL2 (E8) write: signature matched {len(hits)}x, expected exactly 1")
    m = hits[0]
    found = (m.group(1)[0], dec_movin(m.group(2)), dec_movin(m.group(3)))
    state = {v: k for k, v in PWCTRL2_STATES.items()}.get(found)
    if state is None:
        raise PatchError(f"PWCTRL2 (E8) write holds an unknown value {found}")
    sites["pwctrl2"] = dict(cmd_rel=m.start(1), d1_rel=m.start(2), d2_rel=m.start(3), value=state)
    return sites


def detect_version(seg):
    """Return (rel_offset_of_major_digit, 'MAJOR.MINOR.PATCH') for the slot's
    app version string, or None. Skips the second-stage bootloader string."""
    for m in VERSION_RE.finditer(seg):
        ver = f"{int(m.group(1))}.{int(m.group(2))}.{int(m.group(3))}"
        if ver != BOOTLOADER_VERSION:
            return m.start() + 1, ver          # +1 -> the MAJOR digit
    return None


def describe_slot(s):
    if s["cmd"] == s["stock_cmd"] and s["data"] == s["stock_data"]:
        return f"stock GC9306 {s['cmd']:#04x}={s['data']:#04x} (ignored by ST7789)"
    if s["cmd"] in ST7789_REGS:
        return f"ST7789 {ST7789_REGS[s['cmd']][0]} ({s['cmd']:#04x}) = {s['data']:#04x}"
    return f"{s['cmd']:#04x}={s['data']:#04x} (modified)"


# --- Gamma region (rewrite the ignored F0..F5 into E0/E1) ---------------------
# call8 / j displacements are relative and both endpoints live in the same IROM
# segment, so working in slot file offsets gives the same result as virtual
# addresses (the constant load base cancels; it is 4-aligned). So the helper
# targets are read out of the existing calls and reused, with no address table.
def _sext18(v):
    return v - 0x40000 if v & 0x20000 else v


def _call8_bytes(pc, tgt):
    off = (tgt - ((pc & ~3) + 4)) >> 2
    v = 0x25 | ((off & 0x3FFFF) << 6)
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF])


def _j_bytes(pc, tgt):
    off = tgt - (pc + 4)
    v = 0x6 | ((off & 0x3FFFF) << 6)
    return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF])


def _call8_target(seg, pc):
    """Decode the target offset of the call8 at file offset pc."""
    instr = seg[pc] | (seg[pc + 1] << 8) | (seg[pc + 2] << 16)
    if (instr & 0x3F) != 0x25:
        raise PatchError(f"gamma: expected call8 at {pc:#x}, got {instr & 0x3F:#x}")
    return (pc & ~3) + 4 + (_sext18(instr >> 6) << 2)


def locate_gamma_region(seg):
    """Find the six consecutive GC9306 F0..F5 gamma writes. Returns a dict with
    the region file offset/length and the write_cmd/write_data/gpio call targets
    read from the stock code, or None if the region is not the stock layout
    (e.g. already gamma-injected, or an unknown build)."""
    # F0 command write: movi a11,0xF0 ; mov.n a10,a2 ; call8 <write_cmd> ; or a3
    pat = b"\xb2\xa0\xf0" + MOV_A10_A2 + b"..\xff" + OR_A3_A3_A10
    hits = [m.start() for m in re.finditer(pat, seg, re.DOTALL)]
    if len(hits) != 1:
        return None
    f0 = hits[0]
    start = f0 - GAMMA_CMD_IN_BLOCK
    if start < 0 or seg[start:start + 2] != CS_PULSE:
        return None
    # verify all six F markers at the expected 85-byte spacing
    for k in range(GAMMA_NBLOCKS):
        off = start + GAMMA_CMD_IN_BLOCK + k * GAMMA_FBLOCK
        if seg[off:off + 3] != bytes([0xB2, 0xA0, 0xF0 + k]):
            return None
    return dict(
        start=start, length=GAMMA_REGION_LEN,
        gpio=_call8_target(seg, start + 4),           # CS-pulse's first gpio call
        write_cmd=_call8_target(seg, f0 + 5),         # F0 command call
        write_data=_call8_target(seg, f0 + 11 + 4),   # F0 first data call
    )


def build_gamma_region(r, pv, nv):
    """Build the replacement region bytes: E0(pv) + E1(nv) transactions, then a
    jump over a zero-filled tail. Uses r['start'] for correct call/j offsets."""
    start, gpio, wc, wd = r["start"], r["gpio"], r["write_cmd"], r["write_data"]
    out = bytearray()

    def cs_pulse():
        for imm in (1, 0):
            out.extend(CS_PULSE)                       # l32i.n a10,[a2+0x10]
            out.extend(enc_movin(imm))                 # movi.n a11, imm
            out.extend(_call8_bytes(start + len(out), gpio))

    def cmd_write(cmd):
        out.extend(bytes([0xB2, 0xA0, cmd]))           # movi a11, cmd
        out.extend(MOV_A10_A2)
        out.extend(_call8_bytes(start + len(out), wc))
        out.extend(OR_A3_A3_A10)

    def data_write(v):
        out.extend(bytes([0xB2, 0xA0, v]) if v > 0x5F else enc_movin(v))
        out.extend(MOV_A10_A2)
        out.extend(_call8_bytes(start + len(out), wd))
        out.extend(OR_A3_A3_A10)

    for cmd, params in ((0xE0, pv), (0xE1, nv)):
        cs_pulse()
        cmd_write(cmd)
        for v in params:
            data_write(v)
    out.extend(_j_bytes(start + len(out), start + r["length"]))   # skip the tail
    if len(out) > r["length"]:
        raise PatchError(f"gamma: built region {len(out)} > {r['length']} bytes")
    out.extend(b"\x00" * (r["length"] - len(out)))   # unreachable padding
    return bytes(out)


# --- Planning edits ----------------------------------------------------------
def allocate_slots(regs):
    """Assign each (cmd, data) to a spare slot. movi.n slots hold data <= 0x5F;
    the 'wide' AF slot holds any byte, so it is used last unless required."""
    if len(regs) > len(SPARE_SLOTS):
        raise PatchError(f"at most {len(SPARE_SLOTS)} ST7789 registers can be injected (got {len(regs)})")
    free = [s[0] for s in SPARE_SLOTS if s[3] == "movin"] + ["AF"]
    plan = {}
    # values that need the wide slot go first
    for cmd, data in sorted(regs, key=lambda r: r[1] <= 0x5F):
        slot = "AF" if data > 0x5F else free[0]
        if slot not in free:
            raise PatchError(f"no free slot can hold {ST7789_REGS[cmd][0]}={data:#04x}")
        free.remove(slot)
        plan[slot] = (cmd, data)
    return plan


def plan_partition(seg, madctl_value, inversion, regs, pwctrl2=None, gamma=None):
    """Return (sites, edits) where edits = [(rel_off, old_bytes, new_bytes, desc)].
    Nothing is written here."""
    sites = locate_sites(seg)
    edits = []

    def edit(rel, new, desc):
        old = bytes(seg[rel:rel + len(new)])
        if old != new:
            edits.append((rel, old, new, desc))

    if madctl_value is not None:
        s = sites["madctl"]
        edit(s["rel"], enc_movin(madctl_value), f"MADCTL {s['value']:#04x} -> {madctl_value:#04x}")

    want_inv = inversion or INV_STOCK
    s = sites["inversion"]
    edit(s["rel"], enc_movin(INV_VALUES[want_inv]),
         f"inversion {s['value']} -> {want_inv} (cmd {INV_VALUES[want_inv]:#04x})")

    slot_plan = allocate_slots(regs)
    for name, stock_cmd, stock_data, enc, _mid, _tail in SPARE_SLOTS:
        s = sites[f"slot_{name}"]
        cmd, data = slot_plan.get(name, (stock_cmd, stock_data))
        label = (f"{ST7789_REGS[cmd][0]}={data:#04x}" if name in slot_plan
                 else f"stock {stock_cmd:#04x}={stock_data:#04x}")
        edit(s["cmd_rel"], bytes([cmd]), f"slot {name} command -> {label}")
        edit(s["data_rel"], bytes([data]) if enc == "wide" else enc_movin(data),
             f"slot {name} data -> {label}")

    want = pwctrl2 or PWCTRL2_STOCK
    s = sites["pwctrl2"]
    cmd, d1, d2 = PWCTRL2_STATES[want]
    label = ("Yoto E8=0x11,0x0B (stock)" if want == "yoto"
             else "STE 0x00,0x0A (E8 skipped, PWCTRL2 stays 0x93)")
    edit(s["cmd_rel"], bytes([cmd]), f"PWCTRL2 write command -> {label}")
    edit(s["d1_rel"], enc_movin(d1), f"PWCTRL2 write param 1 -> {label}")
    edit(s["d2_rel"], enc_movin(d2), f"PWCTRL2 write param 2 -> {label}")

    if gamma is not None:
        r = locate_gamma_region(seg)
        if r is None:
            raise PatchError("gamma: the F0..F5 gamma region was not found in its stock form "
                             "(already injected, or an unrecognised build). Patch a fresh dump.")
        pv, nv = GAMMA_CURVES[gamma]
        new = build_gamma_region(r, pv, nv)
        edit(r["start"], new, f"gamma curves E0/E1 injected ({gamma}) over F0..F5")
        sites["gamma"] = dict(region=r)
    return sites, edits


def patch_partition(img, part, madctl_value, inversion, regs, do_version, major, pwctrl2=None, gamma=None):
    """Verify everything for this slot first, then apply. On any error the slot
    is left completely untouched."""
    base, size = part["off"], part["size"]
    seg = bytes(img[base:base + size])
    _, edits = plan_partition(seg, madctl_value, inversion, regs, pwctrl2, gamma)

    if do_version:
        v = detect_version(seg)
        if v is None:
            raise PatchError("version string not found; cannot --spoof-version")
        rel, ver = v
        edits.append((rel, seg[rel:rel + 1], major.encode("ascii"),
                      f"version v{ver} -> v{major}{ver[ver.index('.'):]}"))

    changes = []
    for rel, old, new, desc in edits:
        p = base + rel
        if bytes(img[p:p + len(old)]) != old:
            raise PatchError(f"internal: bytes at {p:#08x} changed during planning")
        img[p:p + len(new)] = new
        changes.append(dict(desc=desc, flash_offset=p, before=old.hex(), after=new.hex()))

    if changes:
        cpos, spos = resign(img, base)
        changes.append(dict(desc="checksum recomputed", flash_offset=cpos))
        if spos is not None:
            changes.append(dict(desc="sha-256 recomputed", flash_offset=spos))
    return changes


# --- CLI ---------------------------------------------------------------------
def parse_hex(s, what):
    try:
        return int(s, 16)
    except ValueError:
        raise PatchError(f"{what}: {s!r} is not a hex value like 0x20")


def parse_display_param(s):
    if ":" not in s:
        raise PatchError(f"--display-param {s!r}: expected CMD:DATA, e.g. 0xBB:0x28")
    c, d = s.split(":", 1)
    cmd, data = parse_hex(c, "--display-param command"), parse_hex(d, "--display-param data")
    if cmd not in ST7789_REGS:
        allowed = ", ".join(f"{k:#04x} {v[0]}" for k, v in sorted(ST7789_REGS.items()))
        raise PatchError(f"--display-param: {cmd:#04x} is not an allowed register. Allowed: {allowed}")
    name, _meaning, ok, _default = ST7789_REGS[cmd]
    if not 0 <= data <= 0xFF or not ok(data):
        raise PatchError(f"--display-param: {data:#04x} is not a valid value for {name}")
    return cmd, data


def build_request(args):
    """Return (madctl, inversion, regs, pwctrl2, gamma, label) from the CLI."""
    madctl = None if args.no_madctl else parse_hex(args.madctl, "--madctl")
    if madctl is not None and madctl not in MADCTL_VALUES:
        raise PatchError("--madctl must be one of: " + ", ".join(f"{v:#04x}" for v in MADCTL_VALUES))

    manual = (args.inversion or args.gamma_profile or args.vcom or args.display_param
              or args.pwctrl2 or args.gamma_curve)
    if args.experiment:
        if manual:
            raise PatchError("--experiment cannot be combined with --inversion/--gamma-profile/"
                             "--vcom/--display-param/--pwctrl2/--gamma-curve")
        e = EXPERIMENTS[args.experiment]
        return madctl, e["inversion"], list(e["regs"]), e.get("pwctrl2"), e.get("gamma"), args.experiment

    regs = []
    if args.gamma_profile:
        regs.append((0x26, GAMMA_PROFILES[args.gamma_profile]))
    if args.vcom:
        regs.append(parse_display_param(f"0xBB:{args.vcom}"))
    for p in args.display_param or []:
        regs.append(parse_display_param(p))
    cmds = [c for c, _ in regs]
    if len(set(cmds)) != len(cmds):
        raise PatchError("the same register was requested more than once")
    if args.gamma_profile and args.gamma_curve:
        raise PatchError("--gamma-profile (GAMSET preset) and --gamma-curve (full E0/E1) are "
                         "different mechanisms; pick one")

    n_vars = (len(regs) + (1 if args.inversion and args.inversion != INV_STOCK else 0)
              + (1 if args.pwctrl2 and args.pwctrl2 != PWCTRL2_STOCK else 0)
              + (1 if args.gamma_curve else 0))
    if n_vars > 1 and not args.allow_multi:
        raise PatchError(f"{n_vars} colour parameters requested at once. Change ONE per experiment "
                         "so each photo isolates one cause; pass --allow-multi to override.")
    return madctl, args.inversion, regs, args.pwctrl2, args.gamma_curve, None


def load_image(path):
    with open(path, "rb") as f:
        img = bytearray(f.read())
    if len(img) != FLASH_SIZE:
        raise PatchError(f"input is {len(img)} bytes, expected {FLASH_SIZE} (a full 8 MB dump)")
    if img[0x1000] != ESP_IMAGE_MAGIC:
        raise PatchError(f"byte @0x1000 = {img[0x1000]:#04x}, expected 0xe9 (bootloader magic)")
    return img


def select_targets(img, parts, which):
    apps = app_partitions(parts)
    if which == "all":
        return apps
    idx = active_ota_index(img, parts)
    ota = [p for p in apps if p["subtype"] >= 0x10]
    if idx is None or idx >= len(ota):
        raise PatchError("could not determine the active OTA slot; use --slots all")
    return [ota[idx]]


def inspect(img, parts):
    active = active_ota_index(img, parts)
    ota = [p for p in app_partitions(parts) if p["subtype"] >= 0x10]
    for p in app_partitions(parts):
        tag = " (active)" if active is not None and active < len(ota) and ota[active] is p else ""
        seg = bytes(img[p["off"]:p["off"] + p["size"]])
        try:
            s = locate_sites(seg)
        except PatchError as e:
            print(f"  [{p['label']}{tag}] display init not recognised (code differs from known layout): {e}")
            continue
        v = detect_version(seg)
        vtxt = f"v{v[1]}" if v else "version unknown"
        print(f"  [{p['label']}{tag}] {vtxt}  MADCTL={s['madctl']['value']:#04x}  inversion={s['inversion']['value']}")
        for name, *_ in SPARE_SLOTS:
            print(f"      slot {name}: {describe_slot(s['slot_' + name])}")
        pw = s["pwctrl2"]["value"]
        print("      PWCTRL2: " + ("Yoto writes E8=0x11 (stock)" if pw == "yoto"
                                    else "E8 write skipped, ST7789 default 0x93"))
        if locate_gamma_region(seg) is not None:
            print("      gamma: stock GC9306 F0..F5 (ignored by ST7789; ST7789 runs its default gamma)")
        elif re.search(b"\xb2\xa0\xe0" + MOV_A10_A2 + b"..\xff" + OR_A3_A3_A10, seg, re.DOTALL):
            print("      gamma: ST7789 E0/E1 curves injected")
        else:
            print("      gamma: unrecognised")


def run_one(img_in, parts, targets_mode, madctl, inversion, regs, args, out_path, label,
            pwctrl2=None, gamma=None):
    img = bytearray(img_in)
    targets = select_targets(img, parts, targets_mode)
    print(f"\nTarget slot(s): {', '.join(p['label'] for p in targets)}")
    report, patched = [], 0
    for p in targets:
        try:
            changes = patch_partition(img, p, madctl, inversion, regs, args.spoof_version,
                                      args.major, pwctrl2, gamma)
        except PatchError as e:
            if targets_mode == "all":
                print(f"  [{p['label']}] SKIPPED (left untouched): {e}", file=sys.stderr)
                continue
            raise
        patched += 1
        if not changes:
            print(f"  [{p['label']}] already matches the request; no bytes changed")
        for c in changes:
            det = ""
            if "before" in c:
                b, a = c["before"], c["after"]
                if len(b) > 24:            # e.g. the 510-byte gamma region
                    b, a = b[:24] + "..", a[:24] + f".. ({len(c['after'])//2} B)"
                det = f"  {b} -> {a}"
            print(f"  [{p['label']}] {c['desc']} @ {c['flash_offset']:#08x}{det}")
        report.append(dict(slot=p["label"], changes=changes))
    if patched == 0:
        raise PatchError("no slot matched the known display-init signatures (code differs?)")

    v = detect_version(bytes(img[targets[0]["off"]:targets[0]["off"] + targets[0]["size"]]))
    fw = f"v{v[1]}" if v else "unknown"
    md5 = hashlib.md5(img).hexdigest()
    print(f"MD5 out: {md5}")
    if args.dry_run:
        print("(dry run - no file written)")
        return
    with open(out_path, "wb") as f:
        f.write(img)
    manifest = dict(
        output=os.path.basename(out_path), md5=md5, experiment=label,
        firmware=fw,
        request=dict(madctl=None if madctl is None else f"{madctl:#04x}",
                     inversion=inversion or INV_STOCK,
                     pwctrl2=pwctrl2 or PWCTRL2_STOCK,
                     gamma_curve=gamma,
                     st7789_registers=[dict(cmd=f"{c:#04x}", name=ST7789_REGS[c][0], value=f"{d:#04x}",
                                            st7789_default=f"{ST7789_REGS[c][3]:#04x}")
                                       for c, d in regs]),
        purpose=EXPERIMENTS[label]["purpose"] if label else None,
        revert="Flash your untouched dump, or re-run yoto_patch.py on this image with no colour "
               "options (e.g. --experiment baseline): managed sites are restored to stock.",
        slots=report)
    with open(out_path + ".manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Wrote {out_path} (+ .manifest.json)")


def main():
    ap = argparse.ArgumentParser(
        description="Patch a self-dumped Yoto Mini firmware image (display init + re-sign).")
    ap.add_argument("input", help="8 MB flash image dumped from YOUR device")
    ap.add_argument("-o", "--output", help="output image (default: <input>.patched.bin / <input>.<experiment>.bin)")
    ap.add_argument("--slots", choices=["active", "all"], default="active",
                    help="which app slot(s) to patch (default: active boot slot only; "
                         "'all' so a slot switch keeps the same display settings)")
    ap.add_argument("--inspect", action="store_true",
                    help="print the display settings found in each slot and exit (writes nothing)")
    ap.add_argument("--no-madctl", action="store_true", help="leave MADCTL as found in the image")
    ap.add_argument("--madctl", default="0x00",
                    help="MADCTL value (default 0x00 = no mirror, RGB). One of "
                         + ", ".join(f"{v:#04x}" for v in MADCTL_VALUES))
    ap.add_argument("--inversion", choices=["on", "off"],
                    help="send INVON (stock) or INVOFF (default: stock INVON)")
    ap.add_argument("--gamma-profile", choices=sorted(GAMMA_PROFILES),
                    help="inject ST7789 GAMSET (26h) preset curve; ST7789 default is 2.2")
    ap.add_argument("--vcom", metavar="HEX",
                    help="inject ST7789 VCOMS (BBh), 0x00-0x3F = 0.100-1.675 V; ST7789 default 0x20 = 0.90 V")
    ap.add_argument("--display-param", action="append", metavar="CMD:DATA",
                    help="inject one allowlisted single-parameter ST7789 register, e.g. 0xC3:0x12 "
                         "(repeatable, max 4). Allowed: "
                         + ", ".join(f"{k:#04x} {v[0]}" for k, v in sorted(ST7789_REGS.items())))
    ap.add_argument("--pwctrl2", choices=["yoto", "default"],
                    help="'default' skips the Yoto's E8 write so the ST7789 keeps PWCTRL2=0x93 "
                         "(default: 'yoto', stock behaviour)")
    ap.add_argument("--gamma-curve", choices=sorted(GAMMA_CURVES),
                    help="inject full ST7789 gamma curves PVGAMCTRL/NVGAMCTRL (E0/E1) in place of "
                         "the ignored GC9306 F0..F5 writes. This is the fix for dark midtones; the "
                         "'tft' curve was confirmed on hardware.")
    ap.add_argument("--allow-multi", action="store_true",
                    help="allow more than one colour parameter in one build (off: one variable per experiment)")
    ap.add_argument("--experiment", choices=sorted(EXPERIMENTS) + ["all"],
                    help="build a predefined one-variable experiment; 'all' builds every one (needs --out-dir)")
    ap.add_argument("--out-dir", help="output directory for --experiment all")
    ap.add_argument("--spoof-version", action="store_true",
                    help="bump the reported version to v<major>.x.x. NOT recommended (see docstring)")
    ap.add_argument("--major", default="9", help="major version digit for --spoof-version (default: 9)")
    ap.add_argument("--dry-run", action="store_true", help="report what would change; write nothing")
    args = ap.parse_args()

    if len(args.major) != 1 or not args.major.isdigit():
        ap.error("--major must be a single digit 0-9")

    try:
        img = load_image(args.input)
        parts = parse_partitions(img)
    except (OSError, PatchError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"MD5 in : {hashlib.md5(img).hexdigest()}")
    print("Partitions:")
    for p in parts:
        print(f"  {p['label']:12s} type={p['type']:#04x} sub={p['subtype']:#04x} "
              f"off={p['off']:#08x} size={p['size']:#08x}")

    if args.inspect:
        print("\nDisplay settings found:")
        inspect(img, parts)
        return 0

    stem = args.input.rsplit(".", 1)[0]
    try:
        if args.experiment == "all":
            if not args.out_dir:
                raise PatchError("--experiment all needs --out-dir")
            if (args.inversion or args.gamma_profile or args.vcom or args.display_param
                    or args.pwctrl2 or args.gamma_curve):
                raise PatchError("--experiment cannot be combined with manual colour options")
            os.makedirs(args.out_dir, exist_ok=True)
            base = os.path.basename(stem)
            for name in EXPERIMENTS:
                e = EXPERIMENTS[name]
                madctl = None if args.no_madctl else parse_hex(args.madctl, "--madctl")
                print(f"\n=== experiment {name}: {e['purpose']}")
                run_one(img, parts, args.slots, madctl, e["inversion"], list(e["regs"]), args,
                        os.path.join(args.out_dir, f"{base}.{name}.bin"), name,
                        e.get("pwctrl2"), e.get("gamma"))
        else:
            madctl, inversion, regs, pwctrl2, gamma, label = build_request(args)
            out = args.output or (f"{stem}.{label}.bin" if label else f"{stem}.patched.bin")
            if label:
                print(f"\n=== experiment {label}: {EXPERIMENTS[label]['purpose']}")
            run_one(img, parts, args.slots, madctl, inversion, regs, args, out, label, pwctrl2, gamma)
    except PatchError as e:
        print(f"\nREFUSED: {e}", file=sys.stderr)
        print("Nothing was written for this build.", file=sys.stderr)
        return 4

    print("\nFlash it back with the ESP32-S3 helper: tools/yoto-flasher (run_yoto_write.bat, Windows),\n"
          "or tools/s3-flasher (macOS/Linux, esptool via tools/yoto_write_checked.py).\n"
          "Keep dumps and patched images private.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
