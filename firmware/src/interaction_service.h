#ifndef INTERACTION_SERVICE_H
#define INTERACTION_SERVICE_H

/**
 * Physical-interaction sensing: head touch (SI12T), body motion (BMI270),
 * and screen gestures (FT6336). Ported interaction design from
 * mo-hantang/Stackchan-HtSz (xiaozhi/ESP-IDF world) onto our Arduino stack.
 *
 * Every detected interaction does two things:
 *   1. Local instant reaction — face + LED flash (+ a small servo move when
 *      idle), so touching the robot feels alive with zero network latency.
 *   2. Emits an unsolicited WS event to the gateway:
 *        {"event":"interaction","kind":"head_touch","detail":"front",...}
 *      The gateway keeps a ring buffer readable via the stackchan_events
 *      MCP tool, so Claude can find out it was petted while away.
 *
 * Sensor plumbing is already alive before this service existed:
 * M5StackChan.update() (called every loop) polls the SI12T and runs
 * M5.update() for the FT6336 — this service only *reads* their state.
 * The BMI270 is the one thing we sample ourselves (10 Hz, via M5.Imu).
 */

// Call in setup() after M5StackChan.begin().
void initInteraction();

// Call every loop() after M5StackChan.update().
void updateInteraction();

#endif
