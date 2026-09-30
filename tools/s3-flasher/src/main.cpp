// s3-uart-bridge - ESP32-S3-DevKitC-1 as a USB <-> UART bridge for flashing
// chips from macOS.
//
//   * Yoto Mini (ESP32)          -> esptool.    Automatic download-mode reset.
//   * Beken BK7238 (OpenBeken)   -> ltchiptool. Plain bridge, CEN by hand.
//
// The bridge decides by itself which kind of target is attached, WITHOUT
// resetting it: it senses the target's EN line on the black wire. Every ESP32
// board holds EN high with a pull-up, so on a powered Yoto the pin reads HIGH
// against the S3's weak internal pull-down. In the Beken setup the black wire
// is not connected, the pin reads LOW, and the bridge never drives EN or IO0
// and ignores the host's DTR/RTS lines.
//
// USB runs on TinyUSB (ARDUINO_USB_MODE=0) instead of the S3's built-in
// USB-Serial-JTAG. Only TinyUSB lets the firmware see the host's baud rate and
// DTR/RTS lines. (The previous version read a line-coding register that the
// ESP32-S3 does not have, so it never compiled.)
//
// Wiring and usage: see README.md in this project folder.

#include <Arduino.h>
#include "USB.h"
#include "esp32-hal-tinyusb.h"
#include "driver/gpio.h"

// ---- Pins (ESP32-S3-DevKitC-1, header J1: GPIO15..18 are adjacent) --------
static const int        BRIDGE_TX   = 17;           // -> target RX   (Yoto: green,  RXD0)
static const int        BRIDGE_RX   = 18;           // <- target TX   (Yoto: yellow, TXD0)
static const gpio_num_t PIN_IO0     = GPIO_NUM_15;  // -> Yoto IO0 / boot  (red)
static const gpio_num_t PIN_EN      = GPIO_NUM_16;  // -> Yoto EN / reset  (black), also target sense
static const int        BOOT_BUTTON = 0;            // DevKit BOOT button (manual Yoto reset)
static const int        RGB_PIN     = 48;           // on-board WS2812 (DevKitC-1 v1.1)

// ---- Timing -------------------------------------------------------------------
static const uint32_t RESET_DEBOUNCE_MS = 5;     // ignore DTR/RTS glitches shorter than this
static const uint32_t BOOT_WINDOW_MS    = 180;   // DTR-only this recently => reset into download mode
static const uint32_t IO0_HOLD_MS       = 100;   // keep IO0 low this long after EN is released
static const uint32_t SENSE_PERIOD_MS   = 250;   // how often to sense the EN line
static const int      SENSE_AGREE       = 3;     // consecutive samples needed to change mode
static const uint32_t SENSE_HOLDOFF_MS  = 500;   // no sensing right after we drove EN
static const uint32_t SENSE_QUIET_MS    = 1000;  // no sensing while data is flowing
static const uint32_t LONG_PRESS_MS     = 1000;  // BOOT button: short = download, long = run
static const uint32_t BAUD_SETTLE_MS    = 15;    // apply a host baud only after it is stable this long

// ---- Host line state, written by the USB event task, read in loop() ----------
static volatile bool     hostDtr      = false;
static volatile bool     hostRts      = false;
static volatile uint32_t rtsOnlySince = 0;       // millis() when RTS-only began; 0 = not in it
static volatile uint32_t dtrOnlyLast  = 0;       // last millis() the lines were DTR-only; 0 = never
static volatile uint32_t hostBaud     = 115200;
static volatile uint32_t hostBaudAt   = 0;       // millis() of the last host baud change

// ---- Bridge state -------------------------------------------------------------
static uint32_t uartBaud       = 115200;
static bool     espTarget      = false;          // EN line present => auto-reset armed
static bool     enHeld         = false;
static uint32_t io0ReleaseAt   = 0;              // 0 = not holding IO0
static uint32_t senseHoldUntil = 0;              // 0 = no hold-off
static uint32_t lastSense      = 0;
static int      senseVotes     = 0;
static uint32_t lastActivity   = 0;              // 0 = no traffic yet
static uint32_t flashUntil     = 0;              // 0 = no LED flash in progress
static uint32_t ledColor       = 0xFFFFFFFF;

static inline uint32_t nz(uint32_t t) { return t ? t : 1; }
static inline bool reached(uint32_t now, uint32_t t) { return (int32_t)(now - t) >= 0; }

static void led(uint8_t r, uint8_t g, uint8_t b) {
    uint32_t c = (uint32_t(r) << 16) | (uint32_t(g) << 8) | b;
    if (c != ledColor) { ledColor = c; rgbLedWrite(RGB_PIN, r, g, b); }
}

// A target pin is either released (high-Z input) or pulled low, never driven
// high: the target's own pull-ups set the idle level. The output latch stays
// 0, so switching to output is glitch-free.
static void pinRelease(gpio_num_t p) { gpio_set_direction(p, GPIO_MODE_INPUT); }
static void pinPullLow(gpio_num_t p) { gpio_set_level(p, 0); gpio_set_direction(p, GPIO_MODE_INPUT_OUTPUT); }

// True if something pulls the EN wire up (a powered ESP32). The weak pull-down
// is on for only 300 us; an ESP32's EN has an RC filter, so this cannot reset it.
static bool senseEnPulledUp() {
    gpio_set_pull_mode(PIN_EN, GPIO_PULLDOWN_ONLY);
    delayMicroseconds(300);
    int level = gpio_get_level(PIN_EN);
    gpio_set_pull_mode(PIN_EN, GPIO_FLOATING);
    return level == 1;
}

static void setEspTarget(bool present) {
    espTarget = present;
    if (!present) {                     // never leave a line pulled low
        pinRelease(PIN_EN);
        pinRelease(PIN_IO0);
        enHeld = false;
        io0ReleaseAt = 0;
    }
    Serial0.printf("[bridge] target: %s\n",
                   present ? "ESP32 detected (EN pulled up) - auto download-mode reset ON"
                           : "no EN line - plain bridge (Beken / manual reset)");
}

// ---- USB CDC events (host baud + DTR/RTS) --------------------------------------
static void onUsbEvent(void *, esp_event_base_t, int32_t id, void *data) {
    auto *e = static_cast<arduino_usb_cdc_event_data_t *>(data);
    uint32_t now = millis();
    if (id == ARDUINO_USB_CDC_LINE_STATE_EVENT) {
        bool dtr = e->line_state.dtr, rts = e->line_state.rts;
        if (hostDtr && !hostRts) dtrOnlyLast = nz(now);   // leaving DTR-only
        if (dtr && !rts)         dtrOnlyLast = nz(now);   // entering DTR-only
        if (rts && !dtr) { if (!rtsOnlySince) rtsOnlySince = nz(now); }
        else rtsOnlySince = 0;
        hostDtr = dtr;
        hostRts = rts;
    } else if (id == ARDUINO_USB_CDC_LINE_CODING_EVENT) {
        uint32_t b = e->line_coding.bit_rate;
        if (b == 1200) usb_persist_restart(RESTART_BOOTLOADER);   // "1200 touch": update this bridge
        else if (b >= 300 && b <= 5000000) { hostBaud = b; hostBaudAt = nz(now); }
    }
}

// ---- Target reset -------------------------------------------------------------
static void beginTargetReset() {
    pinRelease(PIN_IO0);
    io0ReleaseAt = 0;
    pinPullLow(PIN_EN);
    enHeld = true;
}

static void endTargetReset(bool download) {
    if (download) {                     // IO0 must be low when EN rises
        pinPullLow(PIN_IO0);
        io0ReleaseAt = nz(millis() + IO0_HOLD_MS);
    }
    pinRelease(PIN_EN);
    enHeld = false;
    senseHoldUntil = nz(millis() + SENSE_HOLDOFF_MS);
    flashUntil = nz(millis() + 300);
    if (download) led(24, 16, 0); else led(16, 16, 16);
    Serial0.printf("[bridge] Yoto reset -> %s\n", download ? "DOWNLOAD mode" : "normal run");
}

// esptool's reset sequences, as seen on DTR/RTS:
//   classic / unix-tight : RTS-only (EN low) ~100 ms, then DTR-only (IO0 low) ~50 ms
//   usb-jtag             : DTR-only ~100 ms, then RTS-only ~100 ms, then idle
//   hard reset (--after) : RTS-only 100-200 ms with DTR low
// So: RTS-only asserts EN; when it ends, go to download mode if the lines are
// DTR-only now or were within BOOT_WINDOW_MS, otherwise just run.
static void handleHostReset() {
    uint32_t since = rtsOnlySince;
    uint32_t now = millis();
    if (since && !enHeld && (now - since) >= RESET_DEBOUNCE_MS) {
        beginTargetReset();
    } else if (!since && enHeld) {
        uint32_t last = dtrOnlyLast;
        bool download = (hostDtr && !hostRts) || (last && (now - last) < BOOT_WINDOW_MS);
        endTargetReset(download);
    }
}

// BOOT button: short press = Yoto into download mode, long press = Yoto normal run.
static void handleBootButton(uint32_t now) {
    static bool down = false;
    static uint32_t since = 0;
    bool pressed = digitalRead(BOOT_BUTTON) == LOW;
    if (pressed && !down) {
        down = true;
        since = now;
    } else if (!pressed && down) {
        down = false;
        uint32_t held = now - since;
        if (held >= 30 && !enHeld) {
            beginTargetReset();
            delay(100);
            endTargetReset(held < LONG_PRESS_MS);
        }
    }
}

// ---- Target detection ---------------------------------------------------------
static void handleSense(uint32_t now) {
    if (senseHoldUntil) {
        if (!reached(now, senseHoldUntil)) return;
        senseHoldUntil = 0;
    }
    if (enHeld || io0ReleaseAt) return;
    if (lastActivity && (now - lastActivity) < SENSE_QUIET_MS) return;   // don't stall a transfer
    if ((now - lastSense) < SENSE_PERIOD_MS) return;
    lastSense = now;

    bool present = senseEnPulledUp();
    if (present == espTarget) { senseVotes = 0; return; }
    if (++senseVotes >= SENSE_AGREE) { senseVotes = 0; setEspTarget(present); }
}

// ---- Setup / loop -------------------------------------------------------------
void setup() {
    // Host DTR/RTS toggles and a 1200-baud open must not reboot the bridge
    // itself: we act on them ourselves (the 1200 touch is re-implemented above).
    Serial.enableReboot(false);
    Serial.onEvent(ARDUINO_USB_CDC_LINE_STATE_EVENT, onUsbEvent);
    Serial.onEvent(ARDUINO_USB_CDC_LINE_CODING_EVENT, onUsbEvent);
    Serial.setRxBufferSize(8192);       // default 256 is too small for esptool bursts

    Serial0.begin(115200);              // status log on the DevKit's "UART" USB port
    Serial1.setRxBufferSize(8192);
    Serial1.begin(uartBaud, SERIAL_8N1, BRIDGE_RX, BRIDGE_TX);

    gpio_reset_pin(PIN_IO0);
    gpio_reset_pin(PIN_EN);
    gpio_set_level(PIN_IO0, 0);
    gpio_set_level(PIN_EN, 0);
    pinRelease(PIN_IO0);
    pinRelease(PIN_EN);
    gpio_set_pull_mode(PIN_IO0, GPIO_FLOATING);
    gpio_set_pull_mode(PIN_EN, GPIO_FLOATING);
    pinMode(BOOT_BUTTON, INPUT_PULLUP);

    setEspTarget(senseEnPulledUp() && senseEnPulledUp());
}

void loop() {
    uint32_t now = millis();

    // Follow the host's baud rate (esptool/ltchiptool change it mid-session).
    // Only once it has settled: on macOS, pyserial sets any speed above 230400
    // as "38400, then the real speed" and repeats that on every port
    // reconfigure, so following each change would corrupt bytes in flight.
    uint32_t hb = hostBaud;
    uint32_t hbAt = hostBaudAt;
    if (hb != uartBaud && (int32_t)(millis() - hbAt) >= (int32_t)BAUD_SETTLE_MS) {
        Serial1.flush();
        Serial1.updateBaudRate(hb);
        uartBaud = hb;
        Serial0.printf("[bridge] UART baud %lu\n", (unsigned long)hb);
    }

    handleSense(now);
    if (espTarget) {
        handleHostReset();
        handleBootButton(now);
    }
    if (io0ReleaseAt && reached(millis(), io0ReleaseAt)) {
        pinRelease(PIN_IO0);
        io0ReleaseAt = 0;
    }

    // Data pump.
    static uint8_t buf[1024];
    bool active = false;

    int n = Serial.available();
    if (n > 0) {
        n = Serial.read(buf, min(n, (int)sizeof(buf)));
        if (n > 0) { Serial1.write(buf, n); active = true; }
    }

    // Write to USB through TinyUSB directly: USBCDC::write() drops everything
    // while DTR is low, and esptool keeps DTR low for its whole session.
    n = Serial1.available();
    if (n > 0 && tud_ready()) {
        uint32_t room = tud_cdc_n_write_available(0);
        if (room > 0) {
            uint32_t take = min((uint32_t)n, min(room, (uint32_t)sizeof(buf)));
            take = Serial1.read(buf, take);
            if (take > 0) {
                tud_cdc_n_write(0, buf, take);
                tud_cdc_n_write_flush(0);
                active = true;
            }
        }
    }

    // LED: flash on reset, green on traffic, idle colour shows the mode.
    now = millis();
    if (active) lastActivity = nz(now);
    if (flashUntil) {
        if (reached(now, flashUntil)) flashUntil = 0;
    } else if (active) {
        led(0, 16, 0);                                  // green  = traffic
    } else if (!lastActivity || (now - lastActivity) > 300) {
        if (espTarget) led(10, 0, 10);                  // purple = Yoto detected, auto-reset armed
        else           led(0, 0, 8);                    // blue   = plain bridge
    }
}
