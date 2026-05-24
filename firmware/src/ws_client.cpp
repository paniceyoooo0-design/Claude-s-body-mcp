// ws_client.cpp — outbound WS link to the gateway.

#include "ws_client.h"
#include "config.h"
#include "globals.h"
#include "types.h"
#include "queue_manager.h"
#include "servo_service.h"
#include "face_service.h"
#include "led_service.h"
#include "camera_upload.h"
#include "mic_service.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <WebSocketsClient.h>

// We use the standard links2004/WebSockets library — it speaks wss:// over
// the Arduino-WiFi TCP stack and handles TLS via the platform's BearSSL/
// mbedTLS backend. ESP32-S3 has both heap and PSRAM, so the TLS handshake
// fits comfortably.
static WebSocketsClient webSocket;
static bool socketConnected = false;
// Reconnect cadence: try every 5s if the gateway is down. Don't hammer.
static const uint32_t RECONNECT_DELAY_MS = 5000;
// Ping the gateway every 20s; expect pong within 10s; allow 2 missed pings
// before marking the connection dead. Mirrors device_link.py's settings so
// both sides agree on liveness windows.
static const uint32_t PING_INTERVAL_MS = 20000;
static const uint32_t PONG_TIMEOUT_MS  = 10000;
static const uint8_t  PONG_FAILS_DROP  = 2;

// ── Reply helpers ────────────────────────────────────────────────────────
static void sendAck(uint32_t id, JsonVariantConst result) {
    JsonDocument doc;
    doc["id"] = id;
    doc["ok"] = true;
    if (!result.isNull()) doc["result"] = result;
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
}

static void sendAckSimple(uint32_t id, const char* result_str = nullptr) {
    JsonDocument doc;
    doc["id"] = id;
    doc["ok"] = true;
    if (result_str) doc["result"] = result_str;
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
}

static void sendNack(uint32_t id, const String& err) {
    JsonDocument doc;
    doc["id"] = id;
    doc["ok"] = false;
    doc["error"] = err;
    String out;
    serializeJson(doc, out);
    webSocket.sendTXT(out);
}

// ── Method dispatch ──────────────────────────────────────────────────────
// Each handler reads params off `params`, performs the action, and ACKs or
// NACKs. Keep handlers short; long work (TTS download, mic upload) should be
// async or use the existing queue infrastructure.

static void handlePlay(uint32_t id, JsonObjectConst params) {
    const char* url = params["voice_url"] | "";
    if (!url || strlen(url) == 0) {
        sendNack(id, "voice_url required");
        return;
    }
    AudioTask task;
    task.voice_id  = String("ws_") + String(millis());
    task.voice_url = String(url);
    task.priority  = PRIORITY_NORMAL;
    enqueueAudioTask(task);
    Serial.printf("[WS] play -> queued %s\n", url);
    sendAckSimple(id, "queued");
}

static void handleMove(uint32_t id, JsonObjectConst params) {
    float x = params["x"] | 0.0f;
    float y = params["y"] | 0.0f;
    int speed = params["speed"] | 50;
    if (!isServoReady()) { sendNack(id, "servo not ready"); return; }
    bool ok = servoMove(x, y, speed);
    Serial.printf("[WS] move x=%.1f y=%.1f speed=%d -> %s\n", x, y, speed, ok ? "ok" : "fail");
    if (ok) sendAckSimple(id);
    else    sendNack(id, "servo command failed");
}

static void handleGesture(uint32_t id, const String& method) {
    if (!isServoReady()) { sendNack(id, "servo not ready"); return; }
    bool ok = false;
    if      (method == "nod")   ok = servoNod();
    else if (method == "shake") ok = servoShake();
    else if (method == "home")  ok = servoHome(50);
    else { sendNack(id, "unknown gesture"); return; }
    Serial.printf("[WS] gesture %s -> %s\n", method.c_str(), ok ? "ok" : "fail");
    if (ok) sendAckSimple(id);
    else    sendNack(id, "gesture failed");
}

static void handleFace(uint32_t id, JsonObjectConst params) {
    const char* expr = params["expression"] | "";
    WhaleFace target = WHALE_CALM;
    if      (!strcmp(expr, "neutral") || !strcmp(expr, "calm")) target = WHALE_CALM;
    else if (!strcmp(expr, "happy"))                            target = WHALE_HAPPY;
    else if (!strcmp(expr, "sad")    || !strcmp(expr, "pouty")) target = WHALE_POUTY;
    else if (!strcmp(expr, "angry"))                            target = WHALE_POUTY;
    else if (!strcmp(expr, "sleepy"))                           target = WHALE_SLEEPY;
    else if (!strcmp(expr, "doubt")  || !strcmp(expr, "thinking")) target = WHALE_THINKING;
    else { sendNack(id, String("unknown expression: ") + expr); return; }
    setWhaleFace(target);
    Serial.printf("[WS] face -> %s\n", expr);
    sendAckSimple(id, expr);
}

static void handleStatus(uint32_t id) {
    JsonDocument doc;
    doc["uptime_ms"] = millis();
    doc["free_heap"] = ESP.getFreeHeap();
    doc["free_psram"] = ESP.getFreePsram();
    doc["servo_ready"] = isServoReady();
    doc["face"] = getCurrentFaceName();
    doc["wifi_rssi"] = WiFi.RSSI();
    JsonDocument reply;
    reply["id"] = id;
    reply["ok"] = true;
    reply["result"] = doc;
    String out;
    serializeJson(reply, out);
    webSocket.sendTXT(out);
}

// ── Message dispatcher ───────────────────────────────────────────────────
static void onMessage(const char* payload, size_t length) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, payload, length);
    if (err) {
        Serial.printf("[WS] JSON parse error: %s\n", err.c_str());
        return;
    }
    if (!doc["id"].is<uint32_t>() || !doc["method"].is<const char*>()) {
        Serial.println("[WS] message missing id/method");
        return;
    }

    uint32_t id = doc["id"];
    String method = doc["method"].as<String>();
    JsonObjectConst params = doc["params"].as<JsonObjectConst>();

    // Defer to the right handler. v1 firmware scope: play / move / gestures
    // / face / status. LED + listen + snapshot return a clear "not implemented"
    // so the gateway tools surface a clean error rather than timing out.
    if      (method == "play")     handlePlay(id, params);
    else if (method == "move")     handleMove(id, params);
    else if (method == "nod" ||
             method == "shake" ||
             method == "home")     handleGesture(id, method);
    else if (method == "face")     handleFace(id, params);
    else if (method == "status")   handleStatus(id);
    else if (method == "led_one") {
        uint8_t index = params["index"] | 0;
        String color = params["color"] | "";
        if (setLedOne(index, color)) sendAckSimple(id);
        else                         sendNack(id, "bad color or index");
    }
    else if (method == "led_all") {
        String color = params["color"] | "";
        if (setLedsAll(color)) sendAckSimple(id);
        else                   sendNack(id, "bad color");
    }
    else if (method == "led_multi") {
        JsonArrayConst arr = params["colors"].as<JsonArrayConst>();
        if (arr.isNull() || arr.size() != 12) {
            sendNack(id, "colors must be a 12-element array");
        } else {
            String cs[12];
            for (int i = 0; i < 12; i++) cs[i] = arr[i].as<const char*>();
            if (setLedsBulk(cs)) sendAckSimple(id);
            else                 sendNack(id, "bad color in colors[]");
        }
    }
    else if (method == "led_clear") {
        clearLeds();
        sendAckSimple(id);
    }
    else if (method == "listen") {
        // Arm the mic for one capture window. Mic is otherwise off so it
        // doesn't burn TLS bandwidth on ambient triggers. `duration_ms`
        // bounds how long we wait for speech before disarming silently.
        uint32_t dur = params["duration_ms"] | 8000;
        armMicrophone(dur);
        sendAckSimple(id, "listening");
    }
    else if (method == "snapshot") {
        // Ack immediately so the gateway tool can subscribe to the
        // photo_ready event without racing with our upload completion.
        // The actual capture+upload+emit happens after this ack returns.
        sendAckSimple(id, "capturing");
        if (!captureUploadAndNotify()) {
            Serial.println("[WS] snapshot capture+upload failed");
        }
    }
    else                           sendNack(id, String("unknown method: ") + method);
}

// ── WebSocket event handler ──────────────────────────────────────────────
static void wsEvent(WStype_t type, uint8_t* payload, size_t length) {
    switch (type) {
        case WStype_DISCONNECTED:
            if (socketConnected) {
                Serial.println("[WS] disconnected");
                socketConnected = false;
            }
            break;
        case WStype_CONNECTED: {
            socketConnected = true;
            Serial.printf("[WS] connected to %s\n", (char*)payload);
            // Send hello — gateway logs which device just came online.
            JsonDocument doc;
            doc["event"] = "hello";
            doc["device_id"] = WiFi.macAddress();
            doc["firmware"] = "claudes-body-mcp v1";
            doc["free_psram"] = ESP.getFreePsram();
            String out;
            serializeJson(doc, out);
            webSocket.sendTXT(out);
            break;
        }
        case WStype_TEXT:
            onMessage((const char*)payload, length);
            break;
        case WStype_ERROR:
            Serial.printf("[WS] error: %.*s\n", (int)length, (char*)payload);
            break;
        default:
            break;
    }
}

// ── Public API ───────────────────────────────────────────────────────────
void initWsClient() {
    // Parse WSS_URL (e.g. "wss://body.aerogelovepanice.com/ws") into host /
    // port / path / TLS flag for WebSocketsClient::beginSSL/begin.
    String url = String(WSS_URL);
    bool ssl = url.startsWith("wss://");
    String host_path = ssl ? url.substring(6) : url.substring(5);
    int slash = host_path.indexOf('/');
    String host = (slash > 0) ? host_path.substring(0, slash) : host_path;
    String path = (slash > 0) ? host_path.substring(slash)    : "/";
    int port = ssl ? 443 : 80;
    int colon = host.indexOf(':');
    if (colon > 0) {
        port = host.substring(colon + 1).toInt();
        host = host.substring(0, colon);
    }

    Serial.printf("[WS] connecting %s://%s:%d%s\n", ssl ? "wss" : "ws", host.c_str(), port, path.c_str());

    // The Authorization header is a custom WS upgrade header. The library's
    // setExtraHeaders takes a single \r\n-joined string of additional headers.
    String extra = String("Authorization: Bearer ") + STACKCHAN_TOKEN;
    webSocket.setExtraHeaders(extra.c_str());

    if (ssl) {
        // beginSSL(host, port, path) uses the platform's default cert store.
        // ESP32-S3 + Arduino comes with mozilla CA bundle. body.aerogelovepanice.com
        // uses a Let's Encrypt cert which is in that bundle.
        webSocket.beginSSL(host.c_str(), port, path.c_str());
    } else {
        webSocket.begin(host.c_str(), port, path.c_str());
    }
    webSocket.onEvent(wsEvent);
    webSocket.setReconnectInterval(RECONNECT_DELAY_MS);
    webSocket.enableHeartbeat(PING_INTERVAL_MS, PONG_TIMEOUT_MS, PONG_FAILS_DROP);
}

void handleWsClient() {
    webSocket.loop();
}

bool wsEmitEvent(const String& json) {
    if (!socketConnected) return false;
    // WebSocketsClient::sendTXT takes a non-const String& (it might mutate
    // it internally for fragmenting). Copy locally so callers can keep
    // passing const refs without surprises.
    String mutableCopy(json);
    return webSocket.sendTXT(mutableCopy);
}

bool wsIsConnected() {
    return socketConnected;
}
