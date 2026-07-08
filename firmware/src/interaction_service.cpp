// interaction_service.cpp — head touch / body motion / screen gestures.
//
// Interaction design ported from mo-hantang/Stackchan-HtSz
// (main/boards/m5stack-core-s3/m5stack_core_s3.cc), reimplemented on our
// Arduino stack. Their firmware hand-rolls the SI12T/FT6336/BMI270 drivers;
// on our side StackChan-BSP + M5Unified already provide all three, so this
// file is pure gesture classification + reactions.

#include "interaction_service.h"
#include "face_service.h"
#include "led_service.h"
#include "servo_service.h"
#include "mic_service.h"
#include "tracker_service.h"
#include "ws_client.h"
#include "globals.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <M5Unified.h>
#include <M5StackChan.h>

// ── Tunables ─────────────────────────────────────────────────────────────
// Head-touch cooldown matches HtSz's 5s — long enough that one caress
// doesn't machine-gun events, short enough to still feel responsive.
static const uint32_t HEAD_COOLDOWN_MS   = 5000;
static const uint32_t SCREEN_COOLDOWN_MS = 3000;

// Motion detection constants are HtSz's field-tuned values (their comments
// disagree with their constants; the constants are what actually shipped).
// Sampled at 10 Hz. "Moving" = per-axis delta OR |mag - 1g| above threshold;
// the delta term catches rotation/shaking that doesn't change magnitude.
static const uint32_t MOTION_SAMPLE_MS        = 100;
static const float    MOTION_THRESHOLD_G      = 0.3f;
static const int      SHAKE_PEAKS_TO_TRIGGER  = 2;      // peaks within 1s
static const uint32_t SHAKE_WINDOW_MS         = 1000;
static const int      LIFT_SAMPLES_TO_TRIGGER = 5;      // 500ms sustained
static const int      STILL_SAMPLES_TO_REARM  = 50;     // 5s of stillness
// HtSz uses a 5-minute global cooldown because triggers inject LLM chat
// messages. Ours are a local reaction + a logged event, so 30s is plenty.
static const uint32_t MOTION_COOLDOWN_MS      = 30000;

// Reaction hold times before restoring whatever face/LEDs had before.
static const uint32_t FACE_HOLD_MS = 3000;
static const uint32_t LED_HOLD_MS  = 1500;

// ── Reaction state ───────────────────────────────────────────────────────
static bool      imuOk = false;
static uint32_t  headCooldownUntil   = 0;
static uint32_t  screenCooldownUntil = 0;
static uint32_t  motionCooldownUntil = 0;

static WhaleFace reactionPrevFace = WHALE_CALM;
static WhaleFace reactionFace     = WHALE_CALM;
static uint32_t  faceRestoreAtMs  = 0;   // 0 = no restore pending
static uint32_t  ledClearAtMs     = 0;

// Last head zone seen while pressed — intensities read 0 by the time the
// release edge (wasClicked) fires, so we remember the strongest zone live.
static int lastHeadZone = -1;

// Motion detector state (HtSz MotionLoop, minus the FreeRTOS task — we
// sample from loop() on a millis() schedule instead).
static uint32_t nextImuSampleMs = 0;
static float    lastAx = 0, lastAy = 0, lastAz = 0;
static bool     lastAccelValid = false;
static bool     motionArmed = true;
static int      liftCount = 0;
static int      stillCount = 0;
static uint32_t shakePeakTimes[8] = {0};
static int      shakeIdx = 0;

// ── Helpers ──────────────────────────────────────────────────────────────
static void emitInteraction(const char* kind, const char* detail = nullptr) {
    JsonDocument doc;
    doc["event"]  = "interaction";
    doc["kind"]   = kind;
    if (detail) doc["detail"] = detail;
    doc["uptime_ms"] = millis();
    String out;
    serializeJson(doc, out);
    wsEmitEvent(out);
    Serial.printf("[INTERACT] %s%s%s\n", kind, detail ? " " : "",
                  detail ? detail : "");
}

static void reactFace(WhaleFace f) {
    // Only snapshot the pre-reaction face when no restore is pending,
    // otherwise chained reactions would "restore" to a reaction face.
    if (faceRestoreAtMs == 0) reactionPrevFace = (WhaleFace)getCurrentWhaleFace();
    reactionFace = f;
    setWhaleFace(f);
    faceRestoreAtMs = millis() + FACE_HOLD_MS;
}

static void reactLeds(const String& color) {
    setLedsAll(color);
    ledClearAtMs = millis() + LED_HOLD_MS;
}

// A servo reaction is only safe when nothing else owns the head: playback
// wiggles via lip-sync timing, an armed mic would record servo whine.
static bool servoFree() {
    return isServoReady() && !isPlaying && !isMicArmed();
}

static void expireReactions() {
    uint32_t now = millis();
    if (faceRestoreAtMs != 0 && now >= faceRestoreAtMs) {
        // If the face changed under us (gateway sent stackchan_face during
        // the hold), the reaction is no longer on screen — don't stomp it.
        if ((WhaleFace)getCurrentWhaleFace() == reactionFace) {
            setWhaleFace(reactionPrevFace);
        }
        faceRestoreAtMs = 0;
    }
    if (ledClearAtMs != 0 && now >= ledClearAtMs) {
        clearLeds();
        ledClearAtMs = 0;
    }
}

// ── Head touch (SI12T via StackChan-BSP) ─────────────────────────────────
static void pollHeadTouch() {
    auto& ts = M5StackChan.TouchSensor;

    // Track the strongest zone while a finger is down; consumed on release.
    if (ts.isPressed()) {
        const auto& in = ts.getIntensities();
        int best = -1, bestVal = 0;
        for (int i = 0; i < 3; i++) {
            if (in[i] > bestVal) { bestVal = in[i]; best = i; }
        }
        if (best >= 0) lastHeadZone = best;
    }

    uint32_t now = millis();
    if (now < headCooldownUntil) return;

    // Swipe fires mid-gesture (all three zones touched in order); the
    // release afterwards would also read as a click, so the shared
    // cooldown set here swallows it.
    if (ts.wasSwipedForward() || ts.wasSwipedBackward()) {
        bool fwd = ts.wasSwipedForward();
        headCooldownUntil = now + HEAD_COOLDOWN_MS;
        emitInteraction("head_swipe", fwd ? "forward" : "backward");
        reactFace(WHALE_HAPPY);
        reactLeds("cyan");
        return;
    }

    if (ts.wasHold()) {
        // Sustained petting. React shy, remember the zone.
        headCooldownUntil = now + HEAD_COOLDOWN_MS;
        static const char* zones[] = {"front", "middle", "back"};
        emitInteraction("head_pet",
                        lastHeadZone >= 0 ? zones[lastHeadZone] : "unknown");
        reactFace(WHALE_SHY);
        reactLeds("#ff5078");   // warm pink
        return;
    }

    if (ts.wasClicked()) {
        headCooldownUntil = now + HEAD_COOLDOWN_MS;
        static const char* zones[] = {"front", "middle", "back"};
        emitInteraction("head_touch",
                        lastHeadZone >= 0 ? zones[lastHeadZone] : "unknown");
        reactFace(WHALE_HAPPY);
        reactLeds("#ffb400");   // warm amber
        if (servoFree()) { trackerHoldOff(5000); servoNod(); }
        return;
    }
}

// ── Screen gestures (FT6336 via M5.Touch) ────────────────────────────────
static void pollScreenTouch() {
    if (M5.Touch.getCount() == 0 && M5.Touch.getDetail().state == m5::touch_state_t::none) {
        // Fast path — nothing touching, nothing pending.
        return;
    }

    uint32_t now = millis();
    if (now < screenCooldownUntil) return;

    auto t = M5.Touch.getDetail();

    if (t.wasHold()) {
        screenCooldownUntil = now + SCREEN_COOLDOWN_MS;
        emitInteraction("screen_long_press");
        reactFace(WHALE_SHY);
        reactLeds("#ff5078");
        return;
    }

    if (t.wasFlicked()) {
        int dx = t.distanceX(), dy = t.distanceY();
        const char* dir = (abs(dx) > abs(dy))
                              ? (dx < 0 ? "left" : "right")
                              : (dy < 0 ? "up" : "down");
        screenCooldownUntil = now + SCREEN_COOLDOWN_MS;
        emitInteraction("screen_swipe", dir);
        reactFace(WHALE_HAPPY);
        reactLeds("blue");
        return;
    }

    // Double tap only — single tap is deliberately unmapped so that casual
    // pokes (and Panice cleaning the screen) don't trigger anything.
    if (t.wasClicked() && t.getClickCount() == 2) {
        screenCooldownUntil = now + SCREEN_COOLDOWN_MS;
        emitInteraction("screen_double_tap");
        reactFace(WHALE_HAPPY);
        reactLeds("green");
        if (servoFree()) { trackerHoldOff(5000); servoNod(); }
        return;
    }
}

// ── Body motion (BMI270 via M5.Imu) ──────────────────────────────────────
static void pollMotion() {
    uint32_t now = millis();
    if (now < nextImuSampleMs) return;
    nextImuSampleMs = now + MOTION_SAMPLE_MS;

    M5.Imu.update();
    float ax, ay, az;
    if (!M5.Imu.getAccel(&ax, &ay, &az)) return;

    float mag = sqrtf(ax * ax + ay * ay + az * az);
    float delta = 0.0f;
    if (lastAccelValid) {
        float dx = ax - lastAx, dy = ay - lastAy, dz = az - lastAz;
        delta = sqrtf(dx * dx + dy * dy + dz * dz);
    }
    lastAx = ax; lastAy = ay; lastAz = az; lastAccelValid = true;

    bool moving = (delta > MOTION_THRESHOLD_G) ||
                  (fabsf(mag - 1.0f) > MOTION_THRESHOLD_G);

    if (!moving) {
        stillCount++;
        if (stillCount >= STILL_SAMPLES_TO_REARM) motionArmed = true;
        liftCount = 0;
        for (int i = 0; i < 8; i++) shakePeakTimes[i] = 0;
        return;
    }

    stillCount = 0;
    if (!motionArmed) return;
    if (now < motionCooldownUntil) return;

    // Shake: enough motion peaks inside a 1s window.
    shakePeakTimes[shakeIdx % 8] = now;
    shakeIdx++;
    int peaks = 0;
    for (int i = 0; i < 8; i++) {
        if (shakePeakTimes[i] > 0 && (now - shakePeakTimes[i]) < SHAKE_WINDOW_MS) {
            peaks++;
        }
    }
    if (peaks >= SHAKE_PEAKS_TO_TRIGGER) {
        motionArmed = false;
        liftCount = 0;
        motionCooldownUntil = now + MOTION_COOLDOWN_MS;
        for (int i = 0; i < 8; i++) shakePeakTimes[i] = 0;
        emitInteraction("shake");
        reactFace(WHALE_POUTY);
        reactLeds("#ff3200");   // alarmed orange-red
        // No servo reaction — driving servos while being shaken fights
        // the human and stresses the gears.
        return;
    }

    // Lift: sustained motion (being carried) rather than sharp peaks.
    liftCount++;
    if (liftCount >= LIFT_SAMPLES_TO_TRIGGER) {
        motionArmed = false;
        liftCount = 0;
        motionCooldownUntil = now + MOTION_COOLDOWN_MS;
        emitInteraction("lift");
        reactFace(WHALE_THINKING);   // wide-eyed "whoa?" approximation
        reactLeds("white");
        return;
    }
}

// ── Public API ───────────────────────────────────────────────────────────
void initInteraction() {
    // SI12T + FT6336 are already brought up by M5StackChan.begin(); only
    // the IMU needs a check. M5.begin() (inside the BSP) inits it when
    // present — BMI270 on the CoreS3 sits at 0x69 and M5Unified knows that.
    imuOk = M5.Imu.isEnabled();
    Serial.printf("[INTERACT] init: head-touch ready, screen ready, imu=%s\n",
                  imuOk ? "ok" : "MISSING");
}

void updateInteraction() {
    pollHeadTouch();
    pollScreenTouch();
    if (imuOk) pollMotion();
    expireReactions();
}
