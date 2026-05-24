// camera_upload.cpp — capture + multipart upload + event emit.
//
// Same recipe as mic_service::uploadAndNotify but for JPEG/photo instead of
// WAV/audio. Multipart body hand-crafted to avoid pulling in another lib.

#include "camera_upload.h"
#include "camera_service.h"
#include "config.h"
#include "ws_client.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// Derive https://host/upload/photo from WSS_URL — same approach as mic_service.
static String uploadPhotoUrl() {
    String url = String(WSS_URL);
    if (url.startsWith("wss://"))      url = "https://" + url.substring(6);
    else if (url.startsWith("ws://"))  url = "http://"  + url.substring(5);
    int pathStart = url.indexOf('/', 8);
    if (pathStart > 0) url = url.substring(0, pathStart);
    url += "/upload/photo";
    return url;
}

bool captureUploadAndNotify() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("[CAM] WiFi down");
        return false;
    }

    uint8_t* jpg = nullptr;
    size_t   jpg_len = 0;
    if (!captureJpeg(&jpg, &jpg_len, 80)) return false;

    String url = uploadPhotoUrl();
    Serial.printf("[CAM] uploading %u bytes -> %s\n", (unsigned)jpg_len, url.c_str());

    WiFiClientSecure secure;
    secure.setInsecure();
    HTTPClient http;
    if (!http.begin(secure, url)) {
        Serial.println("[CAM] http.begin failed");
        free(jpg);
        return false;
    }
    http.addHeader("Authorization", String("Bearer ") + STACKCHAN_TOKEN);

    const char* boundary = "----stackchanbound9f8e7d6c5b4a3210";
    String head = String("--") + boundary + "\r\n"
                + "Content-Disposition: form-data; name=\"file\"; filename=\"snap.jpg\"\r\n"
                + "Content-Type: image/jpeg\r\n\r\n";
    String tail = String("\r\n--") + boundary + "--\r\n";
    http.addHeader("Content-Type", String("multipart/form-data; boundary=") + boundary);

    size_t total = head.length() + jpg_len + tail.length();
    uint8_t* body = (uint8_t*)ps_malloc(total);
    if (!body) {
        Serial.println("[CAM] body alloc failed");
        free(jpg);
        http.end();
        return false;
    }
    memcpy(body,                            head.c_str(), head.length());
    memcpy(body + head.length(),            jpg,          jpg_len);
    memcpy(body + head.length() + jpg_len,  tail.c_str(), tail.length());
    free(jpg);

    int code = http.sendRequest("POST", body, total);
    free(body);

    if (code != HTTP_CODE_OK) {
        Serial.printf("[CAM] upload HTTP=%d body=%s\n", code, http.getString().c_str());
        http.end();
        return false;
    }
    String payload = http.getString();
    http.end();

    JsonDocument doc;
    if (deserializeJson(doc, payload) != DeserializationError::Ok) {
        Serial.println("[CAM] upload response parse error");
        return false;
    }
    const char* path = doc["path"] | "";
    Serial.printf("[CAM] uploaded ok, path=%s\n", path);

    JsonDocument ev;
    ev["event"] = "photo_ready";
    ev["path"]  = path;
    String out;
    serializeJson(ev, out);
    wsEmitEvent(out);
    return true;
}
