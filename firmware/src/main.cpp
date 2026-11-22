/**
 * PokeDisplay Thin RAM-Stream Firmware v1.3.0 for JUZIPi SD PRO (ESP8266 + ST7789 240x240)
 *
 * Ultra-lean "Dumb Screen" receiver controlled by Poké-Hub on the PC, with autonomous
 * offline NTP clock + OpenWeatherMap weather updates AND persistent EEPROM offline cache:
 *   - POST /frame          : Streams full 240x240 or partial (?x=&y=) baseline JPEG into static RAM
 *                            and blits to ST7789 via 40MHz SPI (~40-110ms, ZERO flash wear!)
 *   - GET  /brightness     : ?val=0..100 (inverted PWM on GPIO5, debounced EEPROM persistence)
 *   - GET  /rotation       : ?val=0..3 (debounced EEPROM persistence)
 *   - GET  /offline_cfg    : Syncs offline city/key/units/12h/trainer name (+ optional weather snapshot)
 *                            from PC (persisted only on meaningful change)
 *   - GET  /offline_weather: Triggers an autonomous weather fetch directly over Wi-Fi (persisted to EEPROM)
 *   - GET  /status         : Live JSON telemetry (RSSI, heap, frames, last_ms, standby, offline weather)
 *   - GET  /               : Status page with direct link to Poké-Hub on your PC
 *   - GET  /ota            : Built-in Web OTA updater (update custom or restore stock SDP .bin)
 *   - Offline Persistence  : Saves config, brightness, rotation, and latest weather snapshot in EEPROM
 *                            with FNV-1a checksum so power/Wi-Fi loss immediately restores last weather!
 *   - Auto Weather Sync    : Automatically syncs weather shortly after boot, on Wi-Fi reconnect, and
 *                            every 15 min (without waiting for standby), persisting updates to EEPROM.
 *   - Resilient Recovery   : If Wi-Fi fails at boot (e.g. router booting after power outage), keeps
 *                            "PokeDisplay-OTA" AP alive while showing cached offline weather AND
 *                            continuously auto-reconnecting STA until Wi-Fi + NTP + weather recover!
 */

#include <Arduino.h>
#include <EEPROM.h>
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <Updater.h>
#include <math.h>
#include <stddef.h>
#include <time.h>
#include <TFT_eSPI.h>
#include <TJpg_Decoder.h>

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Missing firmware/src/secrets.h — copy firmware/src/secrets.h.example to firmware/src/secrets.h"
#endif

static const char* HOSTNAME      = "poke-display";
static const char* FW_VERSION    = "1.3.0";
static const long  TZ_OFFSET_SEC = -5 * 3600; // America/Lima (UTC-5)

static constexpr size_t   MAX_FRAME_BYTES             = 24576;   // 24 KB static RAM buffer
static constexpr size_t   EEPROM_SIZE_BYTES           = 512;
static constexpr uint32_t BOOT_STANDBY_TIMEOUT_MS     = 12000;   // 12s after boot if no PC -> Offline mode
static constexpr uint32_t STANDBY_TIMEOUT_MS          = 90000;   // 90s without PC frame -> Offline mode
static constexpr uint32_t AP_SPLASH_HOLD_MS           = 12000;   // Show AP recovery info 12s before offline screen
static constexpr uint32_t WIFI_RECONNECT_INTERVAL_MS  = 15000;   // 15s STA reconnect retry (even in AP fallback)
static constexpr uint32_t OFFLINE_WEATHER_INTERVAL_MS = 900000;  // 15 min autonomous weather refresh
static constexpr uint32_t OFFLINE_WEATHER_RETRY_MS    = 30000;   // 30s retry on transient Wi-Fi/WAN error
static constexpr uint32_t EEPROM_SAVE_DEBOUNCE_MS     = 4000;    // 4s debounce for brightness/rotation writes
static constexpr uint32_t EEPROM_MAGIC_V12            = 0x504B1201;
static constexpr uint32_t EEPROM_MAGIC                = 0x504B1301;

struct OfflineConfigV12 {
    uint32_t magic;
    char city[40];
    char weatherKey[48];
    char trainerName[16];
    uint8_t celsius;
    uint8_t hour12;
};

struct OfflineState {
    uint32_t magic;
    char city[40];
    char weatherKey[48];
    char trainerName[16];
    uint8_t celsius;
    uint8_t hour12;
    uint8_t brightness;
    uint8_t rotation;
    // Persisted offline weather snapshot (survives power loss & Wi-Fi loss)
    uint8_t weatherValid;
    uint8_t reserved;
    int16_t humidity;
    float tempC;
    char desc[28];
    char cityResp[20];
    char updatedHm[8];
    uint32_t updatedEpoch;
    uint32_t checksum;
};

static_assert(sizeof(OfflineState) <= EEPROM_SIZE_BYTES, "OfflineState exceeds EEPROM_SIZE_BYTES");

static OfflineState offlineState;

static uint8_t frameBuf[MAX_FRAME_BYTES];
static size_t frameLen = 0;
static bool frameOverflow = false;

TFT_eSPI tft = TFT_eSPI();
ESP8266WebServer server(80);

static uint8_t  currentBrightness   = 95;
static uint8_t  currentRotation     = 0;
static uint32_t frameCount          = 0;
static uint32_t lastDecodeMs        = 0;
static uint32_t bootMs              = 0;
static uint32_t lastFrameMs         = 0;
static bool     pcContacted         = false;
static bool     wifiWasConnected    = false;
static bool     apFallbackActive    = false;
static uint32_t apStartedMs         = 0;
static bool     standbyActive       = false;
static int      lastStandbyMin      = -1;
static bool     lastStandbyHasTime  = false;
static bool     lastStandbyWifi     = false;
static String   lastHubIp           = "";

// Debounced EEPROM save for brightness / rotation
static bool     displayPrefsDirty   = false;
static uint32_t displayPrefsDirtyMs = 0;

// Autonomous offline weather state
static bool     offlineWeatherValid     = false;
static bool     offlineWeatherFromCache = false;
static bool     weatherSyncedThisBoot   = false;
static bool     weatherSyncRequested    = false;
static uint32_t nextWeatherSyncMs       = 0;
static float    offlineTempC            = 0.0f;
static int      offlineHumidity         = 0;
static String   offlineDesc             = "";
static String   offlineCityResp         = "";
static String   offlineWeatherUpdatedHm = "";
static uint32_t offlineWeatherEpoch     = 0;
static uint32_t offlineWeatherUpdatedMs = 0;
static uint32_t lastWeatherAttemptMs    = 0;

static const char* DAYS_ES[7]   = {"DOM", "LUN", "MAR", "MIE", "JUE", "VIE", "SAB"};
static const char* MONTHS_ES[12] = {"ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"};

// ============================================================
// Offline State EEPROM Helpers (Checksummed & Wear-Protected)
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

static uint32_t calcStateChecksum(const OfflineState& st) {
    const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&st);
    const size_t len = offsetof(OfflineState, checksum);
    uint32_t hash = 2166136261UL;
    for (size_t i = 0; i < len; i++) {
        hash ^= bytes[i];
        hash *= 16777619UL;
    }
    return hash;
}

static void sanitizeOfflineState(OfflineState& st) {
    st.magic = EEPROM_MAGIC;
    st.city[sizeof(st.city) - 1] = '\0';
    st.weatherKey[sizeof(st.weatherKey) - 1] = '\0';
    st.trainerName[sizeof(st.trainerName) - 1] = '\0';
    st.desc[sizeof(st.desc) - 1] = '\0';
    st.cityResp[sizeof(st.cityResp) - 1] = '\0';
    st.updatedHm[sizeof(st.updatedHm) - 1] = '\0';

    if (st.city[0] == '\0') {
        copyStrSafe(st.city, sizeof(st.city), DEFAULT_WEATHER_CITY);
    }
    if (st.weatherKey[0] == '\0') {
        copyStrSafe(st.weatherKey, sizeof(st.weatherKey), DEFAULT_WEATHER_KEY);
    }
    if (st.trainerName[0] == '\0') {
        copyStrSafe(st.trainerName, sizeof(st.trainerName), "POKEGEAR");
    }

    st.celsius = st.celsius ? 1 : 0;
    st.hour12 = st.hour12 ? 1 : 0;
    if (st.brightness > 100) st.brightness = 95;
    st.rotation = st.rotation & 3;
    st.reserved = 0;

    if (st.weatherValid != 1 || !isfinite(st.tempC) || st.tempC < -90.0f || st.tempC > 90.0f) {
        st.weatherValid = 0;
        st.humidity = 0;
        st.tempC = 0.0f;
        st.desc[0] = '\0';
        st.cityResp[0] = '\0';
        st.updatedHm[0] = '\0';
        st.updatedEpoch = 0;
    } else {
        if (st.humidity < 0) st.humidity = 0;
        if (st.humidity > 100) st.humidity = 100;
    }

    st.checksum = calcStateChecksum(st);
}

static bool commitOfflineStateIfChanged(const OfflineState& rawNext) {
    OfflineState next = rawNext;
    sanitizeOfflineState(next);
    if (memcmp(&offlineState, &next, sizeof(OfflineState)) == 0) {
        return false;
    }
    offlineState = next;
    EEPROM.put(0, offlineState);
    return EEPROM.commit();
}

static void loadOfflineState() {
    EEPROM.begin(EEPROM_SIZE_BYTES);
    OfflineState stored;
    EEPROM.get(0, stored);

    bool validV13 = (stored.magic == EEPROM_MAGIC && stored.checksum == calcStateChecksum(stored));
    if (validV13) {
        offlineState = stored;
        sanitizeOfflineState(offlineState);
    } else if (stored.magic == EEPROM_MAGIC_V12) {
        // Migrate existing v1.2 config seamlessly to v1.3
        OfflineConfigV12 oldCfg;
        EEPROM.get(0, oldCfg);
        memset(&offlineState, 0, sizeof(offlineState));
        copyStrSafe(offlineState.city, sizeof(offlineState.city), oldCfg.city);
        copyStrSafe(offlineState.weatherKey, sizeof(offlineState.weatherKey), oldCfg.weatherKey);
        copyStrSafe(offlineState.trainerName, sizeof(offlineState.trainerName), oldCfg.trainerName);
        offlineState.celsius = oldCfg.celsius ? 1 : 0;
        offlineState.hour12 = oldCfg.hour12 ? 1 : 0;
        offlineState.brightness = 95;
        offlineState.rotation = 0;
        sanitizeOfflineState(offlineState);
        EEPROM.put(0, offlineState);
        EEPROM.commit();
    } else {
        memset(&offlineState, 0, sizeof(offlineState));
        copyStrSafe(offlineState.city, sizeof(offlineState.city), DEFAULT_WEATHER_CITY);
        copyStrSafe(offlineState.weatherKey, sizeof(offlineState.weatherKey), DEFAULT_WEATHER_KEY);
        copyStrSafe(offlineState.trainerName, sizeof(offlineState.trainerName), "POKEGEAR");
        offlineState.celsius = 1;
        offlineState.hour12 = 1;
        offlineState.brightness = 95;
        offlineState.rotation = 0;
        sanitizeOfflineState(offlineState);
        EEPROM.put(0, offlineState);
        EEPROM.commit();
    }

    currentBrightness = offlineState.brightness;
    currentRotation = offlineState.rotation;

    if (offlineState.weatherValid) {
        offlineWeatherValid = true;
        offlineWeatherFromCache = true;
        offlineTempC = offlineState.tempC;
        offlineHumidity = offlineState.humidity;
        offlineDesc = String(offlineState.desc);
        offlineCityResp = String(offlineState.cityResp);
        if (offlineCityResp.length() == 0) {
            offlineCityResp = String(offlineState.city);
        }
        offlineWeatherUpdatedHm = String(offlineState.updatedHm);
        offlineWeatherEpoch = offlineState.updatedEpoch;
    } else {
        offlineWeatherValid = false;
        offlineWeatherFromCache = false;
    }
}

static bool persistWeatherSnapshot(bool forceWrite = false) {
    if (!offlineWeatherValid || !isfinite(offlineTempC)) return false;

    char descBuf[sizeof(offlineState.desc)];
    char cityBuf[sizeof(offlineState.cityResp)];
    char hmBuf[sizeof(offlineState.updatedHm)];
    copyStrSafe(descBuf, sizeof(descBuf), offlineDesc.c_str());
    copyStrSafe(cityBuf, sizeof(cityBuf), offlineCityResp.c_str());
    copyStrSafe(hmBuf, sizeof(hmBuf), offlineWeatherUpdatedHm.c_str());

    bool meaningfulChange = forceWrite || (offlineState.weatherValid == 0);
    if (!meaningfulChange) {
        if ((int)roundf(offlineState.tempC) != (int)roundf(offlineTempC) ||
            fabsf(offlineState.tempC - offlineTempC) >= 0.4f) {
            meaningfulChange = true;
        } else if (offlineState.humidity != offlineHumidity) {
            meaningfulChange = true;
        } else if (strcmp(offlineState.desc, descBuf) != 0 ||
                   strcmp(offlineState.cityResp, cityBuf) != 0) {
            meaningfulChange = true;
        } else if (offlineState.updatedHm[0] == '\0' && hmBuf[0] != '\0') {
            meaningfulChange = true;
        } else if (offlineWeatherEpoch > 1700000000UL &&
                   (offlineState.updatedEpoch == 0 ||
                    (offlineWeatherEpoch > offlineState.updatedEpoch &&
                     (offlineWeatherEpoch - offlineState.updatedEpoch) >= 3600UL))) {
            meaningfulChange = true;
        }
    }

    if (!meaningfulChange) {
        return false;
    }

    OfflineState next = offlineState;
    next.weatherValid = 1;
    next.tempC = offlineTempC;
    next.humidity = (int16_t)offlineHumidity;
    copyStrSafe(next.desc, sizeof(next.desc), descBuf);
    copyStrSafe(next.cityResp, sizeof(next.cityResp), cityBuf);
    copyStrSafe(next.updatedHm, sizeof(next.updatedHm), hmBuf);
    next.updatedEpoch = offlineWeatherEpoch;
    return commitOfflineStateIfChanged(next);
}

static void scheduleDisplayPrefsSave() {
    displayPrefsDirty = true;
    displayPrefsDirtyMs = millis();
}

static void flushPendingDisplayPrefs(uint32_t now) {
    if (!displayPrefsDirty || (now - displayPrefsDirtyMs) < EEPROM_SAVE_DEBOUNCE_MS) {
        return;
    }
    displayPrefsDirty = false;
    OfflineState next = offlineState;
    next.brightness = currentBrightness;
    next.rotation = currentRotation;
    commitOfflineStateIfChanged(next);
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

static bool extractJsonUInt32(const String& body, const char* key, uint32_t& outVal) {
    int idx = body.indexOf(key);
    if (idx < 0) return false;
    idx += strlen(key);
    while (idx < (int)body.length() && body[idx] == ' ') idx++;
    int end = idx;
    while (end < (int)body.length() && isDigit(body[end])) end++;
    if (end <= idx) return false;
    outVal = (uint32_t)strtoul(body.substring(idx, end).c_str(), nullptr, 10);
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
    if (offlineState.city[0] == '\0' || offlineState.weatherKey[0] == '\0') return false;

    WiFiClient client;
    client.setTimeout(2500);
    if (!client.connect("api.openweathermap.org", 80)) {
        return false;
    }

    String req;
    req.reserve(240);
    req += F("GET /data/2.5/weather?q=");
    req += urlEncodeParam(offlineState.city);
    req += F("&appid=");
    req += urlEncodeParam(offlineState.weatherKey);
    req += F("&units=metric&lang=es HTTP/1.0\r\n"
             "Host: api.openweathermap.org\r\n"
             "Connection: close\r\n\r\n");
    client.print(req);

    // Wait for response
    uint32_t t0 = millis();
    while (client.connected() && !client.available() && (millis() - t0) < 2500) {
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

    // Read JSON body in small chunks (bounded to protect heap)
    String body;
    body.reserve(1024);
    char chunk[128];
    t0 = millis();
    while ((client.connected() || client.available()) && body.length() < 1536 && (millis() - t0) < 2500) {
        int avail = client.available();
        if (avail > 0) {
            int toRead = avail;
            if (toRead > (int)sizeof(chunk) - 1) toRead = sizeof(chunk) - 1;
            int remaining = 1536 - (int)body.length();
            if (toRead > remaining) toRead = remaining;
            int n = client.read(reinterpret_cast<uint8_t*>(chunk), toRead);
            if (n > 0) {
                chunk[n] = '\0';
                body += chunk;
            }
        } else {
            delay(5);
        }
        yield();
    }
    client.stop();

    float tempVal = 0.0f;
    float humVal  = 0.0f;
    uint32_t dtVal = 0;
    String descRaw;
    String cityRaw;

    if (!extractJsonFloat(body, "\"temp\":", tempVal) || !isfinite(tempVal)) {
        return false;
    }
    extractJsonFloat(body, "\"humidity\":", humVal);
    extractJsonString(body, "\"description\":\"", descRaw);
    extractJsonString(body, "\"name\":\"", cityRaw);
    extractJsonUInt32(body, "\"dt\":", dtVal);

    int humInt = (int)roundf(humVal);
    if (humInt < 0) humInt = 0;
    if (humInt > 100) humInt = 100;

    offlineTempC = tempVal;
    offlineHumidity = humInt;
    offlineDesc = sanitizeAsciiUpper(descRaw, 22);
    offlineCityResp = sanitizeAsciiUpper(cityRaw.length() > 0 ? cityRaw : String(offlineState.city), 14);
    offlineWeatherValid = true;
    offlineWeatherFromCache = false;
    weatherSyncedThisBoot = true;
    offlineWeatherUpdatedMs = millis();

    time_t tnow = time(nullptr);
    if (tnow > 1700000000) {
        struct tm tinfo;
        localtime_r(&tnow, &tinfo);
        char hm[8];
        snprintf(hm, sizeof(hm), "%02d:%02d", tinfo.tm_hour, tinfo.tm_min);
        offlineWeatherUpdatedHm = String(hm);
        offlineWeatherEpoch = (uint32_t)tnow;
    } else if (dtVal > 1700000000UL) {
        time_t localTs = (time_t)dtVal + TZ_OFFSET_SEC;
        struct tm tinfo;
        gmtime_r(&localTs, &tinfo);
        char hm[8];
        snprintf(hm, sizeof(hm), "%02d:%02d", tinfo.tm_hour, tinfo.tm_min);
        offlineWeatherUpdatedHm = String(hm);
        offlineWeatherEpoch = dtVal;
    }

    persistWeatherSnapshot();
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

static void drawStandbyClock(const struct tm* t, bool hasTime, bool wifiOnline) {
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
    tft.drawString(offlineState.trainerName, 16, 28, 2);

    tft.setTextDatum(MR_DATUM);
    if (!wifiOnline) {
        tft.setTextColor(RED, CREAM);
        tft.drawString("SIN WIFI", 224, 28, 2);
    } else if (!hasTime) {
        tft.setTextColor(BLUE, CREAM);
        tft.drawString("SIN NTP", 224, 28, 2);
    } else {
        tft.setTextColor(BLUE, CREAM);
        tft.drawString("PC OFF", 224, 28, 2);
    }

    // Big Clock Box (static, zero animations in offline mode)
    tft.fillRect(10, 52, 222, 92, INK);
    tft.fillRect(12, 54, 218, 88, WHITE);
    tft.fillRect(12, 54, 218, 4, wifiOnline ? BLUE : RED);

    if (hasTime && t != nullptr) {
        char timeBuf[16];
        if (offlineState.hour12) {
            int h12 = t->tm_hour % 12;
            if (h12 == 0) h12 = 12;
            snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", h12, t->tm_min);
            tft.setTextDatum(MC_DATUM);
            tft.setTextColor(INK, WHITE);
            tft.drawString(timeBuf, 102, 100, 6);

            tft.fillRect(184, 108, 36, 20, BLUE);
            tft.setTextColor(WHITE, BLUE);
            tft.drawString(t->tm_hour >= 12 ? "PM" : "AM", 202, 118, 2);
        } else {
            snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", t->tm_hour, t->tm_min);
            tft.setTextDatum(MC_DATUM);
            tft.setTextColor(INK, WHITE);
            tft.drawString(timeBuf, 120, 100, 6);
        }
    } else {
        tft.setTextDatum(MC_DATUM);
        tft.setTextColor(MUTED, WHITE);
        tft.drawString("--:--", 120, 100, 6);
    }

    // Classic GBC Double-Border Dialogue Box (Date + Live/Cached Offline Weather)
    tft.fillRect(8, 158, 224, 72, INK);
    tft.fillRect(12, 162, 216, 64, CREAM);
    tft.fillRect(14, 164, 212, 60, INK);
    tft.fillRect(16, 166, 208, 56, CREAM);

    tft.setTextDatum(ML_DATUM);
    tft.setTextColor(INK, CREAM);
    if (hasTime && t != nullptr) {
        char dateBuf[32];
        snprintf(dateBuf, sizeof(dateBuf), "%s %02d %s",
                 DAYS_ES[t->tm_wday % 7], t->tm_mday, MONTHS_ES[t->tm_mon % 12]);
        tft.drawString(dateBuf, 24, 182, 2);
    } else if (offlineWeatherValid && offlineCityResp.length() > 0) {
        String cityLbl = offlineCityResp;
        if (cityLbl.length() > 11) cityLbl = cityLbl.substring(0, 11);
        tft.drawString(cityLbl, 24, 182, 2);
    } else {
        tft.drawString("MODO OFFLINE", 24, 182, 2);
    }

    if (offlineWeatherValid) {
        float dispTemp = offlineState.celsius ? offlineTempC : (offlineTempC * 9.0f / 5.0f + 32.0f);
        char tempBuf[24];
        snprintf(tempBuf, sizeof(tempBuf), "%d%s %d%%",
                 (int)roundf(dispTemp), offlineState.celsius ? "C" : "F", offlineHumidity);
        tft.setTextDatum(MR_DATUM);
        tft.setTextColor(BLUE, CREAM);
        tft.drawString(tempBuf, 216, 182, 2);

        String baseDesc = offlineDesc.length() > 0 ? offlineDesc : offlineCityResp;
        String line2 = baseDesc;
        if (offlineWeatherFromCache && offlineWeatherUpdatedHm.length() == 5) {
            line2 = offlineWeatherUpdatedHm + " " + baseDesc;
        }
        if (line2.length() > 18) line2 = line2.substring(0, 18);
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor(MUTED, CREAM);
        tft.drawString(line2, 24, 205, 2);
    } else {
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor(MUTED, CREAM);
        tft.drawString(wifiOnline ? "SINCRONIZANDO..." : "BUSCANDO WIFI...", 24, 205, 2);
    }

    // Red triangle cursor
    tft.fillTriangle(210, 208, 218, 208, 214, 214, RED);
}

// ============================================================
// Wi-Fi Session Helper
// ============================================================
static void onWifiConnected(bool showReadySplash) {
    bool wasAp = apFallbackActive;
    if (apFallbackActive) {
        WiFi.softAPdisconnect(true);
        WiFi.mode(WIFI_STA);
        apFallbackActive = false;
    }
    wifiWasConnected = true;
    MDNS.begin(HOSTNAME);
    configTime(TZ_OFFSET_SEC, 0, "pool.ntp.org", "time.nist.gov");
    weatherSyncRequested = true;
    nextWeatherSyncMs = millis() + (showReadySplash ? 1500 : 500);

    if (showReadySplash) {
        String ipStr = WiFi.localIP().toString();
        drawSplash("READY v1.3!", ipStr.c_str(), "Waiting for PC");
    } else if (standbyActive || wasAp) {
        lastStandbyMin = -1;
    }
}

// ============================================================
// HTTP Handlers
// ============================================================
static void handleRoot() {
    bool wifiOnline = (WiFi.status() == WL_CONNECTED);
    String html = F(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>PokeDisplay</title>"
        "<style>body{font-family:monospace;background:#f8f8f0;color:#181820;max-width:440px;margin:24px auto;padding:16px;}"
        ".box{border:3px solid #181820;padding:18px;background:#fff;box-shadow:4px 4px 0 #c8d0c0;}"
        "a{display:inline-block;padding:10px 14px;background:#181820;color:#fff;text-decoration:none;margin:6px 6px 0 0;font-weight:bold;}"
        "a.hub{background:#24a848;display:block;text-align:center;margin-bottom:10px;}"
        "</style></head><body><div class='box'>"
        "<h2>POKE-DISPLAY RAM v1.3</h2>"
        "<a class='hub' href='http://pokeclock.local/'>Abrir Panel Poké-Hub (PC)</a>"
    );
    html += F("<p>IP: ");
    html += wifiOnline ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
    html += F("<br>Wi-Fi: ");
    html += wifiOnline ? F("Conectado") : F("Sin Wi-Fi (AP + reconexión)");
    html += F("<br>Modo: ");
    html += standbyActive ? F("Offline (NTP + Clima autónomo)") : F("Controlado por PC");
    html += F("<br>Clima offline: ");
    if (offlineWeatherValid) {
        html += offlineCityResp + " " + String((int)roundf(offlineTempC)) + "C (" + offlineDesc + ")";
        if (offlineWeatherFromCache) {
            html += F(" [cache EEPROM");
            if (offlineWeatherUpdatedHm.length() > 0) {
                html += " " + offlineWeatherUpdatedHm;
            }
            html += F("]");
        }
    } else {
        html += String(offlineState.city) + " (pendiente)";
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
        pcContacted = true;
        lastHubIp = server.client().remoteIP().toString();
    }
    bool wifiOnline = (WiFi.status() == WL_CONNECTED);
    uint32_t weatherAgeS = 0;
    if (offlineWeatherValid) {
        if (!offlineWeatherFromCache && offlineWeatherUpdatedMs > 0) {
            weatherAgeS = (millis() - offlineWeatherUpdatedMs) / 1000;
        } else if (offlineWeatherEpoch > 1700000000UL) {
            time_t tnow = time(nullptr);
            if (tnow > (time_t)offlineWeatherEpoch) {
                weatherAgeS = (uint32_t)(tnow - (time_t)offlineWeatherEpoch);
            }
        }
    }

    String out;
    out.reserve(600);
    out += F("{\"device\":\"poke-display\",\"version\":\"");
    out += FW_VERSION;
    out += F("\",\"partial_frame\":true,\"ip\":\"");
    out += wifiOnline ? WiFi.localIP().toString() : WiFi.softAPIP().toString();
    out += F("\",\"hub_ip\":\"");
    out += lastHubIp;
    out += F("\",\"wifi_connected\":");
    out += wifiOnline ? F("true") : F("false");
    out += F(",\"ap_fallback\":");
    out += apFallbackActive ? F("true") : F("false");
    out += F(",\"rssi\":");
    out += String(wifiOnline ? WiFi.RSSI() : 0);
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
    out += jsonEscape(offlineCityResp.length() > 0 ? offlineCityResp : String(offlineState.city));
    out += F("\",\"offline_weather_ok\":");
    out += offlineWeatherValid ? F("true") : F("false");
    out += F(",\"offline_weather_cached\":");
    out += offlineWeatherFromCache ? F("true") : F("false");
    out += F(",\"offline_weather_synced\":");
    out += weatherSyncedThisBoot ? F("true") : F("false");
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
    OfflineState nextState = offlineState;
    nextState.brightness = currentBrightness;
    nextState.rotation = currentRotation;
    bool weatherChanged = false;
    bool weatherProvided = false;

    if (server.hasArg("city") && server.arg("city").length() > 0) {
        String c = server.arg("city");
        c.trim();
        if (c != String(nextState.city)) weatherChanged = true;
        copyStrSafe(nextState.city, sizeof(nextState.city), c.c_str());
    }
    if (server.hasArg("key") && server.arg("key").length() > 0) {
        String k = server.arg("key");
        k.trim();
        if (k != String(nextState.weatherKey)) weatherChanged = true;
        copyStrSafe(nextState.weatherKey, sizeof(nextState.weatherKey), k.c_str());
    }
    if (server.hasArg("name") && server.arg("name").length() > 0) {
        String n = sanitizeAsciiUpper(server.arg("name"), 14);
        if (n.length() > 0) copyStrSafe(nextState.trainerName, sizeof(nextState.trainerName), n.c_str());
    }
    if (server.hasArg("c")) {
        nextState.celsius = (server.arg("c").toInt() != 0) ? 1 : 0;
    }
    if (server.hasArg("h12")) {
        nextState.hour12 = (server.arg("h12").toInt() != 0) ? 1 : 0;
    }

    // Optional live weather snapshot pushed by PC (immediately persisted offline!)
    if (server.hasArg("temp") && server.arg("temp").length() > 0) {
        float tVal = server.arg("temp").toFloat();
        if (isfinite(tVal) && tVal >= -90.0f && tVal <= 90.0f) {
            weatherProvided = true;
            int hVal = server.hasArg("hum") ? server.arg("hum").toInt() : offlineHumidity;
            if (hVal < 0) hVal = 0;
            if (hVal > 100) hVal = 100;

            String wCity = server.hasArg("wcity") && server.arg("wcity").length() > 0
                               ? sanitizeAsciiUpper(server.arg("wcity"), 14)
                               : sanitizeAsciiUpper(String(nextState.city), 14);
            String wDesc = server.hasArg("desc") ? sanitizeAsciiUpper(server.arg("desc"), 22) : offlineDesc;
            String wHm   = server.hasArg("hm") ? server.arg("hm") : offlineWeatherUpdatedHm;
            wHm.trim();
            if (wHm.length() > 5) wHm = wHm.substring(0, 5);

            offlineTempC = tVal;
            offlineHumidity = hVal;
            offlineCityResp = wCity;
            offlineDesc = wDesc;
            if (wHm.length() > 0) offlineWeatherUpdatedHm = wHm;
            offlineWeatherValid = true;
            offlineWeatherFromCache = false;
            weatherSyncedThisBoot = true;
            offlineWeatherUpdatedMs = millis();

            time_t tnow = time(nullptr);
            if (tnow > 1700000000) {
                offlineWeatherEpoch = (uint32_t)tnow;
            }

            nextState.weatherValid = 1;
            nextState.tempC = offlineTempC;
            nextState.humidity = (int16_t)offlineHumidity;
            copyStrSafe(nextState.cityResp, sizeof(nextState.cityResp), offlineCityResp.c_str());
            copyStrSafe(nextState.desc, sizeof(nextState.desc), offlineDesc.c_str());
            copyStrSafe(nextState.updatedHm, sizeof(nextState.updatedHm), offlineWeatherUpdatedHm.c_str());
            nextState.updatedEpoch = offlineWeatherEpoch;
        }
    }

    bool saved = commitOfflineStateIfChanged(nextState);
    if (weatherChanged && !weatherProvided) {
        // Keep previous cached weather until new city fetches, but force immediate sync
        weatherSyncedThisBoot = false;
        weatherSyncRequested = true;
        nextWeatherSyncMs = millis();
    }
    if (standbyActive) {
        lastStandbyMin = -1;
    }
    String resp = "{\"ok\":true,\"saved\":" + String(saved ? "true" : "false") +
                  ",\"weather_synced\":" + String(weatherProvided ? "true" : "false") +
                  ",\"city\":\"" + jsonEscape(String(offlineState.city)) + "\"}";
    server.send(200, "application/json", resp);
}

static void handleOfflineWeatherNow() {
    bool ok = fetchOfflineWeather();
    if (ok && standbyActive) {
        lastStandbyMin = -1;
    }
    String resp = "{\"ok\":" + String(ok ? "true" : "false") +
                  ",\"city\":\"" + jsonEscape(offlineCityResp.length() > 0 ? offlineCityResp : String(offlineState.city)) +
                  "\",\"temp_c\":" + String(offlineTempC, 1) +
                  ",\"humidity\":" + String(offlineHumidity) +
                  ",\"desc\":\"" + jsonEscape(offlineDesc) +
                  "\",\"updated_hm\":\"" + jsonEscape(offlineWeatherUpdatedHm) +
                  "\",\"cached\":" + String(offlineWeatherFromCache ? "true" : "false") + "}";
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
    pcContacted = true;
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
    scheduleDisplayPrefsSave();
    server.send(200, "application/json", "{\"ok\":true,\"brightness\":" + String(currentBrightness) + "}");
}

static void handleRotation() {
    if (!server.hasArg("val")) {
        server.send(400, "application/json", "{\"ok\":false,\"error\":\"missing val\"}");
        return;
    }
    currentRotation = (uint8_t)(server.arg("val").toInt() & 3);
    tft.setRotation(currentRotation);
    scheduleDisplayPrefsSave();
    lastStandbyMin = -1;
    server.send(200, "application/json", "{\"ok\":true,\"rotation\":" + String(currentRotation) + "}");
}

static void handleRestart() {
    flushPendingDisplayPrefs(millis() + EEPROM_SAVE_DEBOUNCE_MS);
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
    loadOfflineState();
    applyBrightness(currentBrightness);

    tft.init();
    tft.setRotation(currentRotation);
    tft.setSwapBytes(true);

    TJpgDec.setJpgScale(1);
    TJpgDec.setCallback(tftOutputCallback);

    if (offlineWeatherValid) {
        float dispTemp = offlineState.celsius ? offlineTempC : (offlineTempC * 9.0f / 5.0f + 32.0f);
        char cachedLine[32];
        snprintf(cachedLine, sizeof(cachedLine), "%.10s %d%s",
                 offlineCityResp.c_str(), (int)roundf(dispTemp), offlineState.celsius ? "C" : "F");
        drawSplash("Connecting WiFi", WIFI_SSID, cachedLine);
    } else {
        drawSplash("Connecting WiFi", WIFI_SSID, "Please wait...");
    }

    WiFi.persistent(false);
    WiFi.setAutoReconnect(true);
    WiFi.mode(WIFI_STA);
    WiFi.setSleepMode(WIFI_NONE_SLEEP);
    WiFi.hostname(HOSTNAME);
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    uint32_t startMs = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - startMs) < 20000) {
        delay(200);
        yield();
    }

    bootMs = millis();
    lastFrameMs = bootMs;

    if (WiFi.status() == WL_CONNECTED) {
        onWifiConnected(true);
    } else {
        wifiWasConnected = false;
        apFallbackActive = true;
        apStartedMs = bootMs;
        WiFi.mode(WIFI_AP_STA);
        WiFi.softAP("PokeDisplay-OTA");
        WiFi.begin(WIFI_SSID, WIFI_PASS); // Keep STA auto-reconnecting in background
        drawSplash("AP RECOVERY", "PokeDisplay-OTA", "192.168.4.1/ota");
    }

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
    uint32_t now = millis();

    flushPendingDisplayPrefs(now);

    // 1. Continuous Wi-Fi monitoring & auto-recovery (works even from AP fallback!)
    bool wifiOnline = (WiFi.status() == WL_CONNECTED);
    if (wifiOnline) {
        if (!wifiWasConnected || apFallbackActive) {
            onWifiConnected(false);
        }
        MDNS.update();
    } else {
        if (wifiWasConnected) {
            wifiWasConnected = false;
            if (standbyActive) {
                lastStandbyMin = -1;
            }
        }
        if (now - lastWifiCheck >= WIFI_RECONNECT_INTERVAL_MS) {
            lastWifiCheck = now;
            WiFi.begin(WIFI_SSID, WIFI_PASS);
        }
    }

    // 2. Autonomous OpenWeatherMap sync (runs on boot, on Wi-Fi recovery, and every 15 min)
    if (wifiOnline) {
        uint32_t retryInterval = (pcContacted && !standbyActive)
                                     ? (OFFLINE_WEATHER_RETRY_MS * 2)
                                     : OFFLINE_WEATHER_RETRY_MS;
        bool bootOrRequested = weatherSyncRequested && (int32_t)(now - nextWeatherSyncMs) >= 0;
        bool initialRetry = !weatherSyncedThisBoot &&
                            (int32_t)(now - nextWeatherSyncMs) >= 0 &&
                            (lastWeatherAttemptMs == 0 || (now - lastWeatherAttemptMs) >= retryInterval);
        bool periodicRefresh = weatherSyncedThisBoot &&
                               (now - offlineWeatherUpdatedMs) >= OFFLINE_WEATHER_INTERVAL_MS;

        if (bootOrRequested || initialRetry || periodicRefresh) {
            weatherSyncRequested = false;
            bool ok = fetchOfflineWeather();
            if (ok && standbyActive) {
                lastStandbyMin = -1;
            }
        }
    }

    // 3. Autonomous standby / offline clock + cached/live weather display
    bool shouldStandby = false;
    if (pcContacted) {
        shouldStandby = (now - lastFrameMs) > STANDBY_TIMEOUT_MS;
    } else if (apFallbackActive) {
        shouldStandby = (now - apStartedMs) > AP_SPLASH_HOLD_MS;
    } else {
        shouldStandby = (now - bootMs) > BOOT_STANDBY_TIMEOUT_MS;
    }

    if (shouldStandby && (now - lastStandbyCheck > 2000 || !standbyActive || lastStandbyMin == -1)) {
        lastStandbyCheck = now;
        time_t tnow = time(nullptr);
        bool hasTime = (tnow > 1700000000);

        if (hasTime) {
            struct tm tinfo;
            localtime_r(&tnow, &tinfo);
            // Backfill updatedHm if weather synced right before NTP finished
            if (offlineWeatherValid && weatherSyncedThisBoot && offlineWeatherUpdatedHm.length() == 0) {
                char hm[8];
                snprintf(hm, sizeof(hm), "%02d:%02d", tinfo.tm_hour, tinfo.tm_min);
                offlineWeatherUpdatedHm = String(hm);
                offlineWeatherEpoch = (uint32_t)tnow;
                persistWeatherSnapshot();
            }
            if (!standbyActive || tinfo.tm_min != lastStandbyMin ||
                !lastStandbyHasTime || lastStandbyWifi != wifiOnline) {
                standbyActive = true;
                lastStandbyMin = tinfo.tm_min;
                lastStandbyHasTime = true;
                lastStandbyWifi = wifiOnline;
                drawStandbyClock(&tinfo, true, wifiOnline);
            }
        } else {
            // No NTP yet (e.g. router or internet still offline): still show cached weather!
            if (!standbyActive || lastStandbyMin == -1 ||
                lastStandbyHasTime || lastStandbyWifi != wifiOnline) {
                standbyActive = true;
                lastStandbyMin = -2;
                lastStandbyHasTime = false;
                lastStandbyWifi = wifiOnline;
                drawStandbyClock(nullptr, false, wifiOnline);
            }
        }
    }

    yield();
}
