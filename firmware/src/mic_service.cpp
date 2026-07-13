// mic_service.cpp — v2 VAD + upload to gateway.
//
// Rewritten from the migratorywhale original to drop the HTTP-buffer model
// (the device used to expose /audio for the gateway to pull) in favor of
// device-pushes-to-gateway upload + WS event notification. Same VAD state
// machine, new I/O path.

#include <M5Unified.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

#include "mic_service.h"
#include "globals.h"
#include "config.h"
#include "types.h"
#include "face_service.h"
#include "led_service.h"
#include "ws_client.h"

enum MicState {
    MIC_IDLE = 0,
    MIC_TRIGGERING,
    MIC_RECORDING,
    MIC_SENDING
};

#pragma pack(push, 1)
struct WAVHeader {
    char riff[4] = {'R','I','F','F'};
    uint32_t file_size;
    char wave[4] = {'W','A','V','E'};
    char fmt_[4] = {'f','m','t',' '};
    uint32_t fmt_size = 16;
    uint16_t audio_format = 1;
    uint16_t channels = 1;
    uint32_t sample_rate = MIC_SAMPLE_RATE;
    uint32_t byte_rate = MIC_SAMPLE_RATE * 2;
    uint16_t block_align = 2;
    uint16_t bits_per_sample = 16;
    char data_[4] = {'d','a','t','a'};
    uint32_t data_size;
};
#pragma pack(pop)

static int16_t* record_buffer = nullptr;
static size_t max_samples = MIC_SAMPLE_RATE * MIC_MAX_RECORD_SECONDS;
static size_t recorded_samples = 0;
static MicState mic_state = MIC_IDLE;
static uint32_t trigger_start_ms = 0;
static uint32_t silence_start_ms = 0;
static int16_t pre_trigger_buf[PRE_TRIGGER_BUFFER_SAMPLES];
static size_t  pre_buf_write = 0;
static bool    pre_buf_full  = false;

// Armed-window state (v2.3 redesign): mic is off-by-default; ws_client's
// listen handler arms us for one window. Outside the window the state
// machine doesn't even run, freeing the TLS stack for WS + camera.
static bool     mic_armed = false;
static uint32_t arm_deadline_ms = 0;
static uint32_t arm_duration_ms = 0;
static bool     speech_seen = false;   // window 内出现过说话级别的音量

static void disarmMic() {
    mic_armed = false;
    mic_state = MIC_IDLE;
    pre_buf_write = 0;
    pre_buf_full  = false;
    silence_start_ms = 0;
    trigger_start_ms = 0;
    clearLeds();   // 聆听灯灭 = 没在听
}

void armMicrophone(uint32_t duration_ms) {
    mic_armed = true;
    arm_duration_ms = duration_ms;
    arm_deadline_ms = millis() + duration_ms;
    // 2026-07-13: record the WHOLE armed window instead of VAD-gating the
    // start. Wind/fans kept tripping the old trigger and Panice's actual
    // words fell outside the captured clip; Scribe pulls speech out of
    // ambient noise fine, so just hand it everything the window hears.
    recorded_samples = 0;
    silence_start_ms = 0;
    speech_seen = false;
    mic_state = MIC_RECORDING;
    setFaceExpression(FACE_LISTENING);
    setLedsAll("#00b4d2");   // 聆听指示：青色灯环 =「现在可以说」（Panice 点名要的）
    Serial.printf("[MIC] armed %u ms (full-window capture)\n", (unsigned)duration_ms);
}

bool isMicArmed() {
    return mic_armed;
}

static inline float calcRmsNorm(const int16_t* data, size_t n) {
    if (n == 0) return 0.0f;
    float sum = 0.0f;
    for (size_t i = 0; i < n; ++i) {
        float x = (float)data[i] / 32768.0f;
        sum += x * x;
    }
    return sqrtf(sum / (float)n);
}

// Build the upload URL from WSS_URL by swapping scheme + path. We keep
// the upload URL out of config.h so there's one source of truth (the WSS
// URL) — derivation guarantees they point at the same VPS+Caddy.
static String uploadAudioUrl() {
    String url = String(WSS_URL);
    if (url.startsWith("wss://"))      url = "https://" + url.substring(6);
    else if (url.startsWith("ws://"))  url = "http://"  + url.substring(5);
    int pathStart = url.indexOf('/', 8);  // skip past "https://"
    if (pathStart > 0) url = url.substring(0, pathStart);
    url += "/upload/audio";
    return url;
}

static bool uploadAndNotify(const uint8_t* wav, size_t wav_size) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[MIC] WiFi disconnected");
        return false;
    }

    String url = uploadAudioUrl();
    Serial.printf("[MIC] uploading %u bytes -> %s\n", (unsigned)wav_size, url.c_str());

    WiFiClientSecure secure;
    secure.setInsecure();
    HTTPClient http;
    if (!http.begin(secure, url)) {
        Serial.println("[MIC] http.begin failed");
        return false;
    }
    // A full 8s clip is ~256KB; over ESP32 TLS that's well past HTTPClient's
    // 5s default timeout — the client aborted mid-body and the gateway saw
    // "Connection lost" (2026-07-13, killed the first table conversations).
    http.setTimeout(30000);
    http.addHeader("Authorization", String("Bearer ") + STACKCHAN_TOKEN);

    // Multipart form-data with one field `file`. We hand-craft the body so
    // we don't have to drag in an additional multipart library — ESP32
    // Arduino HTTPClient doesn't have a built-in multipart helper.
    const char* boundary = "----stackchanbound9f8e7d6c5b4a3210";
    String head = String("--") + boundary + "\r\n"
                + "Content-Disposition: form-data; name=\"file\"; filename=\"rec.wav\"\r\n"
                + "Content-Type: audio/wav\r\n\r\n";
    String tail = String("\r\n--") + boundary + "--\r\n";
    http.addHeader("Content-Type", String("multipart/form-data; boundary=") + boundary);

    size_t total = head.length() + wav_size + tail.length();
    uint8_t* body = (uint8_t*)ps_malloc(total);
    if (!body) {
        Serial.println("[MIC] body alloc failed");
        http.end();
        return false;
    }
    memcpy(body,                            head.c_str(), head.length());
    memcpy(body + head.length(),            wav,          wav_size);
    memcpy(body + head.length() + wav_size, tail.c_str(), tail.length());

    int code = http.sendRequest("POST", body, total);
    free(body);

    if (code != HTTP_CODE_OK) {
        Serial.printf("[MIC] upload HTTP=%d body=%s\n", code, http.getString().c_str());
        http.end();
        return false;
    }
    String payload = http.getString();
    http.end();

    JsonDocument doc;
    if (deserializeJson(doc, payload) != DeserializationError::Ok) {
        Serial.println("[MIC] upload response parse error");
        return false;
    }
    const char* path = doc["path"] | "";
    Serial.printf("[MIC] uploaded ok, path=%s\n", path);

    // Tell gateway "I just uploaded a recording, here's where". The gateway's
    // stackchan_listen tool is subscribed to this event and will pick up the
    // file, run STT, and return the transcript to the calling LLM.
    JsonDocument ev;
    ev["event"] = "audio_ready";
    ev["path"]  = path;
    String out;
    serializeJson(ev, out);
    wsEmitEvent(out);
    return true;
}

static bool isValidAudio(const int16_t* audio_data, size_t sample_count) {
    if (sample_count < MIC_MIN_VALID_SAMPLES) {
        Serial.printf("[MIC] too short (%u), discarding\n", (unsigned)sample_count);
        return false;
    }
    size_t check_samples = MIC_SAMPLE_RATE / 2;
    if (sample_count > check_samples) {
        float early_rms = calcRmsNorm(audio_data, check_samples);
        if (early_rms < MIC_VOICE_CONFIRM_RMS) {
            Serial.printf("[MIC] no voice (early RMS=%.3f), discarding\n", early_rms);
            return false;
        }
    }
    return true;
}

static uint8_t* buildWav(const int16_t* audio_data, size_t sample_count, size_t& wav_size) {
    WAVHeader header;
    header.data_size = sample_count * 2;
    header.file_size = header.data_size + sizeof(WAVHeader) - 8;
    wav_size = sizeof(WAVHeader) + header.data_size;
    uint8_t* wav = (uint8_t*)ps_malloc(wav_size);
    if (!wav) {
        Serial.println("[MIC] WAV alloc failed");
        return nullptr;
    }
    memcpy(wav, &header, sizeof(WAVHeader));
    memcpy(wav + sizeof(WAVHeader), audio_data, header.data_size);
    return wav;
}

static void applyMicConfig() {
    auto mic_cfg = M5.Mic.config();
    mic_cfg.sample_rate        = MIC_SAMPLE_RATE;
    mic_cfg.stereo             = false;
    mic_cfg.magnification      = MIC_MAGNIFICATION;
    mic_cfg.noise_filter_level = MIC_NOISE_FILTER_LEVEL;
    M5.Mic.config(mic_cfg);
}

bool initMicrophone() {
    Serial.println("[MIC] init");
    memset(pre_trigger_buf, 0, sizeof(pre_trigger_buf));
    pre_buf_write = 0;
    pre_buf_full  = false;

    if (M5.Speaker.isRunning()) {
        M5.Speaker.end();
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    applyMicConfig();
    if (!M5.Mic.begin()) {
        Serial.println("[MIC] Mic.begin failed");
        return false;
    }
    if (!record_buffer) {
        record_buffer = (int16_t*)ps_malloc(max_samples * sizeof(int16_t));
        if (!record_buffer) {
            Serial.println("[MIC] record buffer alloc failed");
            return false;
        }
    }
    Serial.printf("[MIC] ready sr=%d maxSec=%d\n", MIC_SAMPLE_RATE, MIC_MAX_RECORD_SECONDS);
    mic_state = MIC_IDLE;
    return true;
}

// Finish a capture: upload if it holds real audio, then disarm.
static void finalizeAndSend(const char* reason) {
    mic_state = MIC_SENDING;
    Serial.printf("[MIC] end samples=%u reason=%s\n", (unsigned)recorded_samples, reason);
    setFaceExpression(FACE_THINKING);
    if (isValidAudio(record_buffer, recorded_samples)) {
        size_t wav_size = 0;
        uint8_t* wav = buildWav(record_buffer, recorded_samples, wav_size);
        if (wav) {
            bool ok = uploadAndNotify(wav, wav_size);
            free(wav);
            Serial.printf("[MIC] upload %s\n", ok ? "ok" : "fail");
        }
    }
    setFaceExpression(FACE_IDLE);
    disarmMic();
}

void updateMicrophone() {
    if (!M5.Mic.isEnabled()) return;
    if (isPlaying) {
        // Armed while the speaker is still talking (I2S is shared, mic can't
        // run yet): keep sliding the window so it starts fresh after playback
        // instead of expiring uselessly mid-speech.
        if (mic_armed) arm_deadline_ms = millis() + arm_duration_ms;
        return;
    }

    // The big change vs v2.2: only do anything when armed. Outside an armed
    // window we don't even read mic frames — the TLS stack gets a break and
    // ambient noise never triggers stray uploads.
    if (!mic_armed) return;

    // Window over: if we were recording and got real length, send what we
    // have (full-window mode records from the start); else disarm quietly.
    if ((int32_t)(millis() - arm_deadline_ms) >= 0 && mic_state != MIC_SENDING) {
        if (mic_state == MIC_RECORDING && recorded_samples > MIC_SAMPLE_RATE / 2) {
            finalizeAndSend("window");
        } else {
            Serial.println("[MIC] window expired with no capture");
            setFaceExpression(FACE_IDLE);
            disarmMic();
        }
        return;
    }

    static int16_t frame[MIC_FRAME_SAMPLES];
    if (!M5.Mic.record(frame, MIC_FRAME_SAMPLES, MIC_SAMPLE_RATE)) return;
    size_t got = MIC_FRAME_SAMPLES;

    float rms = calcRmsNorm(frame, got);
    uint32_t now = millis();

    if (mic_state == MIC_IDLE || mic_state == MIC_TRIGGERING) {
        for (size_t i = 0; i < got; i++) {
            pre_trigger_buf[pre_buf_write] = frame[i];
            pre_buf_write = (pre_buf_write + 1) % PRE_TRIGGER_BUFFER_SAMPLES;
            if (pre_buf_write == 0) pre_buf_full = true;
        }
    }

    switch (mic_state) {
        case MIC_IDLE:
            if (rms > MIC_TRIGGER_RMS) {
                trigger_start_ms = now;
                mic_state = MIC_TRIGGERING;
            }
            break;

        case MIC_TRIGGERING:
            if (rms > MIC_TRIGGER_RMS) {
                if (now - trigger_start_ms >= MIC_TRIGGER_HOLD_MS) {
                    if (pre_buf_full) {
                        size_t older = PRE_TRIGGER_BUFFER_SAMPLES - pre_buf_write;
                        memcpy(record_buffer,
                               pre_trigger_buf + pre_buf_write,
                               older * sizeof(int16_t));
                        memcpy(record_buffer + older,
                               pre_trigger_buf,
                               pre_buf_write * sizeof(int16_t));
                        recorded_samples = PRE_TRIGGER_BUFFER_SAMPLES;
                    } else {
                        memcpy(record_buffer, pre_trigger_buf, pre_buf_write * sizeof(int16_t));
                        recorded_samples = pre_buf_write;
                    }
                    pre_buf_write = 0;
                    pre_buf_full  = false;
                    silence_start_ms = 0;
                    mic_state = MIC_RECORDING;
                    setFaceExpression(FACE_LISTENING);
                    Serial.printf("[MIC] -> RECORDING (pre-buf %u)\n", (unsigned)recorded_samples);
                }
            } else {
                mic_state = MIC_IDLE;
            }
            break;

        case MIC_RECORDING: {
            size_t remain = max_samples - recorded_samples;
            size_t to_copy = (got < remain) ? got : remain;
            memcpy(record_buffer + recorded_samples, frame, to_copy * sizeof(int16_t));
            recorded_samples += to_copy;

            bool maxed = (recorded_samples >= max_samples);
            if (rms > MIC_TRIGGER_RMS) speech_seen = true;
            if (rms < MIC_SILENCE_RMS) {
                if (silence_start_ms == 0) silence_start_ms = now;
            } else {
                silence_start_ms = 0;
            }
            // Early cut on silence ONLY after we heard speech-level audio —
            // full-window mode starts recording immediately, so a quiet room
            // would otherwise "silence-end" before she even opened her mouth.
            bool silent_end = (speech_seen && silence_start_ms != 0 &&
                               (now - silence_start_ms) >= MIC_SILENCE_HOLD_MS);

            if (maxed || silent_end) {
                // One listen window = one capture max; LLM calls listen
                // again to arm another.
                finalizeAndSend(maxed ? "max" : "silence");
            }
            break;
        }

        case MIC_SENDING:
            break;
    }
}
