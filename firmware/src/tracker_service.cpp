// tracker_service.cpp — frame-diff motion tracking + idle scan.
//
// Port of Stackchan-HtSz's FaceTracker + StackChanServo::IdleScanCb, with
// their field-tuned constants. Differences from the original:
//   - runs on the main loop() schedule instead of a FreeRTOS task (all our
//     camera/servo users are loop-context, so no locking needed)
//   - emits presence events to the gateway on appear/lost transitions
//   - explicit hold-off API instead of their Pause/Resume pairs

#include "tracker_service.h"
#include "camera_service.h"
#include "servo_service.h"
#include "mic_service.h"
#include "ws_client.h"
#include "globals.h"

#include <ArduinoJson.h>

// ── Tunables ─────────────────────────────────────────────────────────────
static const int      DS_W = 40;
static const int      DS_H = 30;
static const uint32_t TRACK_PERIOD_MS = 100;   // 10 Hz, same as HtSz

// Pixel-level thresholds (HtSz values): a downsampled cell counts as motion
// when it changed by >20 luma steps; cells brighter than 200 are ignored
// (windows / lamps flicker without anything moving).
static const int  DIFF_THRESHOLD   = 20;
static const int  BRIGHT_CUTOFF    = 200;
// Fewer than 3 changed cells = noise; more than a third of the frame = the
// robot itself is moving (or lighting changed) — both ignored.
static const int  MIN_MOTION_CELLS = 3;
static const int  LOST_TICKS       = 6;        // ~600ms of stillness → lost

// Servo steering (HtSz): proportional nudge with exponential smoothing,
// deadband so breathing-level noise doesn't twitch the head.
static const float SMOOTH_ALPHA   = 0.7f;
static const float DEADBAND       = 0.03f;
static const float YAW_GAIN_DEG   = 6.0f;
static const float PITCH_GAIN_DEG = 4.0f;
// Flip these to -1.0f if the head turns AWAY from motion on real hardware —
// depends on camera vs servo orientation, verifiable only on the desk.
static const float YAW_SIGN   = 1.0f;
static const float PITCH_SIGN = 1.0f;
// Physical envelope while tracking. Our servos allow yaw ±128 / pitch 5–85,
// but chasing motion to the extremes looks frantic; HtSz stays modest.
static const float YAW_LIMIT_DEG   = 45.0f;
static const float PITCH_MIN_DEG   = 5.0f;
static const float PITCH_MAX_DEG   = 50.0f;
static const float PITCH_HOME_DEG  = 10.0f;

// Idle scan: HtSz glances every 4s flat; we randomize 6–14s so it reads as
// curiosity rather than a metronome.
static const uint32_t IDLE_SCAN_MIN_MS = 6000;
static const uint32_t IDLE_SCAN_SPAN_MS = 8000;

// Presence events: at most one motion_appear per minute — a lived-in room
// would otherwise flood the event log.
static const uint32_t APPEAR_EVENT_GAP_MS = 60000;

// ── State ────────────────────────────────────────────────────────────────
static bool     enabled = false;
static uint32_t holdOffUntil = 0;
static uint32_t nextTrackMs = 0;
static uint32_t nextIdleScanMs = 0;

static uint8_t  prevFrame[DS_W * DS_H];
static uint8_t  curFrame[DS_W * DS_H];
static bool     hasPrev = false;

static bool     tracking = false;
static int      noMoveTicks = 0;
static float    yawEst = 0.0f;
static float    pitchEst = PITCH_HOME_DEG;
static float    smoothX = 0.0f, smoothY = 0.0f;

static bool     appearActive = false;
static uint32_t lastAppearEmitMs = 0;

static void emitPresence(const char* kind) {
    JsonDocument doc;
    doc["event"] = "presence";
    doc["kind"]  = kind;
    doc["uptime_ms"] = millis();
    String out;
    serializeJson(doc, out);
    wsEmitEvent(out);
    Serial.printf("[TRACK] %s\n", kind);
}

// The tracker only owns the head when nobody else does.
static bool trackerAllowed() {
    return enabled && isServoReady() && !isPlaying && !isMicArmed() &&
           millis() >= holdOffUntil;
}

static void stopTracking(bool emitLost) {
    if (tracking && emitLost && appearActive) {
        emitPresence("motion_lost");
    }
    if (!tracking && appearActive) {
        // Lost can also happen from a hold-off while not tracking.
        emitPresence("motion_lost");
    }
    tracking = false;
    appearActive = false;
    noMoveTicks = 0;
}

static void track() {
    if (!captureLuma(curFrame, DS_W, DS_H)) return;

    if (!hasPrev) {
        memcpy(prevFrame, curFrame, sizeof(prevFrame));
        hasPrev = true;
        return;
    }

    // Diff centroid. Top rows skipped (HtSz: dy from 3) — the camera sees a
    // slice of ceiling there and ceiling lights are all flicker, no signal.
    long sumX = 0, sumY = 0;
    int  count = 0;
    for (int dy = 3; dy < DS_H - 1; dy++) {
        for (int dx = 1; dx < DS_W - 1; dx++) {
            int idx = dy * DS_W + dx;
            if (curFrame[idx] > BRIGHT_CUTOFF) continue;
            if (abs((int)curFrame[idx] - (int)prevFrame[idx]) > DIFF_THRESHOLD) {
                sumX += dx;
                sumY += dy;
                count++;
            }
        }
    }
    memcpy(prevFrame, curFrame, sizeof(prevFrame));

    int totalCells = (DS_W - 2) * (DS_H - 4);
    if (count < MIN_MOTION_CELLS || count > totalCells / 3) {
        if (tracking && ++noMoveTicks > LOST_TICKS) {
            stopTracking(true);
        }
        return;
    }

    noMoveTicks = 0;
    if (!tracking) {
        tracking = true;
        uint32_t now = millis();
        if (now - lastAppearEmitMs > APPEAR_EVENT_GAP_MS) {
            lastAppearEmitMs = now;
            appearActive = true;
            emitPresence("motion_appear");
        }
    }

    float cx = (float)sumX / count;
    float cy = (float)sumY / count;
    float targetX = (cx - DS_W / 2.0f) / (DS_W / 2.0f);
    float targetY = (cy - DS_H / 2.0f) / (DS_H / 2.0f);

    smoothX = smoothX * (1.0f - SMOOTH_ALPHA) + targetX * SMOOTH_ALPHA;
    smoothY = smoothY * (1.0f - SMOOTH_ALPHA) + targetY * SMOOTH_ALPHA;
    if (fabsf(smoothX) < DEADBAND) smoothX = 0;
    if (fabsf(smoothY) < DEADBAND) smoothY = 0;

    yawEst   -= YAW_SIGN   * smoothX * YAW_GAIN_DEG;
    pitchEst -= PITCH_SIGN * smoothY * PITCH_GAIN_DEG;
    if (yawEst < -YAW_LIMIT_DEG) yawEst = -YAW_LIMIT_DEG;
    if (yawEst >  YAW_LIMIT_DEG) yawEst =  YAW_LIMIT_DEG;
    if (pitchEst < PITCH_MIN_DEG) pitchEst = PITCH_MIN_DEG;
    if (pitchEst > PITCH_MAX_DEG) pitchEst = PITCH_MAX_DEG;

    servoMove(yawEst, pitchEst, 80);
}

static void idleScan() {
    // A slow glance somewhere vaguely forward. Also refreshes yawEst/pitchEst
    // so the next tracking run starts from where the head actually is.
    yawEst   = (float)((int)random(-25, 26));
    pitchEst = (float)((int)random(5, 21));
    servoMove(yawEst, pitchEst, 15);
}

// ── Public API ───────────────────────────────────────────────────────────
void initTracker() {
    enabled = isCameraReady() && isServoReady();
    nextIdleScanMs = millis() + IDLE_SCAN_MIN_MS + random(IDLE_SCAN_SPAN_MS);
    Serial.printf("[TRACK] init: %s\n", enabled ? "enabled" : "DISABLED (no cam/servo)");
}

void updateTracker() {
    uint32_t now = millis();
    if (now < nextTrackMs) return;
    nextTrackMs = now + TRACK_PERIOD_MS;

    if (!trackerAllowed()) {
        // Someone else owns the head/camera. Drop stale state so the takeover
        // motion doesn't read as phantom "appear" when we come back.
        hasPrev = false;
        if (tracking || appearActive) stopTracking(false);
        return;
    }

    track();

    if (!tracking && now >= nextIdleScanMs) {
        nextIdleScanMs = now + IDLE_SCAN_MIN_MS + random(IDLE_SCAN_SPAN_MS);
        idleScan();
        // The scan itself moves the camera — invalidate the frame so our own
        // sweep doesn't look like scene motion.
        hasPrev = false;
    }
}

void trackerHoldOff(uint32_t ms) {
    uint32_t until = millis() + ms;
    if (until > holdOffUntil) holdOffUntil = until;
    hasPrev = false;
}
