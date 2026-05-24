#ifndef MIC_SERVICE_H
#define MIC_SERVICE_H

#include <Arduino.h>

/**
 * Microphone service — VAD-triggered auto-recording with upload to gateway.
 *
 * Architecture (v2):
 *   1. Mic continuously samples at 16kHz, computing RMS per frame.
 *   2. RMS above MIC_TRIGGER_RMS for MIC_TRIGGER_HOLD_MS → start recording.
 *   3. Pre-trigger ring buffer (~300ms) is prepended so we don't clip the
 *      first word.
 *   4. Recording stops on MIC_SILENCE_HOLD_MS of silence, or
 *      MIC_MAX_RECORD_SECONDS hard cap.
 *   5. WAV is built in PSRAM, multipart-POSTed to
 *      https://body/upload/audio, then a `audio_ready` WS event is
 *      emitted with the saved path so any gateway-side stackchan_listen
 *      tool can deliver the transcript to the calling LLM.
 *
 * Mic and Speaker share M5Stack's audio peripheral — only one can be
 * active. playback_service stops Mic before playing; this module's
 * `micResumeRequested` global signals "speaker done, please bring me
 * back up" so main.cpp can call `initMicrophone()` from the loop.
 */

bool initMicrophone();
void updateMicrophone();

// Arm the mic for a single capture window of `duration_ms`. The VAD only
// runs while armed — outside a window, the mic is fully idle so it doesn't
// fight WS/camera/playback for TLS/CPU. After a successful capture+upload,
// OR after duration_ms elapses with no trigger, the mic auto-disarms.
//
// Called by ws_client's handleListen — replaces the original "always-on VAD"
// design which ran the state machine every loop and saturated the TLS stack
// with ambient-triggered uploads.
void armMicrophone(uint32_t duration_ms);

// True iff currently in an active listen window.
bool isMicArmed();

#endif
