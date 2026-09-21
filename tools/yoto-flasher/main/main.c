// Yoto full-flash tool - esp-serial-flasher native on ESP32-S3.
//
// READ  mode (default, unchanged): connects to the Yoto ESP32, reads all 8MB,
//       streams it base64-encoded over the UART0 console (CH343 / COM18).
//       Protocol: READY -> host sends "START_ADDR 0xNNNNNN" -> DUMP_BEGIN ->
//       base64 lines -> DUMP_END.  (used by run_yoto_dump.bat)
//
// WRITE mode (new): host sends "WRITE size=.. block=.. baud=.. md5=<32hex>".
//       Firmware connects with the stub, optionally raises the target baud,
//       erases + flashes the whole image streamed block-by-block (lockstep
//       "NEXT idx=" handshake, each block carries a 16-bit checksum), verifies
//       the target flash against the known MD5, then resets the Yoto to run.
//       Nothing is written unless the host explicitly requests WRITE.
//       (used by run_yoto_write.bat)
//
//   GPIO6 (RX) <- yellow (Yoto TXD0)
//   GPIO5 (TX) -> green  (Yoto RXD0)
//   GPIO7      -> black  (Yoto EN)
//   GPIO4      -> red    (Yoto IO0/BOOT)

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp32_port.h"
#include "esp_loader.h"
#include "esp_loader_io.h"

static const char *TAG = "yoto_flasher";

#define FLASH_SIZE          (8u * 1024u * 1024u)
#define CHUNK               1024u          // READ chunk (keep for dump compatibility)
#define WRITE_BLOCK         4096u          // WRITE block (4K, divides 8MB evenly)
#define INITIAL_TARGET_BAUD 115200u
#define CONSOLE_BAUD        921600u
#define CONSOLE_RX_BUF      16384u
#define RESUME_WAIT_MS      15000u
#define MAX_READ_RETRIES    20

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static uint8_t rbuf[CHUNK];
static char    b64buf[CHUNK / 3 * 4 + 8];
static uint8_t wbuf[WRITE_BLOCK];
static char    linebuf[WRITE_BLOCK / 3 * 4 + 64];

static int b64enc(const uint8_t *in, int len, char *out)
{
    int o = 0;
    for (int i = 0; i < len; i += 3) {
        int r = len - i;
        unsigned n = (unsigned)in[i] << 16;
        if (r > 1) {
            n |= (unsigned)in[i + 1] << 8;
        }
        if (r > 2) {
            n |= (unsigned)in[i + 2];
        }
        out[o++] = B64[(n >> 18) & 63];
        out[o++] = B64[(n >> 12) & 63];
        out[o++] = (r > 1) ? B64[(n >> 6) & 63] : '=';
        out[o++] = (r > 2) ? B64[n & 63] : '=';
    }
    return o;
}

// Decode base64 `in` (NUL-terminated) into `out`. Returns decoded byte count, or -1 on error.
static int b64dec(const char *in, uint8_t *out)
{
    static int8_t rev[256];
    static bool init = false;
    if (!init) {
        for (int i = 0; i < 256; i++) {
            rev[i] = -1;
        }
        for (int i = 0; i < 64; i++) {
            rev[(uint8_t)B64[i]] = (int8_t)i;
        }
        init = true;
    }
    int acc = 0, bits = 0, o = 0;
    for (const char *p = in; *p; p++) {
        if (*p == '=' || *p == '\r' || *p == '\n' || *p == ' ') {
            break;
        }
        int8_t v = rev[(uint8_t)*p];
        if (v < 0) {
            return -1;
        }
        acc = (acc << 6) | v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out[o++] = (uint8_t)((acc >> bits) & 0xFF);
        }
    }
    return o;
}

// CRC-32 (IEEE 802.3, reflected, poly 0xEDB88320) matching Python's zlib.crc32.
static uint32_t crc32_calc(const uint8_t *d, uint32_t n)
{
    uint32_t c = 0xFFFFFFFFu;
    for (uint32_t i = 0; i < n; i++) {
        c ^= d[i];
        for (int k = 0; k < 8; k++) {
            c = (c >> 1) ^ (0xEDB88320u & (uint32_t)(-(int32_t)(c & 1u)));
        }
    }
    return c ^ 0xFFFFFFFFu;
}

// Read one '\n'-terminated line from the console into buf (NUL-terminated, without CR/LF).
// Returns length, or -1 on timeout.
static int read_line(char *buf, int cap, int timeout_ms)
{
    int len = 0;
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (esp_timer_get_time() < deadline && len < cap - 1) {
        uint8_t ch;
        int got = uart_read_bytes(UART_NUM_0, &ch, 1, pdMS_TO_TICKS(20));
        if (got <= 0) {
            continue;
        }
        if (ch == '\n') {
            buf[len] = 0;
            return len;
        }
        if (ch == '\r') {
            continue;
        }
        buf[len++] = (char)ch;
    }
    buf[len] = 0;
    return (len > 0) ? len : -1;
}

static bool connect_rom(void)
{
    loader_port_change_transmission_rate(INITIAL_TARGET_BAUD);
    for (int a = 1; a <= 6; a++) {
        esp_loader_connect_args_t c = ESP_LOADER_CONNECT_DEFAULT();
        if (esp_loader_connect(&c) == ESP_LOADER_SUCCESS) {
            ESP_LOGI(TAG, "CONNECTED (ROM) target=%d", (int)esp_loader_get_target());
            return true;
        }
        ESP_LOGW(TAG, "ROM connect retry %d", a);
        vTaskDelay(pdMS_TO_TICKS(400));
    }
    return false;
}

// Prints the READY prompt and returns the first command line the host sends
// (either "START_ADDR 0x..." for a dump, or "WRITE ..." for a flash write).
static void read_command(char *cmd, int cap)
{
    printf("READY size=%u chunk=%u default_start=0x000000 send: START_ADDR 0xNNNNNN | WRITE size=.. block=.. baud=.. md5=..\n",
           (unsigned)FLASH_SIZE, (unsigned)CHUNK);
    int n = read_line(cmd, cap, RESUME_WAIT_MS);
    if (n <= 0) {
        cmd[0] = 0;
    }
}

// ---------------- READ (dump) ----------------
static void do_read(uint32_t start_addr)
{
    printf("\n\nDUMP_BEGIN size=%u chunk=%u start=0x%06x fmt=base64-per-line\n",
           (unsigned)FLASH_SIZE, (unsigned)CHUNK, (unsigned)start_addr);
    esp_log_level_set("*", ESP_LOG_NONE);

    for (uint32_t addr = start_addr; addr < FLASH_SIZE; addr += CHUNK) {
        esp_loader_error_t e = ESP_LOADER_ERROR_FAIL;
        int retries = 0;
        while (e != ESP_LOADER_SUCCESS) {
            e = esp_loader_flash_read(rbuf, addr, CHUNK);
            if (e == ESP_LOADER_SUCCESS) {
                break;
            }
            retries++;
            printf("RETRY addr=0x%06x try=%d\n", (unsigned)addr, retries);
            vTaskDelay(pdMS_TO_TICKS(100));
            if (retries % 10 == 0) {
                printf("RECONNECT addr=0x%06x\n", (unsigned)addr);
                while (!connect_rom()) {
                    printf("RECONNECT_WAIT addr=0x%06x\n", (unsigned)addr);
                    vTaskDelay(pdMS_TO_TICKS(1000));
                }
            }
            if (retries >= MAX_READ_RETRIES) {
                printf("\nDUMP_ABORT addr=0x%06x retries=%d\n", (unsigned)addr, retries);
                return;
            }
        }
        int n = b64enc(rbuf, CHUNK, b64buf);
        b64buf[n++] = '\n';
        fwrite(b64buf, 1, n, stdout);
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    esp_log_level_set("*", ESP_LOG_INFO);
    printf("\nDUMP_END errors=0\n");
    ESP_LOGI(TAG, "dump done");
}

// ---------------- WRITE (flash) ----------------
static void do_write(const char *cmd)
{
    unsigned long size = 0, block = 0, baud = 0;
    char md5hex[40] = {0};
    // Fields may arrive in any order; parse leniently.
    const char *p;
    if ((p = strstr(cmd, "size=")))  size  = strtoul(p + 5, NULL, 0);
    if ((p = strstr(cmd, "block="))) block = strtoul(p + 6, NULL, 0);
    if ((p = strstr(cmd, "baud=")))  baud  = strtoul(p + 5, NULL, 0);
    if ((p = strstr(cmd, "md5=")))   { strncpy(md5hex, p + 4, 32); md5hex[32] = 0; }

    if (size == 0)  size = FLASH_SIZE;
    if (block == 0) block = WRITE_BLOCK;
    if (baud == 0)  baud = INITIAL_TARGET_BAUD;

    if (size > FLASH_SIZE || block == 0 || block > WRITE_BLOCK || (size % block) != 0) {
        printf("WRITE_FAILED reason=badparams size=%lu block=%lu\n", size, block);
        return;
    }
    // The library's verify expects expected_md5 as a 32-char lowercase-hex STRING
    // (it hexifies the target's raw MD5 and memcmp's MD5_SIZE_ROM=32 bytes), NOT
    // 16 raw bytes. Validate + normalize md5hex to lowercase and pass it directly.
    bool have_md5 = (strlen(md5hex) == 32);
    for (int i = 0; i < 32 && have_md5; i++) {
        char c = md5hex[i];
        if (c >= 'A' && c <= 'F') { c += 32; md5hex[i] = c; }
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) have_md5 = false;
    }

    printf("WRITE_ACK size=%lu block=%lu req_baud=%lu md5=%d\n", size, block, baud, (int)have_md5);
    uart_flush_input(UART_NUM_0);   // drop any repeated WRITE commands from the host

    // Connect with the stub (fast, robust erase/write). Fall back to ROM.
    bool stub = false;
    for (int a = 0; a < 4 && !stub; a++) {
        loader_port_change_transmission_rate(INITIAL_TARGET_BAUD);
        esp_loader_connect_args_t c = ESP_LOADER_CONNECT_DEFAULT();
        if (esp_loader_connect_with_stub(&c) == ESP_LOADER_SUCCESS) {
            stub = true;
        } else {
            ESP_LOGW(TAG, "stub connect retry %d", a);
            vTaskDelay(pdMS_TO_TICKS(300));
        }
    }
    if (!stub && !connect_rom()) {
        printf("WRITE_FAILED reason=noconnect\n");
        return;
    }
    printf("WRITE_STUB stub=%d target=%d\n", (int)stub, (int)esp_loader_get_target());

    uint32_t actual_baud = INITIAL_TARGET_BAUD;
    if (baud > INITIAL_TARGET_BAUD) {
        esp_loader_error_t re = stub
            ? esp_loader_change_transmission_rate_stub(INITIAL_TARGET_BAUD, (uint32_t)baud)
            : esp_loader_change_transmission_rate((uint32_t)baud);
        if (re == ESP_LOADER_SUCCESS) {
            loader_port_change_transmission_rate((uint32_t)baud);
            actual_baud = (uint32_t)baud;
        } else {
            ESP_LOGW(TAG, "baud change to %lu failed (%d); staying at %u", baud, re, (unsigned)INITIAL_TARGET_BAUD);
        }
    }
    printf("WRITE_BAUD baud=%u\n", (unsigned)actual_baud);

    printf("WRITE_ERASE\n");
    esp_log_level_set("*", ESP_LOG_NONE);
    esp_loader_error_t e = esp_loader_flash_start(0, (uint32_t)size, (uint32_t)block);
    if (e != ESP_LOADER_SUCCESS) {
        esp_log_level_set("*", ESP_LOG_INFO);
        printf("WRITE_FAILED reason=flashstart e=%d\n", e);
        return;
    }
    uart_flush_input(UART_NUM_0);
    uint32_t nblocks = (uint32_t)(size / block);
    printf("WRITE_BEGIN size=%lu block=%lu nblocks=%u\n", size, block, (unsigned)nblocks);

    for (uint32_t idx = 0; idx < nblocks; idx++) {
        // Lockstep: request block `idx`, expect "D <idx> <b64> <sum16hex>".
        bool got = false;
        int miss = 0;
        while (!got) {
            printf("NEXT idx=%u\n", (unsigned)idx);
            int ln = read_line(linebuf, sizeof(linebuf), 5000);
            if (ln <= 0) {
                if (++miss > 60) {
                    esp_log_level_set("*", ESP_LOG_INFO);
                    printf("WRITE_FAILED reason=hosttimeout idx=%u\n", (unsigned)idx);
                    return;
                }
                continue;
            }
            // parse tokens
            char *save = NULL;
            char *t0 = strtok_r(linebuf, " ", &save);   // "D"
            char *t1 = strtok_r(NULL, " ", &save);       // idx
            char *t2 = strtok_r(NULL, " ", &save);       // b64
            char *t3 = strtok_r(NULL, " ", &save);       // crc32 hex
            if (!t0 || !t1 || !t2 || !t3 || t0[0] != 'D' ||
                (uint32_t)strtoul(t1, NULL, 10) != idx) {
                if (++miss > 100) {
                    esp_log_level_set("*", ESP_LOG_INFO);
                    printf("WRITE_FAILED reason=badframe idx=%u\n", (unsigned)idx);
                    return;
                }
                continue;   // wrong/late/garbled frame; re-request
            }
            int dl = b64dec(t2, wbuf);
            if (dl != (int)block || crc32_calc(wbuf, (uint32_t)dl) != (uint32_t)strtoul(t3, NULL, 16)) {
                printf("RESEND idx=%u\n", (unsigned)idx);   // corruption caught -> host resends
                if (++miss > 100) {
                    esp_log_level_set("*", ESP_LOG_INFO);
                    printf("WRITE_FAILED reason=crc idx=%u\n", (unsigned)idx);
                    return;
                }
                continue;
            }
            got = true;
        }
        // Write the block. The library already retries the FLASH_DATA packet
        // internally; re-calling flash_write here would advance the sequence
        // number and misalign the image, so on failure we abort and let the
        // host restart (the final MD5 verify is the safety gate either way).
        esp_loader_error_t we = esp_loader_flash_write(wbuf, (uint32_t)block);
        if (we != ESP_LOADER_SUCCESS) {
            esp_log_level_set("*", ESP_LOG_INFO);
            printf("WRITE_FAILED reason=flashwrite idx=%u e=%d\n", (unsigned)idx, we);
            return;
        }
        if ((idx & 0x3F) == 0) {
            printf("PROG idx=%u nblocks=%u\n", (unsigned)idx, (unsigned)nblocks);
        }
    }

    e = esp_loader_flash_finish(false);
    esp_log_level_set("*", ESP_LOG_INFO);
    if (e != ESP_LOADER_SUCCESS) {
        printf("WRITE_FAILED reason=finish e=%d\n", e);
        return;
    }

    if (have_md5) {
        esp_loader_error_t v = esp_loader_flash_verify_known_md5(0, (uint32_t)size, (const uint8_t *)md5hex);
        if (v != ESP_LOADER_SUCCESS) {
            printf("WRITE_VERIFY_FAIL e=%d\n", v);
            printf("WRITE_FAILED reason=verify (flash NOT confirmed; rerun before power-cycling)\n");
            return;
        }
        printf("WRITE_VERIFY_OK\n");
    } else {
        printf("WRITE_VERIFY_SKIP\n");
    }

    esp_loader_reset_target();   // boot the Yoto into the freshly written firmware
    printf("WRITE_DONE\n");
    ESP_LOGI(TAG, "write done");
}

void app_main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);
    uart_set_baudrate(UART_NUM_0, CONSOLE_BAUD);
    esp_err_t console_driver = uart_driver_install(UART_NUM_0, CONSOLE_RX_BUF, 0, 0, NULL, 0);
    if (console_driver != ESP_OK && console_driver != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "UART0 driver install returned %d", console_driver);
    }
    uart_flush_input(UART_NUM_0);

    const loader_esp32_config_t config = {
        .baud_rate = INITIAL_TARGET_BAUD,
        .uart_port = UART_NUM_1,
        .uart_rx_pin = GPIO_NUM_6,
        .uart_tx_pin = GPIO_NUM_5,
        .reset_trigger_pin = GPIO_NUM_7,
        .gpio0_trigger_pin = GPIO_NUM_4,
    };
    if (loader_port_esp32_init(&config) != ESP_LOADER_SUCCESS) {
        ESP_LOGE(TAG, "serial init failed");
        vTaskDelete(NULL);
        return;
    }

    if (!connect_rom()) {
        ESP_LOGE(TAG, "NOT CONNECTED");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "connected; waiting for command (READ default, or WRITE)...");
    vTaskDelay(pdMS_TO_TICKS(150));

    char cmd[128] = {0};
    read_command(cmd, sizeof(cmd));

    if (strncmp(cmd, "WRITE", 5) == 0) {
        do_write(cmd);
    } else {
        uint32_t start_addr = 0;
        const char *p = strstr(cmd, "START_ADDR");
        if (p) {
            unsigned long a = strtoul(p + 10, NULL, 0);
            if (a < FLASH_SIZE) {
                start_addr = (uint32_t)(a - (a % CHUNK));
            }
        }
        printf("RESUME start=0x%06x\n", (unsigned)start_addr);
        do_read(start_addr);
    }

    vTaskDelete(NULL);
}
