# s3-flasher — macOS/Linux flasher for the Yoto Mini

An **ESP32-S3-DevKitC-1** turned into a USB-to-UART bridge so you can dump and
write the Yoto Mini's flash from macOS or Linux with plain `esptool`, with no
manual boot buttons: it drives the Yoto's EN/IO0 for you. (It also works as a
plain bridge for other chips, e.g. a Beken BK7238, hence the two modes below.)
Plug the DevKit's **"USB"** port (native USB) into the computer, build once with
PlatformIO, then use it with [`../yoto_write_checked.py`](../yoto_write_checked.py)
or `esptool` directly. This is the alternative to the Windows
[`../yoto-flasher`](../yoto-flasher) `.bat` workflow.

It works with two kinds of target and **decides by itself** which one is
attached:

| Target | Tool | Reset / boot mode |
|---|---|---|
| **Yoto Mini** (ESP32) | `esptool` | automatic, the bridge drives EN and IO0 |
| **Beken BK7238** (Tuya T1-3S, OpenBeken) | `ltchiptool` | manual, touch CEN to GND by hand |

**How it decides:** the bridge senses the black wire (EN). Every ESP32 board
holds EN high with a pull-up, so on a powered Yoto the pin reads HIGH. In the
Beken setup the black wire is not connected, so it reads LOW and the bridge
never drives EN or IO0. Sensing does not reset or disturb the target.

The bridge also follows the baud rate the Mac sets, so tools that switch to a
faster baud mid-session (esptool, ltchiptool) work.

---

## Wiring

All pins are on the DevKit's left header (J1). GPIO15, 16, 17 and 18 sit next
to each other.

### Yoto Mini (6 wires)

| Harness wire | Yoto pad | ESP32-S3 DevKitC-1 pin |
|---|---|---|
| yellow | TXD0 | **GPIO18** (bridge RX) |
| green | RXD0 | **GPIO17** (bridge TX) |
| black | EN (reset) | **GPIO16** |
| red | IO0 (boot) | **GPIO15** |
| white | 3V3 | **3V3** |
| blue | GND | **GND** |

### Beken BK7238 / Tuya T1-3S

| Module pin | ESP32-S3 DevKitC-1 pin |
|---|---|
| RX1 | GPIO17 (bridge TX) |
| TX1 | GPIO18 (bridge RX) |
| 3V3 | 3V3 (white wire) |
| GND | GND (blue wire) |
| CEN | loose wire, touch to GND by hand when ltchiptool asks |

Leave **GPIO15 and GPIO16 unconnected** for Beken. A wire on GPIO16 that is
pulled up (for example a module's CEN) would make the bridge think an ESP32 is
attached.

### Power

The **white wire is the S3's 3V3**, and it powers the target's 3.3 V rail.
Power the target from **one** source only: either the white wire, or the
target's own battery/USB with the white wire disconnected. Not both.

With the white wire connected, the Yoto is powered as soon as the DevKit is
plugged in, so the bridge detects it right away.

---

## LED

| Colour | Meaning |
|---|---|
| blue | plain bridge, no ESP32 detected (Beken mode) |
| purple | Yoto / ESP32 detected, automatic reset armed |
| green | data flowing |
| yellow flash | Yoto was reset into **download** (bootloader) mode |
| white flash | Yoto was reset into **normal run** |

---

## Using it with the Yoto

No manual reset and no special flags: esptool's normal reset does it.

```bash
ls /dev/cu.usbmodem*          # the bridge's port

# dump the whole 8 MB flash (keep this file private: Wi-Fi, token, MACs)
esptool --chip esp32 -p /dev/cu.usbmodemXXXX -b 460800 read-flash 0 0x800000 yoto_now.bin

# write a full image back (esptool verifies the hash, then resets the Yoto to run)
esptool --chip esp32 -p /dev/cu.usbmodemXXXX -b 460800 write-flash 0x0 image.bin
```

If a connection is flaky, use `-b 115200`.

**Manual fallback,** if a tool does not toggle DTR/RTS: press the DevKit's
**BOOT** button while the LED is purple.

- short press puts the Yoto into download mode;
- long press, over 1 second, resets it into normal run.

## Using it with a Beken chip

Same as before: run `ltchiptool` on the bridge's port and touch CEN to GND by
hand when it asks. The bridge ignores DTR/RTS in this mode.

---

## Updating this bridge's firmware

```bash
cd ~/Desktop/ESP32/s3-uart-bridge
pio run -t upload
```

PlatformIO opens the port at 1200 baud, the firmware reboots into the S3's ROM
bootloader, and the upload runs.

If that ever fails: hold **BOOT**, tap **RST**, release **BOOT**, then run
`pio run -t upload` again.

## Troubleshooting

The bridge prints status lines, such as target detected, Yoto reset, and baud
changes, on the DevKit's **other** USB port, labelled "UART", at 115200 baud:

```bash
ls /dev/cu.usbserial* /dev/cu.wchusbserial*
pio device monitor -p /dev/cu.usbserial-XXXX -b 115200
```

- **LED stays blue with the Yoto wired:** the black wire is not reaching the
  Yoto's EN pad, or the Yoto has no power.
- **esptool cannot connect:** check that yellow goes to GPIO18 and green to
  GPIO17 (TX and RX crossed), then try `-b 115200`.

---

## Why TinyUSB

The firmware uses TinyUSB CDC (`ARDUINO_USB_MODE=0`) instead of the S3's
built-in USB-Serial-JTAG. Only TinyUSB tells the firmware the host's baud rate
and its DTR/RTS lines. The previous version read a baud-rate register that
exists only on newer chips (C6, H2, P4), not on the ESP32-S3, so it never
compiled.

Two framework details the code works around:

- `USBCDC::write()` drops all data while DTR is low, and esptool keeps DTR low
  for its whole session. The bridge therefore sends through TinyUSB directly
  (`tud_cdc_n_write`).
- The core would reboot the S3 on esptool's reset sequence and on a 1200-baud
  open. The bridge turns that off and handles both itself.
