#ifndef TRACKER_SERVICE_H
#define TRACKER_SERVICE_H

#include <Arduino.h>

/**
 * Motion tracking + idle scanning — the "it's alive, it looks at you" layer.
 * Ported from mo-hantang/Stackchan-HtSz's FaceTracker.
 *
 * Despite HtSz's class name there is no face detection involved: it grabs
 * 40×30 pseudo-luma frames at 10 Hz, diffs against the previous frame, and
 * steers the servos toward the centroid of changed pixels. Someone walking
 * by (or waving) reads as motion; a static room reads as nothing. Zero ML,
 * a few KB of RAM.
 *
 * Behavior:
 *   - motion seen     → head smoothly follows it; emits a `presence`
 *                       "motion_appear" event (rate-limited)
 *   - motion gone     → after a grace period, emits "motion_lost" and falls
 *                       back to idle mode
 *   - idle            → occasional slow random glances (HtSz's idle scan),
 *                       so the robot never looks frozen
 *
 * The tracker yields to every other servo/camera owner: remote move/nod/etc
 * (via trackerHoldOff), audio playback, an armed mic, and snapshots.
 */

// Call in setup() after initCamera()/initServo().
void initTracker();

// Call every loop(). Cheap no-op when disabled or between sample ticks.
void updateTracker();

// External servo/camera owner (gateway command, interaction reaction,
// snapshot) takes over: tracker freezes for `ms` and drops its previous
// frame so the ownership gap can't read as phantom motion.
void trackerHoldOff(uint32_t ms);

#endif
