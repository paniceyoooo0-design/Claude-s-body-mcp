// face_service.cpp — m5stack-avatar procedural face (replaces PNG-based).
//
// Why m5stack-avatar instead of PNG:
//   1. Real-time lip-sync (amplitude → mouth open ratio, per-frame), vs PNG
//      mode's two-state swap that looks robotic.
//   2. Live eye blinks, breath, micro-expressions — Stack-chan "alive" feel.
//   3. Customizable (eye/mouth shape, palette) without redrawing 7 PNGs.
//   4. Panice explicitly chose path (b) "procedural" over (a) "static art" —
//      see [[project_stackchan_mcp]] 2026-05-24 D 方案 section.
//
// Keeps the same face_service.h interface so mic_service / playback_service
// callers (setFaceExpression / setMouthOpen / setWhaleFace) work unchanged.
// The "WhaleFace" enum names are kept for ABI compat but mapped to the
// equivalent m5avatar Expression — `WHALE_HAPPY` → Expression::Happy etc.

#include "face_service.h"
#include "globals.h"
#include <M5Unified.h>
#include <Avatar.h>

using namespace m5avatar;

static Avatar avatar;
static WhaleFace currentFace = WHALE_CALM;
static bool isTalking = false;
static bool initialized = false;

// Map our WhaleFace enum to m5avatar Expression. Several Stack-chan emotions
// don't have a direct library equivalent (shy / smug / pouty); we approximate
// by reusing close neighbors. Future: switch to custom Face/Eye/Mouth objects
// if Panice wants distinct visuals for these.
static Expression _toExpression(WhaleFace face) {
    switch (face) {
        case WHALE_CALM:     return Expression::Neutral;
        case WHALE_THINKING: return Expression::Doubt;     // squinted eyes
        case WHALE_HAPPY:    return Expression::Happy;
        case WHALE_SLEEPY:   return Expression::Sleepy;
        case WHALE_SHY:      return Expression::Happy;     // best available
        case WHALE_SMUG:     return Expression::Happy;     // best available
        case WHALE_POUTY:    return Expression::Angry;
        default:             return Expression::Neutral;
    }
}

void initFace() {
    avatar.init();
    avatar.setExpression(Expression::Neutral);
    currentFace = WHALE_CALM;
    initialized = true;
    Serial.println("[FACE] m5stack-avatar initialized");
}

void setFaceExpression(FaceExpression expr) {
    if (!initialized) return;

    WhaleFace target;
    switch (expr) {
        case FACE_IDLE:
            // Sleepy at night (server hour 19:00–07:00), calm otherwise. The
            // serverHour global is updated by wifi_manager's syncServerHour().
            target = (serverHour >= 19 || (serverHour >= 0 && serverHour < 7))
                     ? WHALE_SLEEPY : WHALE_CALM;
            isTalking = false;
            break;
        case FACE_LISTENING:
            target = WHALE_THINKING;
            isTalking = false;
            break;
        case FACE_PLAYING:
            target = WHALE_HAPPY;
            isTalking = true;  // enable mouth lip-sync
            break;
        case FACE_THINKING:
            target = WHALE_THINKING;
            isTalking = false;
            break;
        case FACE_HAPPY:
            target = WHALE_HAPPY;
            isTalking = false;
            break;
        default:
            target = WHALE_CALM;
            isTalking = false;
            break;
    }

    if (target != currentFace) {
        avatar.setExpression(_toExpression(target));
        currentFace = target;
    }
}

void setMouthOpen(float ratio) {
    // Driven per-frame by playback_service::updateLipSync() from speaker
    // amplitude. m5avatar takes 0..1 directly and animates the mouth shape.
    if (!initialized || !isTalking) return;
    if (ratio < 0.0f) ratio = 0.0f;
    if (ratio > 1.0f) ratio = 1.0f;
    avatar.setMouthOpenRatio(ratio);
}

void setWhaleFace(WhaleFace face) {
    if (!initialized) return;
    isTalking = false;
    avatar.setExpression(_toExpression(face));
    currentFace = face;
}

WhaleFace getCurrentWhaleFace() {
    return currentFace;
}

const char* getCurrentFaceName() {
    switch (currentFace) {
        case WHALE_CALM:     return "calm";
        case WHALE_THINKING: return "thinking";
        case WHALE_HAPPY:    return "happy";
        case WHALE_SLEEPY:   return "sleepy";
        case WHALE_SHY:      return "shy";
        case WHALE_SMUG:     return "smug";
        case WHALE_POUTY:    return "pouty";
        default:             return "unknown";
    }
}
