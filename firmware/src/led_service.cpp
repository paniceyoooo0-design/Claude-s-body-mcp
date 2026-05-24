// led_service.cpp — color parsing + StackChan-BSP RGB API wrapping.

#include "led_service.h"
#include <M5Unified.h>
#include <M5StackChan.h>

// ── Color parsing ────────────────────────────────────────────────────────
// Gateway sends "#rrggbb" or short names. We accept the same names listed
// in mcp-server/server.py's _validate_color() so the layers agree.

struct NamedColor { const char* name; uint8_t r, g, b; };
static const NamedColor NAMED_COLORS[] = {
    {"red",     255, 0,   0},
    {"green",   0,   255, 0},
    {"blue",    0,   0,   255},
    {"white",   255, 255, 255},
    {"yellow",  255, 255, 0},
    {"cyan",    0,   255, 255},
    {"magenta", 255, 0,   255},
    {"black",   0,   0,   0},
    {"off",     0,   0,   0},
};
static const size_t NUM_NAMED = sizeof(NAMED_COLORS) / sizeof(NAMED_COLORS[0]);

static bool parseColor(const String& spec, uint8_t* r, uint8_t* g, uint8_t* b) {
    if (spec.length() == 0) return false;

    if (spec[0] == '#' && spec.length() == 7) {
        // #rrggbb
        char buf[3] = {0, 0, 0};
        char* end;
        buf[0] = spec[1]; buf[1] = spec[2]; *r = (uint8_t)strtol(buf, &end, 16); if (end == buf) return false;
        buf[0] = spec[3]; buf[1] = spec[4]; *g = (uint8_t)strtol(buf, &end, 16); if (end == buf) return false;
        buf[0] = spec[5]; buf[1] = spec[6]; *b = (uint8_t)strtol(buf, &end, 16); if (end == buf) return false;
        return true;
    }

    // Named color — case-insensitive match.
    String lower = spec;
    lower.toLowerCase();
    for (size_t i = 0; i < NUM_NAMED; i++) {
        if (lower == NAMED_COLORS[i].name) {
            *r = NAMED_COLORS[i].r;
            *g = NAMED_COLORS[i].g;
            *b = NAMED_COLORS[i].b;
            return true;
        }
    }
    return false;
}

// ── Public API ───────────────────────────────────────────────────────────

void initLeds() {
    // No-op. M5StackChan.begin() already initialized the IO expander that
    // drives the LEDs. Start with everything off so a flash always shows
    // a known state regardless of what the previous firmware left.
    clearLeds();
}

bool setLedOne(uint8_t index, const String& color) {
    if (index > 11) return false;
    uint8_t r, g, b;
    if (!parseColor(color, &r, &g, &b)) return false;
    M5StackChan.setRgbColor(index, r, g, b);
    M5StackChan.refreshRgb();
    return true;
}

bool setLedsAll(const String& color) {
    uint8_t r, g, b;
    if (!parseColor(color, &r, &g, &b)) return false;
    // showRgbColor sets all 12 and refreshes in one shot — single I2C burst,
    // better than 12 setRgbColor + refresh.
    M5StackChan.showRgbColor(r, g, b);
    return true;
}

bool setLedsBulk(const String colors[12]) {
    uint8_t r, g, b;
    // Parse-and-validate all 12 first so we don't half-apply on a bad color.
    uint8_t rs[12], gs[12], bs[12];
    for (int i = 0; i < 12; i++) {
        if (!parseColor(colors[i], &r, &g, &b)) return false;
        rs[i] = r; gs[i] = g; bs[i] = b;
    }
    for (int i = 0; i < 12; i++) {
        M5StackChan.setRgbColor(i, rs[i], gs[i], bs[i]);
    }
    M5StackChan.refreshRgb();
    return true;
}

void clearLeds() {
    M5StackChan.showRgbColor(0, 0, 0);
}
