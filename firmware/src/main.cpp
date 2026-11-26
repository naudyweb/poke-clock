/**
 * PokeDisplay Thin RAM-Stream Firmware v1.5.0 for JUZIPi SD PRO (ESP8266 + ST7789 240x240)
 *
 * Ultra-lean "Dumb Screen" receiver controlled by Poké-Hub on the PC, with autonomous
 * offline Pokémon GBC clock + OpenWeatherMap weather updates AND persistent EEPROM cache:
 *   - POST /frame          : Streams full 240x240 or partial (?x=&y=) baseline JPEG into static RAM
 *                            and blits to ST7789 via 40MHz SPI (ZERO flash wear!)
 *   - GET  /brightness     : ?val=0..100 (inverted PWM on GPIO5, debounced EEPROM persistence)
 *   - GET  /display        : ?on=0|1 (ST7789 sleep while Wi-Fi remains active)
 *   - GET  /rotation       : ?val=0..3 (debounced EEPROM persistence)
 *   - GET|POST /offline_cfg: Syncs offline city/key/units/12h/trainer/poke/pal/wicon (+ weather snapshot)
 *                            from PC (wear-protected EEPROM persistence with hysteresis)
 *   - GET  /offline_weather: Triggers an autonomous weather fetch directly over Wi-Fi (persisted to EEPROM)
 *   - GET|POST /standby    : Forces immediate transition to offline Pokémon GBC screen
 *   - GET  /status         : Live JSON telemetry (RSSI, heap, frames, last_ms, standby, offline weather,
 *                            weather diagnostics, active Pokémon/palette/weather icon)
 *   - GET  /               : Status page with direct link to Poké-Hub on your PC
 *   - GET  /ota            : Built-in Web OTA updater (update custom or restore stock SDP .bin)
 */

#include <Arduino.h>
#include <EEPROM.h>
#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ESP8266mDNS.h>
#include <Updater.h>
#include <math.h>
#include <stddef.h>
#include <stdlib.h>
#include <time.h>
#include <TFT_eSPI.h>
#include <TJpg_Decoder.h>

#include "poke_sprites.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#error "Missing firmware/src/secrets.h — copy firmware/src/secrets.h.example to firmware/src/secrets.h"
#endif

static const char* HOSTNAME      = "poke-display";
static const char* FW_VERSION    = "1.5.0";
static const long  TZ_OFFSET_SEC = -5 * 3600; // America/Lima (UTC-5)

static constexpr size_t   MAX_FRAME_BYTES             = 24576;   // 24 KB static RAM buffer
static constexpr size_t   EEPROM_SIZE_BYTES           = 512;
static constexpr uint32_t BOOT_STANDBY_TIMEOUT_MS     = 12000;   // 12s after boot if no PC -> Offline mode
static constexpr uint32_t STANDBY_TIMEOUT_MS          = 90000;   // 90s without PC frame -> Offline mode
static constexpr uint32_t AP_SPLASH_HOLD_MS           = 12000;   // Show AP recovery info 12s before offline screen
static constexpr uint32_t WIFI_RECONNECT_INTERVAL_MS  = 45000;   // 45s disconnect before manual WiFi.begin()
static constexpr uint32_t OFFLINE_WEATHER_INTERVAL_MS = 900000;  // 15 min autonomous weather refresh
static constexpr uint32_t EEPROM_SAVE_DEBOUNCE_MS     = 4000;    // 4s debounce for brightness/rotation writes
static constexpr uint32_t WEATHER_HTTP_TIMEOUT_MS     = 6000;    // 6s OpenWeatherMap connect/read timeout
static constexpr uint32_t EEPROM_MAGIC_V12            = 0x504B1201;
static constexpr uint32_t EEPROM_MAGIC_V13            = 0x504B1301;
static constexpr uint32_t EEPROM_MAGIC                = 0x504B1401;

static const uint32_t WEATHER_BACKOFF_MS[4] = {30000UL, 60000UL, 180000UL, 900000UL};

struct OfflineConfigV12 {
    uint32_t magic;
    char city[40];
    char weatherKey[48];
    char trainerName[16];
    uint8_t celsius;
    uint8_t hour12;
};

struct OfflineStateV13 {
    uint32_t magic;
    char city[40];
    char weatherKey[48];
    char trainerName[16];
    uint8_t celsius;
    uint8_t hour12;
    uint8_t brightness;
    uint8_t rotation;
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

// Naturally aligned, zero implicit padding bytes
struct OfflineState {
    uint32_t magic;
    char city[40];
    char weatherKey[48];
    char trainerName[16];
    uint8_t celsius;
    uint8_t hour12;
    uint8_t brightness;
    uint8_t rotation;
    uint8_t weatherValid;
    uint8_t pokemonIdx;
    int16_t humidity;
    float tempC;
    char desc[28];
    char cityResp[20];
    char updatedHm[8];
    uint32_t updatedEpoch;
    uint8_t paletteIdx;
    uint8_t weatherIcon;
    uint16_t reserved;
    uint32_t checksum;
};

static_assert(sizeof(OfflineState) <= EEPROM_SIZE_BYTES, "OfflineState exceeds EEPROM_SIZE_BYTES");

static OfflineState offlineState;

static uint8_t frameBuf[MAX_FRAME_BYTES];
static size_t frameLen = 0;
static bool frameOverflow = false;
static uint8_t mcuYieldCounter = 0;

TFT_eSPI tft = TFT_eSPI();
ESP8266WebServer server(80);

static uint8_t  currentBrightness      = 95;
static uint8_t  currentRotation        = 0;
static bool     displaySleeping        = false;
static uint32_t frameCount             = 0;
static uint32_t lastDecodeMs           = 0;
static uint32_t bootMs                 = 0;
static uint32_t lastFrameMs            = 0;
static bool     pcContacted            = false;
static bool     wifiWasConnected       = false;
static bool     apFallbackActive       = false;
static uint32_t apStartedMs            = 0;
static uint32_t wifiDisconnectedSinceMs = 0;
static uint32_t lastWifiBeginMs        = 0;

static bool     standbyActive          = false;
static bool     forceStandbyNow        = false;
static bool     standbyNeedsFullRedraw = true;
static int      lastStandbyMin         = -1;
static int      lastStandbyMday        = -1;
static int      lastStandbyMon         = -1;
static int      lastStandbyWday        = -1;
static bool     lastStandbyHasTime     = false;
static bool     lastStandbyWifi        = false;
static char     lastHubIp[16]          = "";

// Debounced EEPROM save for brightness / rotation
static bool     displayPrefsDirty      = false;
static uint32_t displayPrefsDirtyMs    = 0;

// Autonomous offline weather state & diagnostics
static bool     offlineWeatherValid     = false;
static bool     offlineWeatherFromCache = false;
static bool     weatherSyncedThisBoot   = false;
static bool     weatherSyncRequested    = false;
static uint32_t nextWeatherSyncMs       = 0;
static float    offlineTempC            = 0.0f;
static int      offlineHumidity         = 0;
static char     offlineDesc[28]         = "";
static char     offlineCityResp[20]     = "";
static char     offlineWeatherUpdatedHm[8] = "";
static uint8_t  offlineWeatherIcon      = WEATHER_ICON_SUN;
static uint32_t offlineWeatherEpoch     = 0;
static uint32_t offlineWeatherUpdatedMs = 0;
static uint32_t lastWeatherAttemptMs    = 0;
static uint8_t  weatherFailCount        = 0;
static int      lastWeatherHttpCode     = 0;
static char     lastWeatherErr[20]      = "";

// Fixed HTTP body buffer for OpenWeatherMap (zero String heap reallocations)
static char     weatherBody[2048];

static const char* DAYS_ES[7]    = {"DOM", "LUN", "MAR", "MIE", "JUE", "VIE", "SAB"};
static const char* MONTHS_ES[12] = {"ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"};

// ============================================================
// Safe Fixed-Buffer & EEPROM Helpers
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

static void ipToStr(const IPAddress& ip, char* dst, size_t dstSize) {
    if (!dst || dstSize == 0) return;
    snprintf(dst, dstSize, "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
}

template <typename T>
static uint32_t calcStructChecksum(const T& st) {
    const uint8_t* bytes = reinterpret_cast<const uint8_t*>(&st);
    const size_t len = offsetof(T, checksum);
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
    if (st.pokemonIdx >= POKE_SPRITE_COUNT) st.pokemonIdx = 0;
    if (st.paletteIdx >= GBC_PALETTE_COUNT) st.paletteIdx = 0;
    if (st.weatherIcon >= WEATHER_ICON_COUNT) st.weatherIcon = 0;
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

    st.checksum = calcStructChecksum(st);
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

    const bool validV14 = (stored.magic == EEPROM_MAGIC &&
                           stored.checksum == calcStructChecksum(stored));
    if (validV14) {
        offlineState = stored;
        sanitizeOfflineState(offlineState);
    } else if (stored.magic == EEPROM_MAGIC_V13) {
        OfflineStateV13 oldV13;
        EEPROM.get(0, oldV13);
        memset(&offlineState, 0, sizeof(offlineState));
        if (oldV13.checksum == calcStructChecksum(oldV13)) {
            copyStrSafe(offlineState.city, sizeof(offlineState.city), oldV13.city);
            copyStrSafe(offlineState.weatherKey, sizeof(offlineState.weatherKey), oldV13.weatherKey);
            copyStrSafe(offlineState.trainerName, sizeof(offlineState.trainerName), oldV13.trainerName);
            offlineState.celsius = oldV13.celsius ? 1 : 0;
            offlineState.hour12 = oldV13.hour12 ? 1 : 0;
            offlineState.brightness = oldV13.brightness;
            offlineState.rotation = oldV13.rotation;
            offlineState.weatherValid = oldV13.weatherValid;
            offlineState.humidity = oldV13.humidity;
            offlineState.tempC = oldV13.tempC;
            copyStrSafe(offlineState.desc, sizeof(offlineState.desc), oldV13.desc);
            copyStrSafe(offlineState.cityResp, sizeof(offlineState.cityResp), oldV13.cityResp);
            copyStrSafe(offlineState.updatedHm, sizeof(offlineState.updatedHm), oldV13.updatedHm);
            offlineState.updatedEpoch = oldV13.updatedEpoch;
        } else {
            copyStrSafe(offlineState.city, sizeof(offlineState.city), DEFAULT_WEATHER_CITY);
            copyStrSafe(offlineState.weatherKey, sizeof(offlineState.weatherKey), DEFAULT_WEATHER_KEY);
            copyStrSafe(offlineState.trainerName, sizeof(offlineState.trainerName), "POKEGEAR");
            offlineState.celsius = 1;
            offlineState.hour12 = 1;
            offlineState.brightness = 95;
            offlineState.rotation = 0;
        }
        offlineState.pokemonIdx = 0;
        offlineState.paletteIdx = 0;
        offlineState.weatherIcon = WEATHER_ICON_SUN;
        sanitizeOfflineState(offlineState);
        EEPROM.put(0, offlineState);
        EEPROM.commit();
    } else if (stored.magic == EEPROM_MAGIC_V12) {
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
        offlineState.pokemonIdx = 0;
        offlineState.paletteIdx = 0;
        offlineState.weatherIcon = WEATHER_ICON_SUN;
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
        offlineState.pokemonIdx = 0;
        offlineState.paletteIdx = 0;
        offlineState.weatherIcon = WEATHER_ICON_SUN;
        sanitizeOfflineState(offlineState);
        EEPROM.put(0, offlineState);
        EEPROM.commit();
    }

    currentBrightness = offlineState.brightness;
    currentRotation = offlineState.rotation;
    offlineWeatherIcon = offlineState.weatherIcon;

    if (offlineState.weatherValid) {
        offlineWeatherValid = true;
        offlineWeatherFromCache = true;
        offlineTempC = offlineState.tempC;
        offlineHumidity = offlineState.humidity;
        copyStrSafe(offlineDesc, sizeof(offlineDesc), offlineState.desc);
        copyStrSafe(offlineCityResp, sizeof(offlineCityResp),
                    offlineState.cityResp[0] != '\0' ? offlineState.cityResp : offlineState.city);
        copyStrSafe(offlineWeatherUpdatedHm, sizeof(offlineWeatherUpdatedHm), offlineState.updatedHm);
        offlineWeatherEpoch = offlineState.updatedEpoch;
    } else {
        offlineWeatherValid = false;
        offlineWeatherFromCache = false;
        offlineDesc[0] = '\0';
        offlineCityResp[0] = '\0';
        offlineWeatherUpdatedHm[0] = '\0';
    }
}

// Hysteresis shared between autonomous OWM fetches and PC /offline_cfg syncs:
// Avoids EEPROM wear when temp delta < 0.5C, humidity delta < 3%, same city/desc/icon, and < 3600s elapsed.
static bool applyWeatherSnapshotToState(OfflineState& next, bool forceWrite = false) {
    if (!offlineWeatherValid || !isfinite(offlineTempC)) return false;

    char descBuf[sizeof(offlineState.desc)];
    char cityBuf[sizeof(offlineState.cityResp)];
    char hmBuf[sizeof(offlineState.updatedHm)];
    copyStrSafe(descBuf, sizeof(descBuf), offlineDesc);
    copyStrSafe(cityBuf, sizeof(cityBuf), offlineCityResp);
    copyStrSafe(hmBuf, sizeof(hmBuf), offlineWeatherUpdatedHm);

    bool meaningfulChange = forceWrite || (offlineState.weatherValid == 0);
    if (!meaningfulChange) {
        const int humDiff = abs((int)offlineState.humidity - offlineHumidity);
        if (fabsf(offlineState.tempC - offlineTempC) >= 0.5f) {
            meaningfulChange = true;
        } else if (humDiff >= 3) {
            meaningfulChange = true;
        } else if (strcmp(offlineState.desc, descBuf) != 0 ||
                   strcmp(offlineState.cityResp, cityBuf) != 0) {
            meaningfulChange = true;
        } else if (offlineState.weatherIcon != offlineWeatherIcon) {
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

    next.weatherValid = 1;
    next.tempC = offlineTempC;
    next.humidity = (int16_t)offlineHumidity;
    next.weatherIcon = offlineWeatherIcon;
    copyStrSafe(next.desc, sizeof(next.desc), descBuf);
    copyStrSafe(next.cityResp, sizeof(next.cityResp), cityBuf);
    copyStrSafe(next.updatedHm, sizeof(next.updatedHm), hmBuf);
    next.updatedEpoch = offlineWeatherEpoch;
    return true;
}

static bool persistWeatherSnapshot(bool forceWrite = false) {
    OfflineState next = offlineState;
    if (!applyWeatherSnapshotToState(next, forceWrite)) {
        return false;
    }
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
// Fixed-Buffer String, URL, and JSON Helpers
// ============================================================
static void urlEncodeTo(char* dst, size_t dstSize, const char* src) {
    if (!dst || dstSize == 0) return;
    if (!src) {
        dst[0] = '\0';
        return;
    }
    static const char hex[] = "0123456789ABCDEF";
    size_t out = 0;
    while (*src && out + 1 < dstSize) {
        unsigned char c = (unsigned char)*src++;
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
            c == '-' || c == '_' || c == '.' || c == '~') {
            dst[out++] = (char)c;
        } else {
            if (out + 3 >= dstSize) break;
            dst[out++] = '%';
            dst[out++] = hex[(c >> 4) & 0x0F];
            dst[out++] = hex[c & 0x0F];
        }
    }
    dst[out] = '\0';
}

static void sanitizeAsciiUpperTo(char* dst, size_t dstSize, const char* src, size_t maxChars) {
    if (!dst || dstSize == 0) return;
    if (!src) {
        dst[0] = '\0';
        return;
    }
    size_t limit = dstSize - 1;
    if (maxChars < limit) limit = maxChars;

    size_t out = 0;
    for (size_t i = 0; src[i] != '\0' && out < limit; i++) {
        uint8_t c = (uint8_t)src[i];
        if (c == 0xC3 && src[i + 1] != '\0') {
            uint8_t c2 = (uint8_t)src[++i];
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
            if (!(repl == ' ' && out == 0)) {
                dst[out++] = repl;
            }
        } else if (c >= 32 && c <= 126) {
            char ch = (char)c;
            if (ch >= 'a' && ch <= 'z') ch = (char)(ch - 32);
            if (!(ch == ' ' && out == 0)) {
                dst[out++] = ch;
            }
        }
    }
    while (out > 0 && dst[out - 1] == ' ') {
        out--;
    }
    dst[out] = '\0';
}

static void jsonEscapeTo(char* dst, size_t dstSize, const char* src) {
    if (!dst || dstSize == 0) return;
    if (!src) {
        dst[0] = '\0';
        return;
    }
    size_t out = 0;
    while (*src && out + 1 < dstSize) {
        uint8_t c = (uint8_t)*src++;
        if (c < 32) continue;
        if (c == '"' || c == '\\') {
            if (out + 2 >= dstSize) break;
            dst[out++] = '\\';
        }
        dst[out++] = (char)c;
    }
    dst[out] = '\0';
}

// Finds `"keyName"` followed by optional whitespace and `:` and returns pointer to value start.
static const char* findJsonKeyValue(const char* body, const char* keyName) {
    if (!body || !keyName || keyName[0] == '\0') return nullptr;
    size_t klen = strlen(keyName);
    const char* p = body;
    while ((p = strchr(p, '"')) != nullptr) {
        p++;
        if (strncmp(p, keyName, klen) == 0 && p[klen] == '"') {
            const char* q = p + klen + 1;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
            if (*q == ':') {
                q++;
                while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
                return q;
            }
        }
    }
    return nullptr;
}

static bool extractQuotedString(const char* valPtr, char* out, size_t outSize) {
    if (!out || outSize == 0) return false;
    out[0] = '\0';
    if (!valPtr || *valPtr != '"') return false;
    valPtr++;
    size_t n = 0;
    while (*valPtr && *valPtr != '"' && n + 1 < outSize) {
        if (*valPtr == '\\' && valPtr[1] != '\0') {
            valPtr++;
        }
        out[n++] = *valPtr++;
    }
    out[n] = '\0';
    return true;
}

static bool extractJsonFloat(const char* body, const char* keyName, float& outVal) {
    const char* p = findJsonKeyValue(body, keyName);
    if (!p) return false;
    if (*p == '"') p++;
    char* endPtr = nullptr;
    float v = strtof(p, &endPtr);
    if (endPtr == p) return false;
    outVal = v;
    return true;
}

static bool extractJsonUInt32(const char* body, const char* keyName, uint32_t& outVal) {
    const char* p = findJsonKeyValue(body, keyName);
    if (!p) return false;
    char* endPtr = nullptr;
    unsigned long v = strtoul(p, &endPtr, 10);
    if (endPtr == p) return false;
    outVal = (uint32_t)v;
    return true;
}

static bool extractJsonString(const char* body, const char* keyName, char* out, size_t outSize) {
    const char* p = findJsonKeyValue(body, keyName);
    if (!p) return false;
    return extractQuotedString(p, out, outSize);
}

static bool readLineFixed(WiFiClient& client, char* buf, size_t bufSize, uint32_t startMs, uint32_t timeoutMs) {
    if (!buf || bufSize == 0) return false;
    size_t len = 0;
    bool gotAny = false;
    while ((millis() - startMs) < timeoutMs) {
        if (client.available()) {
            int ch = client.read();
            if (ch < 0) continue;
            gotAny = true;
            if (ch == '\n') break;
            if (ch != '\r' && len + 1 < bufSize) {
                buf[len++] = (char)ch;
            }
        } else if (!client.connected()) {
            break;
        } else {
            delay(2);
            yield();
        }
    }
    buf[len] = '\0';
    return gotAny;
}

static bool isNightTimeNow(uint32_t fallbackEpoch, const char* iconCode) {
    time_t tnow = time(nullptr);
    if (tnow > 1700000000) {
        struct tm tinfo;
        localtime_r(&tnow, &tinfo);
        return (tinfo.tm_hour >= 19 || tinfo.tm_hour < 6);
    }
    if (fallbackEpoch > 1700000000UL) {
        time_t localTs = (time_t)fallbackEpoch + TZ_OFFSET_SEC;
        struct tm tinfo;
        gmtime_r(&localTs, &tinfo);
        return (tinfo.tm_hour >= 19 || tinfo.tm_hour < 6);
    }
    if (iconCode && iconCode[0] != '\0') {
        size_t len = strlen(iconCode);
        return (len > 0 && iconCode[len - 1] == 'n');
    }
    return false;
}

static uint8_t mapOwmIdToIcon(int weatherId, bool isNight) {
    if (weatherId >= 200 && weatherId <= 299) return WEATHER_ICON_STORM;
    if (weatherId >= 300 && weatherId <= 599) return WEATHER_ICON_RAIN;
    if (weatherId >= 600 && weatherId <= 799) return WEATHER_ICON_CLOUD;
    if (weatherId == 800) return isNight ? WEATHER_ICON_MOON : WEATHER_ICON_SUN;
    if (weatherId >= 801 && weatherId <= 804) return WEATHER_ICON_CLOUD;
    return isNight ? WEATHER_ICON_MOON : WEATHER_ICON_SUN;
}

static uint8_t inferWeatherIconFromDesc(const char* descUpper, bool isNight) {
    if (!descUpper || descUpper[0] == '\0') {
        return isNight ? WEATHER_ICON_MOON : WEATHER_ICON_SUN;
    }
    if (strstr(descUpper, "TORMENTA") || strstr(descUpper, "STORM") ||
        strstr(descUpper, "THUNDER") || strstr(descUpper, "RAYO")) {
        return WEATHER_ICON_STORM;
    }
    if (strstr(descUpper, "LLUV") || strstr(descUpper, "RAIN") ||
        strstr(descUpper, "CHUBASCO") || strstr(descUpper, "DRIZZLE") ||
        strstr(descUpper, "AGUACERO")) {
        return WEATHER_ICON_RAIN;
    }
    if (strstr(descUpper, "NUBL") || strstr(descUpper, "NUBE") ||
        strstr(descUpper, "CLOUD") || strstr(descUpper, "NIEBL") ||
        strstr(descUpper, "BRUM") || strstr(descUpper, "OVERCAST") ||
        strstr(descUpper, "CUBIERTO")) {
        return WEATHER_ICON_CLOUD;
    }
    if (strstr(descUpper, "DESPEJ") || strstr(descUpper, "CLARO") ||
        strstr(descUpper, "CLEAR") || strstr(descUpper, "SOL") ||
        strstr(descUpper, "SUN")) {
        return isNight ? WEATHER_ICON_MOON : WEATHER_ICON_SUN;
    }
    return isNight ? WEATHER_ICON_MOON : WEATHER_ICON_CLOUD;
}

static uint8_t parsePokeArg(const char* val) {
    if (!val || val[0] == '\0') return offlineState.pokemonIdx;
    if (val[0] >= '0' && val[0] <= '9') {
        long v = strtol(val, nullptr, 10);
        if (v >= 0 && v < POKE_SPRITE_COUNT) return (uint8_t)v;
        return 0;
    }
    return pokeNameToIndex(val);
}

static uint8_t parsePaletteArg(const char* val) {
    if (!val || val[0] == '\0') return offlineState.paletteIdx;
    if (val[0] >= '0' && val[0] <= '9') {
        long v = strtol(val, nullptr, 10);
        if (v >= 0 && v < GBC_PALETTE_COUNT) return (uint8_t)v;
        return 0;
    }
    return paletteNameToIndex(val);
}

static uint8_t parseWeatherIconArg(const char* val) {
    if (!val || val[0] == '\0') return offlineState.weatherIcon;
    if (val[0] >= '0' && val[0] <= '9') {
        long v = strtol(val, nullptr, 10);
        if (v >= 0 && v < WEATHER_ICON_COUNT) return (uint8_t)v;
        return 0;
    }
    for (uint8_t i = 0; i < WEATHER_ICON_COUNT; ++i) {
        if (_eqIgnoreCase(val, WEATHER_ICONS[i].name)) return i;
    }
    return 0;
}

static void noteWeatherFailure(const char* errCode) {
    copyStrSafe(lastWeatherErr, sizeof(lastWeatherErr), errCode);
    if (weatherFailCount < 4) {
        weatherFailCount++;
    }
}

// ============================================================
// Autonomous OpenWeatherMap Fetch (Zero Heap String Churn)
// ============================================================
static bool fetchOfflineWeather() {
    lastWeatherAttemptMs = millis();

    if (WiFi.status() != WL_CONNECTED) {
        lastWeatherHttpCode = 0;
        copyStrSafe(lastWeatherErr, sizeof(lastWeatherErr), "NO_WIFI");
        return false;
    }
    if (offlineState.city[0] == '\0') {
        lastWeatherHttpCode = 0;
        copyStrSafe(lastWeatherErr, sizeof(lastWeatherErr), "NO_CITY");
        return false;
    }
    if (offlineState.weatherKey[0] == '\0') {
        lastWeatherHttpCode = 0;
        copyStrSafe(lastWeatherErr, sizeof(lastWeatherErr), "NO_KEY");
        return false;
    }

    IPAddress owmIp;
    if (!WiFi.hostByName("api.openweathermap.org", owmIp)) {
        lastWeatherHttpCode = 0;
        noteWeatherFailure("ERR_DNS");
        return false;
    }

    WiFiClient client;
    client.setTimeout(WEATHER_HTTP_TIMEOUT_MS);
    if (!client.connect(owmIp, 80)) {
        lastWeatherHttpCode = 0;
        noteWeatherFailure("CONN_ERR");
        return false;
    }

    char cityEnc[124];
    char keyEnc[148];
    char reqBuf[420];
    urlEncodeTo(cityEnc, sizeof(cityEnc), offlineState.city);
    urlEncodeTo(keyEnc, sizeof(keyEnc), offlineState.weatherKey);

    int reqLen = snprintf_P(
        reqBuf, sizeof(reqBuf),
        PSTR("GET /data/2.5/weather?q=%s&appid=%s&units=metric&lang=es HTTP/1.0\r\n"
             "Host: api.openweathermap.org\r\n"
             "Accept-Encoding: identity\r\n"
             "User-Agent: PokeDisplay/1.4\r\n"
             "Connection: close\r\n\r\n"),
        cityEnc, keyEnc);
    if (reqLen <= 0 || (size_t)reqLen >= sizeof(reqBuf)) {
        client.stop();
        lastWeatherHttpCode = 0;
        noteWeatherFailure("REQ_ERR");
        return false;
    }
    client.write(reinterpret_cast<const uint8_t*>(reqBuf), (size_t)reqLen);

    uint32_t t0 = millis();
    while (client.connected() && !client.available() && (millis() - t0) < WEATHER_HTTP_TIMEOUT_MS) {
        delay(10);
        yield();
    }
    if (!client.available()) {
        client.stop();
        lastWeatherHttpCode = 0;
        noteWeatherFailure("TIMEOUT");
        return false;
    }

    char lineBuf[128];
    if (!readLineFixed(client, lineBuf, sizeof(lineBuf), t0, WEATHER_HTTP_TIMEOUT_MS)) {
        client.stop();
        lastWeatherHttpCode = 0;
        noteWeatherFailure("TIMEOUT");
        return false;
    }

    int httpCode = 0;
    if (strncmp(lineBuf, "HTTP/", 5) == 0) {
        const char* sp = strchr(lineBuf, ' ');
        if (sp != nullptr) {
            httpCode = atoi(sp + 1);
        }
    }
    lastWeatherHttpCode = httpCode;
    if (httpCode != 200) {
        client.stop();
        if (httpCode > 0) {
            char codeErr[20];
            snprintf(codeErr, sizeof(codeErr), "HTTP_%d", httpCode);
            noteWeatherFailure(codeErr);
        } else {
            noteWeatherFailure("TIMEOUT");
        }
        return false;
    }

    // Skip HTTP headers until empty line
    while (client.connected() || client.available()) {
        if (!readLineFixed(client, lineBuf, sizeof(lineBuf), t0, WEATHER_HTTP_TIMEOUT_MS)) {
            break;
        }
        if (lineBuf[0] == '\0') {
            break;
        }
        yield();
    }

    // Read JSON body into static fixed buffer
    size_t bodyLen = 0;
    uint32_t bodyT0 = millis();
    while ((client.connected() || client.available()) &&
           bodyLen < sizeof(weatherBody) - 1 &&
           (millis() - bodyT0) < WEATHER_HTTP_TIMEOUT_MS) {
        int avail = client.available();
        if (avail > 0) {
            size_t rem = sizeof(weatherBody) - 1 - bodyLen;
            int toRead = avail;
            if ((size_t)toRead > rem) toRead = (int)rem;
            int n = client.read(reinterpret_cast<uint8_t*>(weatherBody + bodyLen), toRead);
            if (n > 0) {
                bodyLen += (size_t)n;
            }
        } else {
            delay(5);
        }
        yield();
    }
    weatherBody[bodyLen] = '\0';
    client.stop();

    if (bodyLen == 0) {
        noteWeatherFailure("TIMEOUT");
        return false;
    }

    float tempVal = 0.0f;
    float humVal  = 0.0f;
    uint32_t dtVal = 0;
    char descRaw[64] = "";
    char cityRaw[48] = "";
    char iconCode[8] = "";

    if (!extractJsonFloat(weatherBody, "temp", tempVal) || !isfinite(tempVal)) {
        noteWeatherFailure("JSON_ERR");
        return false;
    }
    extractJsonFloat(weatherBody, "humidity", humVal);
    extractJsonString(weatherBody, "description", descRaw, sizeof(descRaw));
    extractJsonString(weatherBody, "name", cityRaw, sizeof(cityRaw));
    extractJsonUInt32(weatherBody, "dt", dtVal);

    bool haveWeatherId = false;
    int weatherId = 800;
    const char* wSection = findJsonKeyValue(weatherBody, "weather");
    if (wSection != nullptr) {
        const char* wEnd = strchr(wSection, ']');
        const char* idPtr = findJsonKeyValue(wSection, "id");
        if (idPtr != nullptr && (wEnd == nullptr || idPtr < wEnd)) {
            char* endp = nullptr;
            long parsedId = strtol(idPtr, &endp, 10);
            if (endp != idPtr) {
                weatherId = (int)parsedId;
                haveWeatherId = true;
            }
        }
        const char* iconPtr = findJsonKeyValue(wSection, "icon");
        if (iconPtr != nullptr && (wEnd == nullptr || iconPtr < wEnd) && *iconPtr == '"') {
            extractQuotedString(iconPtr, iconCode, sizeof(iconCode));
        }
    }

    int humInt = (int)roundf(humVal);
    if (humInt < 0) humInt = 0;
    if (humInt > 100) humInt = 100;

    offlineTempC = tempVal;
    offlineHumidity = humInt;
    sanitizeAsciiUpperTo(offlineDesc, sizeof(offlineDesc), descRaw, 22);
    sanitizeAsciiUpperTo(offlineCityResp, sizeof(offlineCityResp),
                         cityRaw[0] != '\0' ? cityRaw : offlineState.city, 14);
    const bool isNight = isNightTimeNow(dtVal, iconCode);
    offlineWeatherIcon = haveWeatherId
                             ? mapOwmIdToIcon(weatherId, isNight)
                             : inferWeatherIconFromDesc(offlineDesc, isNight);

    offlineWeatherValid = true;
    offlineWeatherFromCache = false;
    weatherSyncedThisBoot = true;
    offlineWeatherUpdatedMs = millis();
    weatherFailCount = 0;
    lastWeatherErr[0] = '\0';

    time_t tnow = time(nullptr);
    if (tnow > 1700000000) {
        struct tm tinfo;
        localtime_r(&tnow, &tinfo);
        snprintf(offlineWeatherUpdatedHm, sizeof(offlineWeatherUpdatedHm),
                 "%02d:%02d", tinfo.tm_hour, tinfo.tm_min);
        offlineWeatherEpoch = (uint32_t)tnow;
    } else if (dtVal > 1700000000UL) {
        time_t localTs = (time_t)dtVal + TZ_OFFSET_SEC;
        struct tm tinfo;
        gmtime_r(&localTs, &tinfo);
        snprintf(offlineWeatherUpdatedHm, sizeof(offlineWeatherUpdatedHm),
                 "%02d:%02d", tinfo.tm_hour, tinfo.tm_min);
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
    if (displaySleeping || pct == 0) {
        digitalWrite(TFT_BL, TFT_BACKLIGHT_ON == LOW ? HIGH : LOW);
        return;
    }
    analogWriteRange(100);
    int pwm = (TFT_BACKLIGHT_ON == LOW) ? (100 - pct) : pct;
    analogWrite(TFT_BL, pwm);
}

static void setDisplaySleeping(bool sleeping) {
    if (displaySleeping == sleeping) return;

    if (sleeping) {
        tft.writecommand(0x28); // ST7789 display off
        tft.writecommand(0x10); // ST7789 sleep in
        displaySleeping = true;
        applyBrightness(currentBrightness);
        return;
    }

    tft.writecommand(0x11); // ST7789 sleep out
    delay(120);
    tft.writecommand(0x29); // ST7789 display on
    displaySleeping = false;
    applyBrightness(currentBrightness);
    standbyActive = false;
    standbyNeedsFullRedraw = true;
    lastStandbyMin = -1;
}

static bool tftOutputCallback(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* bitmap) {
    if (x >= tft.width() || y >= tft.height()) return false;
    tft.pushImage(x, y, w, h, bitmap);
    if ((++mcuYieldCounter & 7) == 0) {
        yield();
    }
    return true;
}

static void drawSplash(const char* line1, const char* line2, const char* line3) {
    const uint16_t CREAM = tft.color565(248, 248, 240);
    const uint16_t INK   = tft.color565(24, 24, 32);
    const uint16_t GOLD  = tft.color565(248, 184, 0);
    const uint16_t BLUE  = tft.color565(40, 104, 200);

    tft.startWrite();
    tft.fillScreen(CREAM);
    tft.fillRect(0, 0, 240, 10, GOLD);
    tft.drawRect(8, 20, 224, 204, INK);
    tft.drawRect(10, 22, 220, 200, INK);
    tft.drawRect(14, 26, 212, 192, INK);

    tft.setTextSize(1);
    tft.setTextDatum(MC_DATUM);
    tft.setTextColor(INK, CREAM);
    tft.drawString("POKE-DISPLAY", 120, 62, 2);

    tft.setTextColor(BLUE, CREAM);
    tft.drawString(line1, 120, 106, 2);

    tft.setTextColor(INK, CREAM);
    tft.drawString(line2, 120, 140, 2);
    tft.drawString(line3, 120, 174, 2);
    tft.endWrite();
}

static void copyTruncatedToWidth(char* dst, size_t dstSize, const char* src, uint8_t font, int16_t maxW) {
    copyStrSafe(dst, dstSize, src);
    size_t len = strlen(dst);
    while (len > 0 && tft.textWidth(dst, font) > maxW) {
        dst[--len] = '\0';
    }
}

static void formatErrFriendly(char* dst, size_t dstSize, const char* err) {
    copyStrSafe(dst, dstSize, err);
    for (size_t i = 0; dst[i] != '\0'; i++) {
        if (dst[i] == '_') dst[i] = ' ';
    }
}

static void formatDateLine(const struct tm* t, bool hasTime, char* out, size_t outSize) {
    if (hasTime && t != nullptr) {
        snprintf(out, outSize, "%s %02d %s",
                 DAYS_ES[t->tm_wday % 7], t->tm_mday, MONTHS_ES[t->tm_mon % 12]);
    } else {
        copyStrSafe(out, outSize, "MODO OFFLINE");
    }
}

static void drawStandbyClockInterior(const struct tm* t, bool hasTime, const GbcPalette565& pal) {
    tft.fillRect(12, 92, 216, 64, pal.card);
    tft.setTextSize(1);

    if (hasTime && t != nullptr) {
        char timeBuf[16];
        if (offlineState.hour12) {
            int h12 = t->tm_hour % 12;
            if (h12 == 0) h12 = 12;
            snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", h12, t->tm_min);
            tft.setTextDatum(MC_DATUM);
            tft.setTextColor(pal.ink, pal.card);
            tft.drawString(timeBuf, 102, 126, 6);

            tft.fillRect(180, 126, 38, 20, pal.border);
            tft.fillRect(182, 128, 34, 16, pal.blue);
            tft.setTextColor(pal.card, pal.blue);
            tft.drawString(t->tm_hour >= 12 ? "PM" : "AM", 199, 136, 2);
        } else {
            snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d", t->tm_hour, t->tm_min);
            tft.setTextDatum(MC_DATUM);
            tft.setTextColor(pal.ink, pal.card);
            tft.drawString(timeBuf, 120, 126, 6);
        }
    } else {
        tft.setTextDatum(MC_DATUM);
        tft.setTextColor(pal.muted, pal.card);
        tft.drawString("--:--", 120, 126, 6);
    }
}

static void drawStandbyClock(const struct tm* t, bool hasTime, bool wifiOnline, bool partialTimeOnly = false) {
    const uint8_t pokeIdx = offlineState.pokemonIdx % POKE_SPRITE_COUNT;
    const uint8_t palIdx = offlineState.paletteIdx % GBC_PALETTE_COUNT;
    const GbcPalette565& pal = GBC_PALETTES[palIdx];
    const PokeSprite565& poke = POKE_SPRITES[pokeIdx];

    tft.startWrite();
    tft.setTextSize(1);

    if (!partialTimeOnly) {
        // Full authentic GBC Pokémon stage render
        tft.fillScreen(pal.bg);
        tft.fillRect(0, 0, 240, 6, poke.accent);

        // Subtle 2x2 checkerboard horizon dither at y=76
        for (int16_t dx = 0; dx < 240; dx += 4) {
            tft.fillRect(dx, 76, 2, 2, pal.dither);
            tft.fillRect(dx + 2, 78, 2, 2, pal.dither);
        }

        // Top-right zone (x=156..232, y=10..78): Oval GBC battle pad + 54x54 Pokémon sprite
        const int16_t padCx = 194;
        const int16_t padY = 62;
        tft.fillRect(padCx - 26, padY,      52, 2, pal.pad_top);
        tft.fillRect(padCx - 34, padY + 2,  68, 2, pal.pad_mid);
        tft.fillRect(padCx - 38, padY + 4,  76, 6, pal.pad_mid);
        tft.fillRect(padCx - 20, padY + 5,  40, 3, pal.pad_top);
        tft.fillRect(padCx - 34, padY + 10, 68, 3, pal.pad_top);
        tft.fillRect(padCx - 26, padY + 13, 52, 3, pal.pad_bot);
        drawPokeSprite565(tft, pokeIdx, 166, 16, 3);

        // Top-left zone (x=10..152, y=12..74): Trainer name + status badge
        const char* badgeTxt = !wifiOnline ? "SIN WIFI" : (!hasTime ? "SIN NTP" : "PC OFF");
        const uint16_t badgeCol = !wifiOnline ? pal.red : (!hasTime ? pal.muted : pal.blue);
        const int16_t badgeW = (int16_t)tft.textWidth(badgeTxt, 1) + 8;
        const int16_t badgeX = 152 - badgeW;
        tft.drawRect(badgeX, 15, badgeW, 14, badgeCol);
        tft.setTextDatum(MC_DATUM);
        tft.setTextColor(badgeCol, pal.bg);
        tft.drawString(badgeTxt, badgeX + badgeW / 2, 22, 1);

        char trainerBuf[16];
        copyTruncatedToWidth(trainerBuf, sizeof(trainerBuf), offlineState.trainerName, 2, badgeX - 14);
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor(pal.ink, pal.bg);
        tft.drawString(trainerBuf, 10, 22, 2);

        // Top-left line 2 (y=42): 14x14 mini weather icon + temp + humidity (or diagnostic)
        const uint8_t iconIdx = offlineWeatherValid
                                    ? (offlineWeatherIcon % WEATHER_ICON_COUNT)
                                    : (wifiOnline ? WEATHER_ICON_CLOUD : WEATHER_ICON_MOON);
        drawWeatherIcon565(tft, iconIdx, 12, 36, 2);

        if (offlineWeatherValid) {
            float dispTemp = offlineState.celsius ? offlineTempC : (offlineTempC * 9.0f / 5.0f + 32.0f);
            char tempStr[12];
            char humStr[8];
            snprintf(tempStr, sizeof(tempStr), "%d%s", (int)roundf(dispTemp), offlineState.celsius ? "C" : "F");
            snprintf(humStr, sizeof(humStr), "%d%%", offlineHumidity);

            tft.setTextDatum(ML_DATUM);
            tft.setTextColor(pal.blue, pal.bg);
            tft.drawString(tempStr, 30, 43, 2);

            int16_t humX = 30 + (int16_t)tft.textWidth(tempStr, 2) + 6;
            if (humX + (int16_t)tft.textWidth(humStr, 2) <= 152) {
                tft.setTextColor(pal.muted, pal.bg);
                tft.drawString(humStr, humX, 43, 2);
            }
        } else {
            char diagBuf[24];
            if (!wifiOnline) {
                copyStrSafe(diagBuf, sizeof(diagBuf), "SIN WIFI");
            } else if (lastWeatherErr[0] != '\0') {
                formatErrFriendly(diagBuf, sizeof(diagBuf), lastWeatherErr);
            } else {
                copyStrSafe(diagBuf, sizeof(diagBuf), "BUSCANDO...");
            }
            copyTruncatedToWidth(diagBuf, sizeof(diagBuf), diagBuf, 2, 122);
            tft.setTextDatum(ML_DATUM);
            tft.setTextColor(wifiOnline ? pal.muted : pal.red, pal.bg);
            tft.drawString(diagBuf, 30, 43, 2);
        }

        // Gen-2 tapered L-bracket underline framing top-left HUD
        tft.fillRect(10, 62, 132, 2, pal.border);
        tft.fillRect(142, 60, 4, 4, pal.border);
        tft.fillRect(12, 64, 128, 2, poke.accent);

        // Central GBC Clock Box (x=10..230, y=86..158)
        tft.fillRect(10, 86, 220, 72, pal.border);
        tft.fillRect(12, 88, 216, 68, pal.card);
        tft.fillRect(12, 88, 216, 4, wifiOnline ? pal.blue : pal.red);
        drawStandbyClockInterior(t, hasTime, pal);

        // Bottom Double-Border GBC Dialogue Box (x=8..232, y=166..232)
        tft.fillRect(8, 166, 224, 66, pal.border);
        tft.fillRect(11, 169, 218, 60, pal.bg);
        tft.fillRect(13, 171, 214, 56, pal.border);
        tft.fillRect(15, 173, 210, 52, pal.card);

        // Dialogue Line 1 (y=184): Date on left, City (max 11 chars) on right
        char dateBuf[24];
        formatDateLine(t, hasTime, dateBuf, sizeof(dateBuf));
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor(pal.ink, pal.card);
        tft.drawString(dateBuf, 22, 184, 2);

        char cityBuf[16];
        const char* rawCity = (offlineCityResp[0] != '\0') ? offlineCityResp : offlineState.city;
        sanitizeAsciiUpperTo(cityBuf, sizeof(cityBuf), rawCity, 11);
        copyTruncatedToWidth(cityBuf, sizeof(cityBuf), cityBuf, 2, 92);
        tft.setTextDatum(MR_DATUM);
        tft.setTextColor(pal.blue, pal.card);
        tft.drawString(cityBuf, 218, 184, 2);

        // Dialogue Line 2 (y=206): Weather description or clear diagnostic
        char line2[40];
        if (offlineWeatherValid) {
            const char* baseDesc = (offlineDesc[0] != '\0') ? offlineDesc : cityBuf;
            if (offlineWeatherFromCache && strlen(offlineWeatherUpdatedHm) == 5) {
                snprintf_P(line2, sizeof(line2), PSTR("%.5s %.27s"), offlineWeatherUpdatedHm, baseDesc);
            } else {
                copyStrSafe(line2, sizeof(line2), baseDesc);
            }
        } else if (!wifiOnline) {
            copyStrSafe(line2, sizeof(line2), "SIN WIFI");
        } else if (lastWeatherErr[0] != '\0') {
            formatErrFriendly(line2, sizeof(line2), lastWeatherErr);
        } else {
            copyStrSafe(line2, sizeof(line2), "SINCRONIZANDO...");
        }
        copyTruncatedToWidth(line2, sizeof(line2), line2, 2, 182);
        tft.setTextDatum(ML_DATUM);
        tft.setTextColor((!offlineWeatherValid && !wifiOnline) ? pal.red : pal.muted, pal.card);
        tft.drawString(line2, 22, 206, 2);

        // Red GBC dialogue cursor triangle at (214, 212)
        tft.fillTriangle(210, 209, 218, 209, 214, 215, pal.red);
    } else {
        // Partial minute update: redraw ONLY clock interior (+ date if day changed)
        drawStandbyClockInterior(t, hasTime, pal);

        if (hasTime && t != nullptr &&
            (t->tm_mday != lastStandbyMday || t->tm_mon != lastStandbyMon || t->tm_wday != lastStandbyWday)) {
            char dateBuf[24];
            formatDateLine(t, true, dateBuf, sizeof(dateBuf));
            tft.fillRect(18, 175, 108, 18, pal.card);
            tft.setTextDatum(ML_DATUM);
            tft.setTextColor(pal.ink, pal.card);
            tft.drawString(dateBuf, 22, 184, 2);
        }
    }

    if (hasTime && t != nullptr) {
        lastStandbyMday = t->tm_mday;
        lastStandbyMon = t->tm_mon;
        lastStandbyWday = t->tm_wday;
    }

    tft.endWrite();
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
    wifiDisconnectedSinceMs = 0;
    WiFi.setSleepMode(displaySleeping ? WIFI_MODEM_SLEEP : WIFI_NONE_SLEEP);
    weatherFailCount = 0;
    MDNS.begin(HOSTNAME);
    configTime(TZ_OFFSET_SEC, 0, "pool.ntp.org", "time.nist.gov");
    weatherSyncRequested = true;
    nextWeatherSyncMs = millis() + (showReadySplash ? 1500 : 500);

    if (showReadySplash) {
        char ipBuf[16];
        ipToStr(WiFi.localIP(), ipBuf, sizeof(ipBuf));
        drawSplash("READY v1.5!", ipBuf, "Waiting for PC");
    } else if (standbyActive || wasAp) {
        lastStandbyMin = -1;
        standbyNeedsFullRedraw = true;
    }
}

// ============================================================
// HTTP Handlers
// ============================================================
static void handleRoot() {
    bool wifiOnline = (WiFi.status() == WL_CONNECTED);
    char ipBuf[16];
    ipToStr(wifiOnline ? WiFi.localIP() : WiFi.softAPIP(), ipBuf, sizeof(ipBuf));

    String html;
    html.reserve(1400);
    html += F(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>PokeDisplay</title>"
        "<style>body{font-family:monospace;background:#f8f8f0;color:#181820;max-width:440px;margin:24px auto;padding:16px;}"
        ".box{border:3px solid #181820;padding:18px;background:#fff;box-shadow:4px 4px 0 #c8d0c0;}"
        "a{display:inline-block;padding:10px 14px;background:#181820;color:#fff;text-decoration:none;margin:6px 6px 0 0;font-weight:bold;}"
        "a.hub{background:#24a848;display:block;text-align:center;margin-bottom:10px;}"
        "</style></head><body><div class='box'>"
        "<h2>POKE-DISPLAY RAM v1.5</h2>"
        "<a class='hub' href='http://pokeclock.local/'>Abrir Panel Poké-Hub (PC)</a>"
    );
    html += F("<p>IP: ");
    html += ipBuf;
    html += F("<br>Wi-Fi: ");
    html += wifiOnline ? F("Conectado") : F("Sin Wi-Fi (AP + reconexión)");
    html += F("<br>Modo: ");
    html += standbyActive ? F("Offline Pokémon GBC") : F("Controlado por PC");
    html += F("<br>Pokémon / Paleta: ");
    html += POKE_SPRITES[offlineState.pokemonIdx % POKE_SPRITE_COUNT].name;
    html += F(" / ");
    html += GBC_PALETTE_NAMES[offlineState.paletteIdx % GBC_PALETTE_COUNT];
    html += F("<br>Clima offline: ");
    if (offlineWeatherValid) {
        char tBuf[8];
        snprintf(tBuf, sizeof(tBuf), "%d", (int)roundf(offlineTempC));
        html += offlineCityResp;
        html += ' ';
        html += tBuf;
        html += F("C (");
        html += offlineDesc;
        html += ')';
        if (offlineWeatherFromCache) {
            html += F(" [cache EEPROM");
            if (offlineWeatherUpdatedHm[0] != '\0') {
                html += ' ';
                html += offlineWeatherUpdatedHm;
            }
            html += ']';
        }
    } else {
        html += offlineState.city;
        html += F(" (");
        html += (lastWeatherErr[0] != '\0') ? lastWeatherErr : "pendiente";
        html += ')';
    }
    char statsBuf[64];
    snprintf_P(statsBuf, sizeof(statsBuf), PSTR("<br>Frames: %lu<br>Last blit: %lu ms<br>Free heap: %u B"),
               (unsigned long)frameCount, (unsigned long)lastDecodeMs, (unsigned)ESP.getFreeHeap());
    html += statsBuf;
    html += F("</p><a href='/ota'>Firmware OTA</a><a href='/status'>JSON Status</a><a href='/offline_weather'>Test Clima</a><a href='/standby'>Modo Offline</a></div></body></html>");
    server.send(200, "text/html", html);
}

static void handleStatus() {
    if (server.hasArg("keepalive")) {
        if (!displaySleeping && !standbyActive && !forceStandbyNow) {
            lastFrameMs = millis();
            pcContacted = true;
        }
        ipToStr(server.client().remoteIP(), lastHubIp, sizeof(lastHubIp));
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

    char ipBuf[16];
    char cityEsc[44];
    char descEsc[60];
    char hmEsc[16];
    char errEsc[44];
    char tempStr[12];

    ipToStr(wifiOnline ? WiFi.localIP() : WiFi.softAPIP(), ipBuf, sizeof(ipBuf));
    jsonEscapeTo(cityEsc, sizeof(cityEsc), offlineCityResp[0] != '\0' ? offlineCityResp : offlineState.city);
    jsonEscapeTo(descEsc, sizeof(descEsc), offlineDesc);
    jsonEscapeTo(hmEsc, sizeof(hmEsc), offlineWeatherUpdatedHm);
    jsonEscapeTo(errEsc, sizeof(errEsc), lastWeatherErr);
    dtostrf(isfinite(offlineTempC) ? offlineTempC : 0.0f, 1, 1, tempStr);

    const char* pokeName = POKE_SPRITES[offlineState.pokemonIdx % POKE_SPRITE_COUNT].name;
    const char* palName = GBC_PALETTE_NAMES[offlineState.paletteIdx % GBC_PALETTE_COUNT];
    const char* iconName = WEATHER_ICONS[offlineWeatherIcon % WEATHER_ICON_COUNT].name;

    char out[896];
    snprintf_P(
        out, sizeof(out),
        PSTR("{\"device\":\"poke-display\",\"version\":\"%s\",\"partial_frame\":true,"
             "\"ip\":\"%s\",\"hub_ip\":\"%s\",\"wifi_connected\":%s,\"ap_fallback\":%s,"
             "\"rssi\":%d,\"free_heap\":%u,\"brightness\":%u,\"rotation\":%u,\"display_on\":%s,"
             "\"frames\":%lu,\"last_ms\":%lu,\"standby\":%s,"
             "\"offline_city\":\"%s\",\"offline_weather_ok\":%s,"
             "\"offline_weather_cached\":%s,\"offline_weather_synced\":%s,"
             "\"offline_temp_c\":%s,\"offline_humidity\":%d,"
             "\"offline_desc\":\"%s\",\"offline_weather_hm\":\"%s\","
             "\"offline_weather_age_s\":%lu,"
             "\"weather_http\":%d,\"weather_err\":\"%s\","
             "\"pokemon\":\"%s\",\"palette\":\"%s\",\"weather_icon\":\"%s\","
             "\"uptime_s\":%lu}"),
        FW_VERSION,
        ipBuf,
        lastHubIp,
        wifiOnline ? "true" : "false",
        apFallbackActive ? "true" : "false",
        wifiOnline ? WiFi.RSSI() : 0,
        (unsigned)ESP.getFreeHeap(),
        (unsigned)currentBrightness,
        (unsigned)currentRotation,
        displaySleeping ? "false" : "true",
        (unsigned long)frameCount,
        (unsigned long)lastDecodeMs,
        standbyActive ? "true" : "false",
        cityEsc,
        offlineWeatherValid ? "true" : "false",
        offlineWeatherFromCache ? "true" : "false",
        weatherSyncedThisBoot ? "true" : "false",
        tempStr,
        offlineHumidity,
        descEsc,
        hmEsc,
        (unsigned long)weatherAgeS,
        lastWeatherHttpCode,
        errEsc,
        pokeName,
        palName,
        iconName,
        (unsigned long)(millis() / 1000));
    server.send(200, "application/json", out);
}

static void handleOfflineCfg() {
    OfflineState nextState = offlineState;
    nextState.brightness = currentBrightness;
    nextState.rotation = currentRotation;
    bool weatherChanged = false;
    bool weatherProvided = false;
    bool wiconProvided = false;

    if (server.hasArg("city") && server.arg("city").length() > 0) {
        char cBuf[sizeof(nextState.city)];
        copyStrSafe(cBuf, sizeof(cBuf), server.arg("city").c_str());
        // Trim leading/trailing spaces in-place
        char* start = cBuf;
        while (*start == ' ') start++;
        size_t len = strlen(start);
        while (len > 0 && start[len - 1] == ' ') start[--len] = '\0';
        if (start[0] != '\0') {
            if (strcmp(start, nextState.city) != 0) weatherChanged = true;
            copyStrSafe(nextState.city, sizeof(nextState.city), start);
        }
    }
    if (server.hasArg("key") && server.arg("key").length() > 0) {
        char kBuf[sizeof(nextState.weatherKey)];
        copyStrSafe(kBuf, sizeof(kBuf), server.arg("key").c_str());
        char* start = kBuf;
        while (*start == ' ') start++;
        size_t len = strlen(start);
        while (len > 0 && start[len - 1] == ' ') start[--len] = '\0';
        if (start[0] != '\0') {
            if (strcmp(start, nextState.weatherKey) != 0) weatherChanged = true;
            copyStrSafe(nextState.weatherKey, sizeof(nextState.weatherKey), start);
        }
    }
    if (server.hasArg("name") && server.arg("name").length() > 0) {
        char nBuf[sizeof(nextState.trainerName)];
        sanitizeAsciiUpperTo(nBuf, sizeof(nBuf), server.arg("name").c_str(), 14);
        if (nBuf[0] != '\0') {
            copyStrSafe(nextState.trainerName, sizeof(nextState.trainerName), nBuf);
        }
    }
    if (server.hasArg("c")) {
        nextState.celsius = (server.arg("c").toInt() != 0) ? 1 : 0;
    }
    if (server.hasArg("h12")) {
        nextState.hour12 = (server.arg("h12").toInt() != 0) ? 1 : 0;
    }
    if (server.hasArg("poke") && server.arg("poke").length() > 0) {
        nextState.pokemonIdx = parsePokeArg(server.arg("poke").c_str());
    }
    if (server.hasArg("pal") && server.arg("pal").length() > 0) {
        nextState.paletteIdx = parsePaletteArg(server.arg("pal").c_str());
    }
    if (server.hasArg("wicon") && server.arg("wicon").length() > 0) {
        wiconProvided = true;
        offlineWeatherIcon = parseWeatherIconArg(server.arg("wicon").c_str());
        nextState.weatherIcon = offlineWeatherIcon;
    }

    // Optional live weather snapshot pushed by PC (RAM updated immediately, EEPROM protected by hysteresis)
    if (server.hasArg("temp") && server.arg("temp").length() > 0) {
        float tVal = server.arg("temp").toFloat();
        if (isfinite(tVal) && tVal >= -90.0f && tVal <= 90.0f) {
            weatherProvided = true;
            int hVal = server.hasArg("hum") ? server.arg("hum").toInt() : offlineHumidity;
            if (hVal < 0) hVal = 0;
            if (hVal > 100) hVal = 100;

            char wCity[sizeof(offlineCityResp)];
            if (server.hasArg("wcity") && server.arg("wcity").length() > 0) {
                sanitizeAsciiUpperTo(wCity, sizeof(wCity), server.arg("wcity").c_str(), 14);
            } else {
                sanitizeAsciiUpperTo(wCity, sizeof(wCity), nextState.city, 14);
            }

            char wDesc[sizeof(offlineDesc)];
            if (server.hasArg("desc")) {
                sanitizeAsciiUpperTo(wDesc, sizeof(wDesc), server.arg("desc").c_str(), 22);
            } else {
                copyStrSafe(wDesc, sizeof(wDesc), offlineDesc);
            }

            if (server.hasArg("hm")) {
                char hmBuf[12];
                copyStrSafe(hmBuf, sizeof(hmBuf), server.arg("hm").c_str());
                char* hStart = hmBuf;
                while (*hStart == ' ') hStart++;
                size_t hLen = strlen(hStart);
                while (hLen > 0 && hStart[hLen - 1] == ' ') hStart[--hLen] = '\0';
                if (hLen > 5) hStart[5] = '\0';
                if (hStart[0] != '\0') {
                    copyStrSafe(offlineWeatherUpdatedHm, sizeof(offlineWeatherUpdatedHm), hStart);
                }
            }

            offlineTempC = tVal;
            offlineHumidity = hVal;
            copyStrSafe(offlineCityResp, sizeof(offlineCityResp), wCity);
            copyStrSafe(offlineDesc, sizeof(offlineDesc), wDesc);
            if (!wiconProvided) {
                offlineWeatherIcon = inferWeatherIconFromDesc(offlineDesc, isNightTimeNow(0, nullptr));
            }
            offlineWeatherValid = true;
            offlineWeatherFromCache = false;
            weatherSyncedThisBoot = true;
            offlineWeatherUpdatedMs = millis();
            weatherFailCount = 0;
            lastWeatherErr[0] = '\0';

            time_t tnow = time(nullptr);
            if (tnow > 1700000000) {
                offlineWeatherEpoch = (uint32_t)tnow;
            }

            applyWeatherSnapshotToState(nextState, false);
        }
    }

    bool saved = commitOfflineStateIfChanged(nextState);
    if (weatherChanged && !weatherProvided) {
        weatherFailCount = 0;
        weatherSyncedThisBoot = false;
        weatherSyncRequested = true;
        nextWeatherSyncMs = millis();
    }
    if (standbyActive) {
        lastStandbyMin = -1;
        standbyNeedsFullRedraw = true;
    }

    char cityEsc[84];
    jsonEscapeTo(cityEsc, sizeof(cityEsc), offlineState.city);
    const char* pokeName = POKE_SPRITES[offlineState.pokemonIdx % POKE_SPRITE_COUNT].name;
    const char* palName = GBC_PALETTE_NAMES[offlineState.paletteIdx % GBC_PALETTE_COUNT];

    char resp[320];
    snprintf_P(
        resp, sizeof(resp),
        PSTR("{\"ok\":true,\"saved\":%s,\"weather_synced\":%s,\"city\":\"%s\","
             "\"pokemon\":\"%s\",\"palette\":\"%s\",\"weather_icon\":\"%s\"}"),
        saved ? "true" : "false",
        weatherProvided ? "true" : "false",
        cityEsc,
        pokeName,
        palName,
        WEATHER_ICONS[offlineWeatherIcon % WEATHER_ICON_COUNT].name);
    server.send(200, "application/json", resp);
}

static void handleOfflineWeatherNow() {
    bool ok = fetchOfflineWeather();
    if (ok && standbyActive) {
        lastStandbyMin = -1;
        standbyNeedsFullRedraw = true;
    }

    char cityEsc[44];
    char descEsc[60];
    char hmEsc[16];
    char errEsc[44];
    char tempStr[12];
    jsonEscapeTo(cityEsc, sizeof(cityEsc), offlineCityResp[0] != '\0' ? offlineCityResp : offlineState.city);
    jsonEscapeTo(descEsc, sizeof(descEsc), offlineDesc);
    jsonEscapeTo(hmEsc, sizeof(hmEsc), offlineWeatherUpdatedHm);
    jsonEscapeTo(errEsc, sizeof(errEsc), lastWeatherErr);
    dtostrf(isfinite(offlineTempC) ? offlineTempC : 0.0f, 1, 1, tempStr);

    const char* pokeName = POKE_SPRITES[offlineState.pokemonIdx % POKE_SPRITE_COUNT].name;
    const char* palName = GBC_PALETTE_NAMES[offlineState.paletteIdx % GBC_PALETTE_COUNT];
    const char* iconName = WEATHER_ICONS[offlineWeatherIcon % WEATHER_ICON_COUNT].name;

    char resp[384];
    snprintf_P(
        resp, sizeof(resp),
        PSTR("{\"ok\":%s,\"city\":\"%s\",\"temp_c\":%s,\"humidity\":%d,"
             "\"desc\":\"%s\",\"updated_hm\":\"%s\",\"cached\":%s,"
             "\"weather_http\":%d,\"weather_err\":\"%s\","
             "\"pokemon\":\"%s\",\"palette\":\"%s\",\"weather_icon\":\"%s\"}"),
        ok ? "true" : "false",
        cityEsc,
        tempStr,
        offlineHumidity,
        descEsc,
        hmEsc,
        offlineWeatherFromCache ? "true" : "false",
        lastWeatherHttpCode,
        errEsc,
        pokeName,
        palName,
        iconName);
    server.send(ok ? 200 : 502, "application/json", resp);
}

static void handleStandby() {
    standbyActive = false;
    lastStandbyMin = -1;
    standbyNeedsFullRedraw = true;
    forceStandbyNow = true;
    server.send(200, "application/json", "{\"ok\":true,\"standby\":true}");
}

static void handleFrameUpload() {
    HTTPUpload& upload = server.upload();
    if (displaySleeping) {
        frameLen = 0;
        frameOverflow = false;
        return;
    }
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
    if (displaySleeping) {
        server.send(423, "application/json", "{\"ok\":false,\"error\":\"display is off\"}");
        return;
    }
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

    mcuYieldCounter = 0;
    uint32_t t0 = millis();
    tft.startWrite();
    JRESULT res = TJpgDec.drawJpg(drawX, drawY, src, len);
    tft.endWrite();
    lastDecodeMs = millis() - t0;

    if (res != JDR_OK) {
        char errBuf[48];
        snprintf_P(errBuf, sizeof(errBuf), PSTR("{\"ok\":false,\"jdr\":%d}"), (int)res);
        server.send(422, "application/json", errBuf);
        return;
    }

    frameCount++;
    lastFrameMs = millis();
    pcContacted = true;
    forceStandbyNow = false;
    standbyActive = false;
    standbyNeedsFullRedraw = true;
    lastStandbyMin = -1;
    ipToStr(server.client().remoteIP(), lastHubIp, sizeof(lastHubIp));

    char resp[64];
    snprintf_P(resp, sizeof(resp), PSTR("{\"ok\":true,\"bytes\":%u,\"ms\":%lu}"),
               (unsigned)len, (unsigned long)lastDecodeMs);
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

    char resp[48];
    snprintf_P(resp, sizeof(resp), PSTR("{\"ok\":true,\"brightness\":%u}"), (unsigned)currentBrightness);
    server.send(200, "application/json", resp);
}

static void handleDisplayPower() {
    if (!server.hasArg("on")) {
        server.send(400, "application/json", "{\"ok\":false,\"error\":\"missing on\"}");
        return;
    }
    String value = server.arg("on");
    if (value != "0" && value != "1") {
        server.send(400, "application/json", "{\"ok\":false,\"error\":\"on must be 0 or 1\"}");
        return;
    }

    setDisplaySleeping(value == "0");
    char resp[48];
    snprintf_P(resp, sizeof(resp), PSTR("{\"ok\":true,\"display_on\":%s}"),
               displaySleeping ? "false" : "true");
    server.send(200, "application/json", resp);
    WiFi.setSleepMode(displaySleeping ? WIFI_MODEM_SLEEP : WIFI_NONE_SLEEP);
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
    standbyNeedsFullRedraw = true;

    char resp[48];
    snprintf_P(resp, sizeof(resp), PSTR("{\"ok\":true,\"rotation\":%u}"), (unsigned)currentRotation);
    server.send(200, "application/json", resp);
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
                 offlineCityResp, (int)roundf(dispTemp), offlineState.celsius ? "C" : "F");
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
        wifiDisconnectedSinceMs = bootMs;
        lastWifiBeginMs = bootMs;
        WiFi.mode(WIFI_AP_STA);
        WiFi.softAP("PokeDisplay-OTA");
        WiFi.begin(WIFI_SSID, WIFI_PASS);
        drawSplash("AP RECOVERY", "PokeDisplay-OTA", "192.168.4.1/ota");
    }

    server.on("/", HTTP_GET, handleRoot);
    server.on("/status", HTTP_GET, handleStatus);
    server.on("/ping", HTTP_GET, handleStatus);
    server.on("/offline_cfg", HTTP_GET, handleOfflineCfg);
    server.on("/offline_cfg", HTTP_POST, handleOfflineCfg);
    server.on("/offline_weather", HTTP_GET, handleOfflineWeatherNow);
    server.on("/standby", HTTP_GET, handleStandby);
    server.on("/standby", HTTP_POST, handleStandby);
    server.on("/frame", HTTP_POST, handleFrameDone, handleFrameUpload);
    server.on("/brightness", HTTP_GET, handleBrightness);
    server.on("/display", HTTP_GET, handleDisplayPower);
    server.on("/rotation", HTTP_GET, handleRotation);
    server.on("/restart", HTTP_GET, handleRestart);
    server.on("/ota", HTTP_GET, handleOtaPage);
    server.on("/update_ota", HTTP_POST, handleOtaDone, handleOtaUpload);
    server.begin();
}

static uint32_t lastStandbyCheck = 0;

void loop() {
    server.handleClient();
    uint32_t now = millis();

    flushPendingDisplayPrefs(now);

    // 1. Smart Wi-Fi monitoring: let auto-reconnect work first, only call WiFi.begin() after >= 45s offline
    bool wifiOnline = (WiFi.status() == WL_CONNECTED);
    if (wifiOnline) {
        if (!wifiWasConnected || apFallbackActive) {
            onWifiConnected(false);
        }
        MDNS.update();
    } else {
        if (wifiWasConnected) {
            wifiWasConnected = false;
            wifiDisconnectedSinceMs = now;
            lastWifiBeginMs = now;
            if (standbyActive) {
                lastStandbyMin = -1;
                standbyNeedsFullRedraw = true;
            }
        }
        if (wifiDisconnectedSinceMs == 0) {
            wifiDisconnectedSinceMs = now;
            lastWifiBeginMs = now;
        }
        if ((now - wifiDisconnectedSinceMs) >= WIFI_RECONNECT_INTERVAL_MS &&
            (now - lastWifiBeginMs) >= WIFI_RECONNECT_INTERVAL_MS) {
            lastWifiBeginMs = now;
            WiFi.begin(WIFI_SSID, WIFI_PASS);
        }
    }

    if (displaySleeping) {
        delay(50);
        return;
    }

    // 2. Autonomous OpenWeatherMap sync with staggered backoff (30s -> 60s -> 180s -> 900s).
    //    Skipped while PC is actively streaming frames and we already have valid weather.
    if (wifiOnline && !(pcContacted && !standbyActive && offlineWeatherValid)) {
        const uint8_t backoffIdx = (weatherFailCount == 0)
                                       ? 0
                                       : ((weatherFailCount - 1 < 4) ? (uint8_t)(weatherFailCount - 1) : 3);
        const uint32_t retryInterval = WEATHER_BACKOFF_MS[backoffIdx];
        const bool retryReady = (lastWeatherAttemptMs == 0) || ((now - lastWeatherAttemptMs) >= retryInterval);

        const bool bootOrRequested = weatherSyncRequested && ((int32_t)(now - nextWeatherSyncMs) >= 0);
        const bool initialRetry = !weatherSyncedThisBoot &&
                                  ((int32_t)(now - nextWeatherSyncMs) >= 0) &&
                                  retryReady;
        const bool periodicRefresh = weatherSyncedThisBoot &&
                                     ((now - offlineWeatherUpdatedMs) >= OFFLINE_WEATHER_INTERVAL_MS) &&
                                     retryReady;

        if (bootOrRequested || initialRetry || periodicRefresh) {
            weatherSyncRequested = false;
            bool ok = fetchOfflineWeather();
            if (standbyActive && (ok || !offlineWeatherValid)) {
                lastStandbyMin = -1;
                standbyNeedsFullRedraw = true;
            }
        }
    }

    // 3. Autonomous standby / offline Pokémon GBC clock + cached/live weather display
    bool shouldStandby = standbyActive || forceStandbyNow;
    if (!shouldStandby) {
        if (pcContacted) {
            shouldStandby = (now - lastFrameMs) > STANDBY_TIMEOUT_MS;
        } else if (apFallbackActive) {
            shouldStandby = (now - apStartedMs) > AP_SPLASH_HOLD_MS;
        } else {
            shouldStandby = (now - bootMs) > BOOT_STANDBY_TIMEOUT_MS;
        }
    }

    if (shouldStandby && ((now - lastStandbyCheck) > 2000 || !standbyActive || lastStandbyMin == -1)) {
        lastStandbyCheck = now;
        time_t tnow = time(nullptr);
        bool hasTime = (tnow > 1700000000);

        if (hasTime) {
            struct tm tinfo;
            localtime_r(&tnow, &tinfo);
            // Backfill updatedHm if weather synced right before NTP finished
            if (offlineWeatherValid && weatherSyncedThisBoot && offlineWeatherUpdatedHm[0] == '\0') {
                snprintf(offlineWeatherUpdatedHm, sizeof(offlineWeatherUpdatedHm),
                         "%02d:%02d", tinfo.tm_hour, tinfo.tm_min);
                offlineWeatherEpoch = (uint32_t)tnow;
                persistWeatherSnapshot();
            }

            const bool needFull = !standbyActive || standbyNeedsFullRedraw ||
                                  lastStandbyMin < 0 || !lastStandbyHasTime ||
                                  lastStandbyWifi != wifiOnline;
            if (needFull) {
                standbyActive = true;
                standbyNeedsFullRedraw = false;
                lastStandbyMin = tinfo.tm_min;
                lastStandbyHasTime = true;
                lastStandbyWifi = wifiOnline;
                drawStandbyClock(&tinfo, true, wifiOnline, false);
            } else if (tinfo.tm_min != lastStandbyMin) {
                lastStandbyMin = tinfo.tm_min;
                drawStandbyClock(&tinfo, true, wifiOnline, true);
            }
        } else {
            const bool needFull = !standbyActive || standbyNeedsFullRedraw ||
                                  lastStandbyMin == -1 || lastStandbyHasTime ||
                                  lastStandbyWifi != wifiOnline;
            if (needFull) {
                standbyActive = true;
                standbyNeedsFullRedraw = false;
                lastStandbyMin = -2;
                lastStandbyHasTime = false;
                lastStandbyWifi = wifiOnline;
                drawStandbyClock(nullptr, false, wifiOnline, false);
            }
        }
    }

    yield();
}
