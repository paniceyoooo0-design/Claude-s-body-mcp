#ifndef LED_SERVICE_H
#define LED_SERVICE_H

#include <Arduino.h>

/**
 * Thin wrapper over StackChan-BSP's M5StackChan RGB API for the 12 front
 * LEDs (0-5 left, 6-11 right). The BSP exposes setRgbColor / refreshRgb /
 * showRgbColor; we add color-name parsing and bulk-set semantics so the
 * MCP tool layer can pass "#rrggbb" or names like "red", "off".
 */

// Init is a no-op — M5StackChan.begin() in main.cpp already brings up
// the IO expander that drives the LEDs. Declared for symmetry.
void initLeds();

// Set a single LED. Color is "#rrggbb" or one of: red green blue white
// yellow cyan magenta off black. Pushes to hardware immediately.
// Returns false if the color string is invalid or index is out of range.
bool setLedOne(uint8_t index, const String& color);

// Set ALL 12 LEDs to the same color in one I2C burst.
bool setLedsAll(const String& color);

// Set every LED individually. `colors` must point to 12 strings.
// Single refresh at the end.
bool setLedsBulk(const String colors[12]);

// Turn off all LEDs.
void clearLeds();

#endif
