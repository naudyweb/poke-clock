/**
 * PokeDisplay Thin RAM-Stream Firmware v1.1.0 for JUZIPi SD PRO (ESP8266 + ST7789 240x240)
 *
 * Ultra-lean "Dumb Screen" receiver controlled 100% by Poké-Hub on the PC:
 *   - POST /frame       : Streams a 240x240 baseline JPEG straight into static RAM
 *                         and blits to ST7789 via 40MHz SPI (~100-150ms, ZERO flash wear!)
 *   - GET  /brightness  : ?val=0..100 (inverted PWM on GPIO5)
 *   - GET  /rotation    : ?val=0..3
 *   - GET  /status      : Live JSON telemetry (RSSI, heap, frames, last_ms, standby)
 *   - GET  /            : Status page with direct link to Poké-Hub on your PC
 *   - GET  /ota         : Built-in Web OTA updater (update custom or restore stock SDP .bin)
 *   - Standby NTP Clock : If PC is turned off (>90s with no frame), automatically displays
 *                         a clean Pokémon GBC live NTP clock until the PC comes back online!
 *   - Recovery SoftAP   : Auto-starts "PokeDisplay-OTA" (192.168.4.1/ota) if Wi-Fi fails >25s
 */

#include <Arduino.h>
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <Updater.h>
#include <time.h>
#include <TFT_eSPI.h>
#include <TJpg_Decoder.h>

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Missing firmware/src/secrets.h — copy firmware/src/secrets.h.example to firmware/src/secrets.h"
#endif
static const char* HOSTNAME  = "poke-display";
static const long  TZ_OFFSET_SEC = -5 * 3600; // America/Lima (UTC-5)

static constexpr size_t   MAX_FRAME_BYTES = 24576; // 24 KB static RAM buffer (~23 KB free heap)
static constexpr uint32_t STANDBY_TIMEOUT_MS = 90000; // 90s without PC frame -> NTP Standby Clock

static uint8_t frameBuf[MAX_FRAME_BYTES];
static size_t frameLen = 0;
static bool frameOverflow = false;

TFT_eSPI tft = TFT_eSPI();
ESP8266WebServer server(80);

static uint8_t  currentBrightness = 95;
static uint8_t  currentRotation   = 0;
static uint32_t frameCount        = 0;
static uint32_t lastDecodeMs      = 0;
static uint32_t lastFrameMs       = 0;
static bool     apFallbackActive  = false;
static bool     standbyActive     = false;
static int      lastStandbyMin    = -1;
static String   lastHubIp         = "";

static const char* DAYS_ES[7]   = {"DOM", "LUN", "MAR", "MIE", "JUE", "VIE", "SAB"};
static const char* MONTHS_ES[12] = {"ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"};

// ============================================================
// Backlight & Display Helpers
// ============================================================
static void applyBrightness(uint8_t pct) {
    if (pct > 100) pct = 100;
    currentBrightness = pct;
    pinMode(TFT_BL, OUTPUT);
    if (pct == 0) {
        digitalWrite(TFT_BL, TFT_BACKLIGHT_ON == LOW ? HIGH : LOW);
        return;
    }
    analogWriteRange(100);
    int pwm = (TFT_BACKLIGHT_ON == LOW) ? (100 - pct) : pct;
    analogWrite(TFT_BL, pwm);
}

static bool tftOutputCallback(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bitmap) {
    if (y >= tft.height()) return false;
    tft.pushImage(x, y, w, h, bitmap);
    yield();
    return true;
}

static void drawSplash(const char* line1, const char* line2, const char* line3) {
    const uint16_t CREAM = tft.color565(248, 248, 240);
    const uint16_t INK   = tft.color565(24, 24, 32);
    const uint16_t GOLD  = tft.color565(248, 184, 0);
    const uint16_t BLUE  = tft.color565(40, 104, 200);

    tft.fillScreen(CREAM);
    tft.fillRect(0, 0, 240, 10, GOLD);
    tft.drawRect(8, 20, 224, 204, INK);
    tft.drawRect(10, 22, 220, 200, INK);
    tft.drawRect(14, 26, 212, 192, INK);

    tft.setTextDatum(MC_DATUM);
    tft.setTextColor(INK, CREAM);
    tft.drawString("POKE-DISPLAY", 120, 62, 2);

    tft.setTextColor(BLUE, CREAM);
    tft.drawString(line1, 120, 106, 2);

    tft.setTextColor(INK, CREAM);
    tft.drawString(line2, 120, 140, 2);
    tft.drawString(line3, 120, 174, 2);
}

static void drawStandbyClock(const struct tm& t) {
    const uint16_t CREAM = tft.color565(248, 248, 240);
    const uint16_t WHITE = tft.color565(255, 255, 255);
    const uint16_t INK   = tft.color565(24, 24, 32);
    const uint16_t MUTED = tft.color565(96, 104, 112);
    const uint16_t BLUE  = tft.color565(40, 104, 200);
    const uint16_t GOLD  = tft.color565(248, 184, 0);
    const uint16_t RED   = tft.color565(232, 56, 56);

    tft.fillScreen(CREAM);
    tft.fillRect(0, 0, 240, 8, GOLD);

    // Header
    tft.setTextDatum(ML_DATUM);
    tft.setTextColor(INK, CREAM);
    tft.drawString("POKEGEAR", 16, 28, 2);
    tft.setTextDatum(MR_DATUM);
    tft.setTextColor(BLUE, CREAM);
    tft.drawString("PC OFF", 224, 28, 2);

    // Big Clock Box
    tft.fillRect(10, 52, 222, 92, INK);
    tft.fillRect(12, 54, 218, 88, WHITE);
    tft.fillRect(12, 54, 218, 4, BLUE);

    int h12 = t.tm_hour % 12;
    if (h12 == 0) h12 = 12;
    char timeBuf[16];
    snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", h12, t.tm_min);

    tft.setTextDatum(MC_DATUM);
    tft.setTextColor(INK, WHITE);
    tft.drawString(timeBuf, 102, 100, 6);

    tft.fillRect(184, 108, 36, 20, BLUE);
    tft.setTextColor(WHITE, BLUE);
    tft.drawString(t.tm_hour >= 12 ? "PM" : "AM", 202, 118, 2);

    // Classic GBC Double-Border Dialogue Box
    tft.fillRect(8, 158, 224, 72, INK);
    tft.fillRect(12, 162, 216, 64, CREAM);
    tft.fillRect(14, 164, 212, 60, INK);
    tft.fillRect(16, 166, 208, 56, CREAM);

    char dateBuf[32];
    snprintf(dateBuf, sizeof(dateBuf), "%s %02d %s %04d",
             DAYS_ES[t.tm_wday % 7], t.tm_mday, MONTHS_ES[t.tm_mon % 12], t.tm_year + 1900);

    tft.setTextDatum(ML_DATUM);
    tft.setTextColor(INK, CREAM);
    tft.drawString(dateBuf, 24, 184, 2);
    tft.setTextColor(MUTED, CREAM);
    tft.drawString("RELOJ NTP AUTONOMO", 24, 206, 2);

    // Red triangle cursor
    tft.fillTriangle(210, 208, 218, 208, 214, 214, RED);
}

// ============================================================
// HTTP Handlers
// ============================================================
static void handleRoot() {
    String html = F(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>PokeDisplay</title>"
        "<style>body{font-family:monospace;background:#f8f8f0;color:#181820;max-width:440px;margin:24px auto;padding:16px;}"
        ".box{border:3px solid #181820;padding:18px;background:#fff;box-shadow:4px 4px 0 #c8d0c0;}"
        "a{display:inline-block;padding:10px 14px;background:#181820;color:#fff;text-decoration:none;margin:6px 6px 0 0;font-weight:bold;}"
        "a.hub{background:#24a848;display:block;text-align:center;margin-bottom:10px;}"
        "</style></head><body><div class='box'>"
        "<h2>POKE-DISPLAY RAM v1.1</h2>"
    );
    if (lastHubIp.length() > 0) {
        html += F("<a class='hub' href='http://");
        html += lastHubIp;
        html += F(":8765/'>Abrir Panel Poké-Hub (PC)</a>");
    }
    html += F("<p>IP: ");
    html += WiFi.localIP().toString();
    html += F("<br>Modo: ");
    html += standbyActive ? F("Reloj NTP (PC apagado)") : F("Controlado por PC");
    html += F("<br>Frames: ");
    html += String(frameCount);
    html += F("<br>Last blit: ");
    html += String(lastDecodeMs);
    html += F(" ms<br>Free heap: ");
    html += String(ESP.getFreeHeap());
    html += F(" B</p><a href='/ota'>Firmware OTA</a><a href='/status'>JSON Status</a></div></body></html>");
    server.send(200, "text/html", html);
}

static void handleStatus() {
    if (server.hasArg("keepalive")) {
        lastFrameMs = millis();
        lastHubIp = server.client().remoteIP().toString();
    }
    String out;
    out.reserve(300);
    out += F("{\"device\":\"poke-display\",\"version\":\"1.1.0\",\"ip\":\"");
    out += WiFi.localIP().toString();
    out += F("\",\"hub_ip\":\"");
    out += lastHubIp;
    out += F("\",\"rssi\":");
    out += String(WiFi.RSSI());
    out += F(",\"free_heap\":");
    out += String(ESP.getFreeHeap());
    out += F(",\"brightness\":");
    out += String(currentBrightness);
    out += F(",\"rotation\":");
    out += String(currentRotation);
    out += F(",\"frames\":");
    out += String(frameCount);
    out += F(",\"last_ms\":");
    out += String(lastDecodeMs);
    out += F(",\"standby\":");
    out += standbyActive ? F("true") : F("false");
    out += F(",\"uptime_s\":");
    out += String(millis() / 1000);
    out += F("}");
    server.send(200, "application/json", out);
}

static void handleFrameUpload() {
    HTTPUpload& upload = server.upload();
    if (upload.status == UPLOAD_FILE_START) {
        frameLen = 0;
        frameOverflow = false;
    } else if (upload.status == UPLOAD_FILE_WRITE) {
        if (!frameOverflow && (frameLen + upload.currentSize) <= MAX_FRAME_BYTES) {
            memcpy(frameBuf + frameLen, upload.buf, upload.currentSize);
            frameLen += upload.currentSize;
        } else {
            frameOverflow = true;
        }
        yield();
    }
}

static void handleFrameDone() {
    if (frameOverflow) {
        server.send(413, "application/json", "{\"ok\":false,\"error\":\"frame too large\"}");
        return;
    }

    const uint8_t* src = frameBuf;
    size_t len = frameLen;

    if (len == 0 && server.hasArg("plain")) {
        const String& body = server.arg("plain");
        if (body.length() > 0 && body.length() <= MAX_FRAME_BYTES) {
            memcpy(frameBuf, body.c_str(), body.length());
            len = body.length();
            src = frameBuf;
        }
    }

    if (len < 16) {
        server.send(400, "application/json", "{\"ok\":false,\"error\":\"empty frame\"}");
        return;
    }

    uint32_t t0 = millis();
    JRESULT res = TJpgDec.drawJpg(0, 0, src, len);
    lastDecodeMs = millis() - t0;

    if (res != JDR_OK) {
        String err = "{\"ok\":false,\"jdr\":" + String((int)res) + "}";
        server.send(422, "application/json", err);
        return;
    }

    frameCount++;
    lastFrameMs = millis();
    standbyActive = false;
    lastStandbyMin = -1;
    lastHubIp = server.client().remoteIP().toString();

    String resp = "{\"ok\":true,\"bytes\":" + String(len) + ",\"ms\":" + String(lastDecodeMs) + "}";
    server.send(200, "application/json", resp);
}

static void handleBrightness() {
    if (!server.hasArg("val")) {
        server.send(400, "application/json", "{\"ok\":false,\"error\":\"missing val\"}");
        return;
    }
    int v = server.arg("val").toInt();
    if (v < 0) v = 0;
    if (v > 100) v = 100;
    applyBrightness((uint8_t)v);
    server.send(200, "application/json", "{\"ok\":true,\"brightness\":" + String(currentBrightness) + "}");
}

static void handleRotation() {
    if (!server.hasArg("val")) {
        server.send(400, "application/json", "{\"ok\":false,\"error\":\"missing val\"}");
        return;
    }
    currentRotation = (uint8_t)(server.arg("val").toInt() & 3);
    tft.setRotation(currentRotation);
    lastStandbyMin = -1;
    server.send(200, "application/json", "{\"ok\":true,\"rotation\":" + String(currentRotation) + "}");
}

static void handleRestart() {
    server.send(200, "application/json", "{\"ok\":true,\"restarting\":true}");
    delay(250);
    ESP.restart();
}

static void handleOtaPage() {
    server.send(200, "text/html", F(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>PokeDisplay OTA</title>"
        "<style>body{font-family:monospace;background:#f8f8f0;color:#181820;max-width:420px;margin:24px auto;padding:16px;}"
        ".box{border:3px solid #181820;padding:16px;background:#fff;}"
        "button{padding:10px 16px;background:#181820;color:#fff;border:0;cursor:pointer;margin-top:12px;}"
        "</style></head><body><div class='box'>"
        "<h2>POKE-DISPLAY OTA</h2>"
        "<p>Upload custom .bin or stock SDP .bin:</p>"
        "<form method='POST' action='/update_ota' enctype='multipart/form-data'>"
        "<input type='file' name='update' accept='.bin' required><br>"
        "<button type='submit'>Flash Firmware</button>"
        "</form></div></body></html>"
    ));
}

static void handleOtaUpload() {
    HTTPUpload& upload = server.upload();
    if (upload.status == UPLOAD_FILE_START) {
        WiFiUDP::stopAll();
        drawSplash("OTA UPDATE...", "Flashing .bin", "Do not unplug");
        uint32_t maxSketchSpace = (ESP.getFreeSketchSpace() - 0x1000) & 0xFFFFF000;
        Update.begin(maxSketchSpace);
    } else if (upload.status == UPLOAD_FILE_WRITE) {
        if (Update.write(upload.buf, upload.currentSize) != upload.currentSize) {
            Update.printError(Serial);
        }
        yield();
    } else if (upload.status == UPLOAD_FILE_END) {
        if (Update.end(true)) {
            drawSplash("OTA SUCCESS!", "Rebooting...", "");
        } else {
            drawSplash("OTA FAILED", "Retry /ota", "");
        }
    }
}

static void handleOtaDone() {
    bool ok = !Update.hasError();
    server.sendHeader("Connection", "close");
    server.send(ok ? 200 : 500, "text/plain", ok ? "OK" : "FAIL");
    if (ok) {
        delay(300);
        ESP.restart();
    }
}

// ============================================================
// Setup & Loop
// ============================================================
void setup() {
    Serial.begin(115200);
    applyBrightness(currentBrightness);

    tft.init();
    tft.setRotation(currentRotation);
    tft.setSwapBytes(true);

    TJpgDec.setJpgScale(1);
    TJpgDec.setCallback(tftOutputCallback);

    drawSplash("Connecting WiFi", WIFI_SSID, "Please wait...");

    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    WiFi.hostname(HOSTNAME);
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    uint32_t startMs = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - startMs) < 25000) {
        delay(200);
        yield();
    }

    if (WiFi.status() == WL_CONNECTED) {
        MDNS.begin(HOSTNAME);
        configTime(TZ_OFFSET_SEC, 0, "pool.ntp.org", "time.nist.gov");
        String ipStr = WiFi.localIP().toString();
        drawSplash("READY v1.1!", ipStr.c_str(), "Waiting for PC");
    } else {
        apFallbackActive = true;
        WiFi.mode(WIFI_AP_STA);
        WiFi.softAP("PokeDisplay-OTA");
        drawSplash("AP RECOVERY", "PokeDisplay-OTA", "192.168.4.1/ota");
    }

    lastFrameMs = millis();

    server.on("/", HTTP_GET, handleRoot);
    server.on("/status", HTTP_GET, handleStatus);
    server.on("/ping", HTTP_GET, handleStatus);
    server.on("/frame", HTTP_POST, handleFrameDone, handleFrameUpload);
    server.on("/brightness", HTTP_GET, handleBrightness);
    server.on("/rotation", HTTP_GET, handleRotation);
    server.on("/restart", HTTP_GET, handleRestart);
    server.on("/ota", HTTP_GET, handleOtaPage);
    server.on("/update_ota", HTTP_POST, handleOtaDone, handleOtaUpload);
    server.begin();
}

static uint32_t lastWifiCheck = 0;
static uint32_t lastStandbyCheck = 0;

void loop() {
    server.handleClient();
    if (!apFallbackActive) {
        MDNS.update();
        uint32_t now = millis();
        if (now - lastWifiCheck > 10000) {
            lastWifiCheck = now;
            if (WiFi.status() != WL_CONNECTED) {
                WiFi.reconnect();
            }
        }
        // Autonomous NTP standby clock if PC is off/asleep for >90s
        if (now - lastFrameMs > STANDBY_TIMEOUT_MS && (now - lastStandbyCheck > 2000)) {
            lastStandbyCheck = now;
            time_t tnow = time(nullptr);
            if (tnow > 1700000000) {
                struct tm tinfo;
                localtime_r(&tnow, &tinfo);
                if (!standbyActive || tinfo.tm_min != lastStandbyMin) {
                    standbyActive = true;
                    lastStandbyMin = tinfo.tm_min;
                    drawStandbyClock(tinfo);
                }
            }
        }
    }
    yield();
}
