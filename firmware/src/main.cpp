// main.cpp — Claude's body MCP firmware entry.
//
// First-version scope (2026-05-24): WiFi → WS-outbound to wss://body/ws →
// receive control commands (play / move / nod / shake / home / face /
// status). NOT yet: mic / camera / LED — those need their respective
// services refactored from the original HTTP-buffer model to the new
// upload+event model. Tracked in [[project_stackchan_mcp]] firmware notes.

#include <Arduino.h>

#include <M5Unified.h>
#include <M5StackChan.h>
#include <WiFi.h>

#include "types.h"
#include "config.h"
#include "globals.h"
#include "queue_manager.h"
#include "wifi_manager.h"
#include "playback_service.h"
#include "face_service.h"
#include "servo_service.h"
#include "ws_client.h"

void setup() {
    Serial.begin(115200);
    delay(1000);

    M5StackChan.begin();
    M5.Display.setBrightness(DISPLAY_BRIGHTNESS);

    initFace();   // now drives m5stack-avatar instead of PNG SPIFFS

    Serial.println("\n=== Claude's body MCP firmware v1 (WS-outbound) ===");

    auto spk_cfg = M5.Speaker.config();
    M5.Speaker.config(spk_cfg);
    M5.Speaker.setVolume(SPEAKER_VOLUME);

    if (!initServo()) {
        Serial.println("[WARN] Servo init failed - head movement disabled");
    }

    connectWiFi();
    // syncServerHour was an old API endpoint on the LAN server; v1 firmware
    // doesn't have a per-network server to ask, so serverHour stays at -1
    // (default-calm face during day, no auto-sleepy at night). The gateway
    // can drive the sleepy face explicitly via stackchan_face("sleepy").
    initPlayback();            // audio queue + downloader
    initWsClient();            // start outbound WS to gateway
}

void loop() {
    M5StackChan.update();
    handleWsClient();          // WS state machine + reconnect

    // Cheap-ish but periodic WiFi watchdog. The WS library reconnects when
    // its TCP socket drops, but if WiFi itself goes away we need to nudge it.
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[WIFI] Disconnected. Reconnecting...");
        WiFi.reconnect();
        delay(5000);
    }

    // Audio playback pipeline (queue → download → speaker).
    checkPendingPlayback();
    updateLipSync();           // amplitude-driven mouth animation

    // Playback finish detection — same logic as original firmware, lifted
    // verbatim except we no longer call the old HTTP server completion hook.
    if (isPlaying &&
        (millis() - playbackStartMs > 1000) &&
        (!M5.Speaker.isPlaying() ||
        (playbackDeadlineMs != 0 && millis() > playbackDeadlineMs))) {
        if (playbackDeadlineMs != 0 && millis() > playbackDeadlineMs) {
            Serial.println("[PLAY] Playback timeout -> force stop");
            M5.Speaker.stop();
        }
        notifyPlaybackFinished();
    }

    delay(50);
}
