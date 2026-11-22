/**
 * PokeDisplay Thin RAM-Stream Firmware v1.2.0 for JUZIPi SD PRO (ESP8266 + ST7789 240x240)
 *
 * Ultra-lean "Dumb Screen" receiver controlled by Poké-Hub on the PC, with autonomous
 * offline NTP clock + OpenWeatherMap weather updates when the PC is turned off:
 *   - POST /frame          : Streams full 240x240 or partial (?x=&y=) baseline JPEG into static RAM
 *                            and blits to ST7789 via 40MHz SPI (~40-110ms, ZERO flash wear!)
 *   - GET  /brightness     : ?val=0..100 (inverted PWM on GPIO5)
 *   - GET  /rotation       : ?val=0..3
 *   - GET  /offline_cfg    : Syncs offline city/key/units/12h/trainer name from PC (persisted only on change)
 *   - GET  /offline_weather: Triggers an autonomous weather fetch directly over Wi-Fi (for verification)
 *   - GET  /status         : Live JSON telemetry (RSSI, heap, frames, last_ms, standby, offline weather)
 *   - GET  /               : Status page with direct link to Poké-Hub on your PC
 *   - GET  /ota            : Built-in Web OTA updater (update custom or restore stock SDP .bin)
 *   - Standby Offline Mode : If PC is turned off (>90s with no frame), displays a static Pokémon GBC
 *                            NTP clock + autonomous live OpenWeatherMap weather updated every 15 min!
 *   - Recovery SoftAP      : Auto-starts "PokeDisplay-OTA" (192.168.4.1/ota) if Wi-Fi fails >25s
 */

#include <Arduino.h>
#include <EEPROM.h>
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

static const char* HOSTNAME      = "poke-display";
static const long  TZ_OFFSET_SEC = -5 * 3600; // America/Lima (UTC-5)

static constexpr size_t   MAX_FRAME_BYTES             = 24576;   // 24 KB static RAM buffer
static constexpr uint32_t STANDBY_TIMEOUT_MS          = 90000;   // 90s without PC frame -> Offline mode
static constexpr uint32_t OFFLINE_WEATHER_INTERVAL_MS = 900000;  // 15 min autonomous weather refresh
static constexpr uint32_t OFFLINE_WEATHER_RETRY_MS    = 60000;   // 60s retry on transient Wi-Fi error
static constexpr uint32_t EEPROM_MAGIC                = 0x504B1201;

struct OfflineConfig {
    uint32_t magic;
    char city[40];
    char weatherKey[48];
    char trainerName[16];
    uint8_t celsius;
    uint8_t hour12;
};

static OfflineConfig offlineCfg;

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

// Autonomous offline weather state
static bool     offlineWeatherValid      = false;
static float    offlineTempC             = 0.0f;
static int      offlineHumidity          = 0;
static String   offlineDesc              = "";
static String   offlineCityResp          = "";
static String   offlineWeatherUpdatedHm  = "";
static uint32_t offlineWeatherUpdatedMs  = 0;
static uint32_t lastWeatherAttemptMs     = 0;

static const char* DAYS_ES[7]   = {"DOM", "LUN", "MAR", "MIE", "JUE", "VIE", "SAB"};
static const char* MONTHS_ES[12] = {"ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"};

// ============================================================
// Offline Config EEPROM Helpers
// ============================================================
static void copyStrSafe(char* dst, size_t dstSize, const char* src) {
    if (!dst || dstSize == 0) return;
    if (!src) {
        dst[0] = '\0';
        return;
    }
    strncpy(dst, src, dstSize - 1);
    dst[dstSize - 1] = '\0';
}

static void loadOfflineConfig() {
    EEPROM.begin(sizeof(OfflineConfig));
    EEPROM.get(0, offlineCfg);
    if (offlineCfg.magic != EEPROM_MAGIC) {
        memset(&offlineCfg, 0, sizeof(offlineCfg));
        offlineCfg.magic = EEPROM_MAGIC;
        copyStrSafe(offlineCfg.city, sizeof(offlineCfg.city), DEFAULT_WEATHER_CITY);
        copyStrSafe(offlineCfg.weatherKey, sizeof(offlineCfg.weatherKey), DEFAULT_WEATHER_KEY);
        copyStrSafe(offlineCfg.trainerName, sizeof(offlineCfg.trainerName), "POKEGEAR");
        offlineCfg.celsius = 1;
        offlineCfg.hour12 = 1;
    }
    if (offlineCfg.city[0] == '\0') {
        copyStrSafe(offlineCfg.city, sizeof(offlineCfg.city), DEFAULT_WEATHER_CITY);
    }
    if (offlineCfg.weatherKey[0] == '\0') {
        copyStrSafe(offlineCfg.weatherKey, sizeof(offlineCfg.weatherKey), DEFAULT_WEATHER_KEY);
    }
    if (offlineCfg.trainerName[0] == '\0') {
        copyStrSafe(offlineCfg.trainerName, sizeof(offlineCfg.trainerName), "POKEGEAR");
    }
}

static bool saveOfflineConfigIfChanged(const OfflineConfig& nextCfg) {
    if (memcmp(&offlineCfg, &nextCfg, sizeof(OfflineConfig)) == 0) {
        return false;
    }
    offlineCfg = nextCfg;
    EEPROM.put(0, offlineCfg);
    return EEPROM.commit();
}

// ============================================================
// String & JSON Helpers for Autonomous Weather Fetch
// ============================================================
static String urlEncodeParam(const char* src) {
    String out;
    if (!src) return out;
    const char* hex = "0123456789ABCDEF";
    while (*src) {
        unsigned char c = (unsigned char)*src++;
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
            c == '-' || c == '_' || c == '.' || c == '~') {
            out += (char)c;
        } else {
            out += '%';
            out += hex[(c >> 4) & 0xF];
            out += hex[c & 0xF];
        }
    }
    return out;
}

static String sanitizeAsciiUpper(const String& raw, size_t maxLen) {
    String out;
    out.reserve(maxLen + 1);
    for (size_t i = 0; i < raw.length() && out.length() < maxLen; i++) {
        uint8_t c = (uint8_t)raw[i];
        if (c == 0xC3 && i + 1 < raw.length()) {
            uint8_t c2 = (uint8_t)raw[++i];
            char repl = ' ';
            switch (c2) {
                case 0xA1: case 0x81: case 0xA0: case 0x80: case 0xA4: case 0x84: repl = 'A'; break;
                case 0xA9: case 0x89: case 0xA8: case 0x88: case 0xAB: case 0x8B: repl = 'E'; break;
                case 0xAD: case 0x8D: case 0xAC: case 0x8C: case 0xAF: case 0x8F: repl = 'I'; break;
                case 0xB3: case 0x93: case 0xB2: case 0x92: case 0xB6: case 0x96: repl = 'O'; break;
                case 0xBA: case 0x9A: case 0xB9: case 0x99: case 0xBC: case 0x9C: repl = 'U'; break;
                case 0xB1: case 0x91: repl = 'N'; break;
                default: repl = ' '; break;
            }
            out += repl;
        } else if (c >= 32 && c <= 126) {
            char ch = (char)c;
            if (ch >= 'a' && ch <= 'z') ch = (char)(ch - 32);
            out += ch;
        }
    }
    out.trim();
    return out;
}

static String jsonEscape(const String& s) {
    String out;
    out.reserve(s.length() + 4);
    for (size_t i = 0; i < s.length(); i++) {
        char c = s[i];
        if (c == '"' || c == '\\') out += '\\';
        if ((uint8_t)c >= 32) out += c;
    }
    return out;
}

static bool extractJsonFloat(const String& body, const char* key, float& outVal) {
    int idx = body.indexOf(key);
    if (idx < 0) return false;
    idx += strlen(key);
    while (idx < (int)body.length() && (body[idx] == ' ' || body[idx] == '"')) idx++;
    int end = idx;
    while (end < (int)body.length() && (isDigit(body[end]) || body[end] == '-' || body[end] == '.')) end++;
    if (end <= idx) return false;
    outVal = body.substring(idx, end).toFloat();
    return true;
}

static bool extractJsonString(const String& body, const char* key, String& outVal) {
    int idx = body.indexOf(key);
    if (idx < 0) return false;
    idx += strlen(key);
    int end = body.indexOf('"', idx);
    if (end < 0) return false;
    outVal = body.substring(idx, end);
    return true;
}

static bool fetchOfflineWeather() {
    lastWeatherAttemptMs = millis();
    if (WiFi.status() != WL_CONNECTED) return false;
    if (offlineCfg.city[0] == '\0' || offlineCfg.weatherKey[0] == '\0') return false;

    WiFiClient client;
    client.setTimeout(4000);
    if (!client.connect("api.openweathermap.org", 80)) {
        return false;
    }

    String req;
    req.reserve(240);
    req += F("GET /data/2.5/weather?q=");
    req += urlEncodeParam(offlineCfg.city);
    req += F("&appid=");
    req += urlEncodeParam(offlineCfg.weatherKey);
    req += F("&units=metric&lang=es HTTP/1.0\r\n"
             "Host: api.openweathermap.org\r\n"
             "Connection: close\r\n\r\n");
    client.print(req);

    // Wait for response
    uint32_t t0 = millis();
    while (client.connected() && !client.available() && (millis() - t0) < 4000) {
        delay(10);
        yield();
    }

    String statusLine = client.readStringUntil('\n');
    if (statusLine.indexOf("200") < 0) {
        client.stop();
        return false;
    }

    // Skip HTTP headers
    while (client.connected() || client.available()) {
        String hdr = client.readStringUntil('\n');
        if (hdr == "\r" || hdr.length() == 0) break;
        yield();
    }

    // Read JSON body (bounded to protect heap)
    String body;
    body.reserve(1024);
    t0 = millis();
    while ((client.connected() || client.available()) && body.length() < 1536 && (millis() - t0) < 4000) {
        while (client.available() && body.length() < 1536) {
            body += (char)client.read();
        }
        yield();
    }
    client.stop();

    float tempVal = 0.0f;
    float humVal  = 0.0f;
    String descRaw;
    String cityRaw;

    if (!extractJsonFloat(body, "\"temp\":", tempVal)) {
        return false;
    }
    extractJsonFloat(body, "\"humidity\":", humVal);
    extractJsonString(body, "\"description\":\"", descRaw);
    extractJsonString(body, "\"name\":\"", cityRaw);

    offlineTempC = tempVal;
    offlineHumidity = (int)roundf(humVal);
    offlineDesc = sanitizeAsciiUpper(descRaw, 22);
    offlineCityResp = sanitizeAsciiUpper(cityRaw.length() > 0 ? cityRaw : String(offlineCfg.city), 14);
    offlineWeatherValid = true;
    offlineWeatherUpdatedMs = millis();

    time_t tnow = time(nullptr);
    if (tnow > 1700000000) {
        struct tm tinfo;
        localtime_r(&tnow, &tinfo);
        char hm[8];
        snprintf(hm, sizeof(hm), "%02d:%02d", tinfo.tm_hour, tinfo.tm_min);
        offlineWeatherUpdatedHm = String(hm);
    }
    return true;
}

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
    tft.drawString(offlineCfg.trainerName, 16, 28, 2);
    tft.setTextDatum(MR_DATUM);
    tft.setTextColor(BLUE, CREAM);
    tft.drawString("PC OFF", 224, 28, 2);

    // Big Clock Box (static, zero animations in offline mode)
    tft.fillRect(10, 52, 222, 92, INK);
    tft.fillRect(12, 54, 218, 88, WHITE);
    tft.fillRect(12, 54, 218, 4, BLUE);

    char timeBuf[16];
    if (offlineCfg.hour12) {
        int h12 = t.tm_hour % 12;
        if (h12 == 0) h12 = 12;
        snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", h12, t.tm_min);
        tft.setTextDatum(MC_DATUM);
        tft.setTextColor(INK, WHITE);
        tft.drawString(timeBuf, 102, 100, 6);

        tft.fillRect(184, 108, 36, 20, BLUE);
        tft.setTextColor(WHITE, BLUE);
        tft.drawString(t.tm_hour >= 12 ? "PM" : "AM", 202, 118, 2);
    } else {
        snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", t.tm_hour, t.tm_min);
        tft.setTextDatum(MC_DATUM);
        tft.setTextColor(INK, WHITE);
        tft.drawString(timeBuf, 120, 100, 6);
    }

    // Classic GBC Double-Border Dialogue Box (Date + Autonomous Live Weather)
    tft.fillRect(8, 158, 224, 72, INK);
    tft.fillRect(12, 162, 216, 64, CREAM);
    tft.fillRect(14, 164, 212, 60, INK);
    tft.fillRect(16, 166, 208, 56, CREAM);

    char dateBuf[32];
    snprintf(dateBuf, sizeof(dateBuf), "%s %02d %s",
             DAYS_ES[t.tm_wday % 7], t.tm_mday, MONTHS_ES[t.tm_mon % 12]);

    tft.setTextDatum(ML_DATUM);
    tft.setTextColor(INK, CREAM);
    tft.drawString(dateBuf, 24, 182, 2);

    if (offlineWeatherValid) {
        float dispTemp = offlineCfg.celsius ? offlineTempC : (offlineTempC * 9.0f / 5.0f + 32.0f);
        char tempBuf[24];
        snprintf(tempBuf, sizeof(tempBuf), "%d%s %d%%",
                 (int)roundf(dispTemp), offlineCfg.celsius ? "C" : "F", offlineHumidity);
        tft.setTextDatum(MR_DATUM);
        tft.setTextColor(BLUE, CREAM);
        tft.drawString(tempBuf, 216, 182, 2);

        String line2 = offlineDesc.length() > 0 ? offlineDesc : offlineCityResp;
        if (line2.length() > 16) line2 = line2.substring(0, 16);
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor(MUTED, CREAM);
        tft.drawString(line2, 24, 205, 2);
    } else {
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor(MUTED, CREAM);
        tft.drawString("RELOJ NTP AUTONOMO", 24, 205, 2);
    }

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
        "<h2>POKE-DISPLAY RAM v1.2</h2>"
        "<a class='hub' href='http://pokeclock.local/'>Abrir Panel Poké-Hub (PC)</a>"
    );
    html += F("<p>IP: ");
    html += WiFi.localIP().toString();
    html += F("<br>Modo: ");
    html += standbyActive ? F("Offline (NTP + Clima autónomo)") : F("Controlado por PC");
    html += F("<br>Clima offline: ");
    if (offlineWeatherValid) {
        html += offlineCityResp + " " + String((int)roundf(offlineTempC)) + "C (" + offlineDesc + ")";
    } else {
        html += String(offlineCfg.city) + " (pendiente)";
    }
    html += F("<br>Frames: ");
    html += String(frameCount);
    html += F("<br>Last blit: ");
    html += String(lastDecodeMs);
    html += F(" ms<br>Free heap: ");
    html += String(ESP.getFreeHeap());
    html += F(" B</p><a href='/ota'>Firmware OTA</a><a href='/status'>JSON Status</a><a href='/offline_weather'>Test Clima</a></div></body></html>");
    server.send(200, "text/html", html);
}

static void handleStatus() {
    if (server.hasArg("keepalive")) {
        lastFrameMs = millis();
        lastHubIp = server.client().remoteIP().toString();
    }
    uint32_t weatherAgeS = offlineWeatherValid ? ((millis() - offlineWeatherUpdatedMs) / 1000) : 0;
    String out;
    out.reserve(512);
    out += F("{\"device\":\"poke-display\",\"version\":\"1.2.0\",\"partial_frame\":true,\"ip\":\"");
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
    out += F(",\"offline_city\":\"");
    out += jsonEscape(offlineCityResp.length() > 0 ? offlineCityResp : String(offlineCfg.city));
    out += F("\",\"offline_weather_ok\":");
    out += offlineWeatherValid ? F("true") : F("false");
    out += F(",\"offline_temp_c\":");
    out += String(offlineTempC, 1);
    out += F(",\"offline_humidity\":");
    out += String(offlineHumidity);
    out += F(",\"offline_desc\":\"");
    out += jsonEscape(offlineDesc);
    out += F("\",\"offline_weather_hm\":\"");
    out += jsonEscape(offlineWeatherUpdatedHm);
    out += F("\",\"offline_weather_age_s\":");
    out += String(weatherAgeS);
    out += F(",\"uptime_s\":");
    out += String(millis() / 1000);
    out += F("}");
    server.send(200, "application/json", out);
}

static void handleOfflineCfg() {
    OfflineConfig nextCfg = offlineCfg;
    nextCfg.magic = EEPROM_MAGIC;
    bool weatherChanged = false;
    if (server.hasArg("city") && server.arg("city").length() > 0) {
        String c = server.arg("city");
        c.trim();
        if (c != String(nextCfg.city)) weatherChanged = true;
        copyStrSafe(nextCfg.city, sizeof(nextCfg.city), c.c_str());
    }
    if (server.hasArg("key") && server.arg("key").length() > 0) {
        String k = server.arg("key");
        k.trim();
        if (k != String(nextCfg.weatherKey)) weatherChanged = true;
        copyStrSafe(nextCfg.weatherKey, sizeof(nextCfg.weatherKey), k.c_str());
    }
    if (server.hasArg("name") && server.arg("name").length() > 0) {
        String n = sanitizeAsciiUpper(server.arg("name"), 14);
        if (n.length() > 0) copyStrSafe(nextCfg.trainerName, sizeof(nextCfg.trainerName), n.c_str());
    }
    if (server.hasArg("c")) {
        nextCfg.celsius = (server.arg("c").toInt() != 0) ? 1 : 0;
    }
    if (server.hasArg("h12")) {
        nextCfg.hour12 = (server.arg("h12").toInt() != 0) ? 1 : 0;
    }
    bool saved = saveOfflineConfigIfChanged(nextCfg);
    if (weatherChanged) {
        offlineWeatherValid = false;
    }
    if (standbyActive) {
        lastStandbyMin = -1;
    }
    String resp = "{\"ok\":true,\"saved\":" + String(saved ? "true" : "false") +
                  ",\"city\":\"" + jsonEscape(String(offlineCfg.city)) + "\"}";
    server.send(200, "application/json", resp);
}

static void handleOfflineWeatherNow() {
    bool ok = fetchOfflineWeather();
    if (ok && standbyActive) {
        lastStandbyMin = -1;
    }
    String resp = "{\"ok\":" + String(ok ? "true" : "false") +
                  ",\"city\":\"" + jsonEscape(offlineCityResp.length() > 0 ? offlineCityResp : String(offlineCfg.city)) +
                  "\",\"temp_c\":" + String(offlineTempC, 1) +
                  ",\"humidity\":" + String(offlineHumidity) +
                  ",\"desc\":\"" + jsonEscape(offlineDesc) +
                  "\",\"updated_hm\":\"" + jsonEscape(offlineWeatherUpdatedHm) + "\"}";
    server.send(ok ? 200 : 502, "application/json", resp);
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

    int16_t drawX = server.hasArg("x") ? (int16_t)server.arg("x").toInt() : 0;
    int16_t drawY = server.hasArg("y") ? (int16_t)server.arg("y").toInt() : 0;
    if (drawX < 0 || drawX >= 240) drawX = 0;
    if (drawY < 0 || drawY >= 240) drawY = 0;

    uint32_t t0 = millis();
    JRESULT res = TJpgDec.drawJpg(drawX, drawY, src, len);
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
    loadOfflineConfig();
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
        drawSplash("READY v1.2!", ipStr.c_str(), "Waiting for PC");
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
    server.on("/offline_cfg", HTTP_GET, handleOfflineCfg);
    server.on("/offline_weather", HTTP_GET, handleOfflineWeatherNow);
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
        // Autonomous NTP standby clock + autonomous OpenWeatherMap refresh if PC is off/asleep >90s
        if (now - lastFrameMs > STANDBY_TIMEOUT_MS && (now - lastStandbyCheck > 2000)) {
            lastStandbyCheck = now;
            bool needWeather = (!offlineWeatherValid && (lastWeatherAttemptMs == 0 || (now - lastWeatherAttemptMs) >= OFFLINE_WEATHER_RETRY_MS)) ||
                               (offlineWeatherValid && (now - offlineWeatherUpdatedMs) >= OFFLINE_WEATHER_INTERVAL_MS);
            bool weatherUpdated = false;
            if (needWeather) {
                weatherUpdated = fetchOfflineWeather();
            }
            time_t tnow = time(nullptr);
            if (tnow > 1700000000) {
                struct tm tinfo;
                localtime_r(&tnow, &tinfo);
                if (!standbyActive || weatherUpdated || tinfo.tm_min != lastStandbyMin) {
                    standbyActive = true;
                    lastStandbyMin = tinfo.tm_min;
                    drawStandbyClock(tinfo);
                }
            }
        }
    }
    yield();
}
