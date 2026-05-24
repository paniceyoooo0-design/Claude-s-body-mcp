#ifndef WS_CLIENT_H
#define WS_CLIENT_H

#include <Arduino.h>

/**
 * WebSocket client that connects outbound to the gateway and stays alive
 * indefinitely. Replaces the original migratorywhale http_server (which had
 * the device act as an HTTP server on the LAN — incompatible with our
 * NAT'd home + no always-on home box setup).
 *
 * Protocol (matches mcp-server/device_link.py exactly):
 *   gateway -> us:   {"id":N,"method":"...","params":{...}}
 *   us -> gateway:   {"id":N,"ok":true/false,"result"|"error":...}
 *   us -> gateway:   {"event":"...",...}    (unsolicited)
 *
 * Auth: Bearer STACKCHAN_TOKEN in the WS upgrade Authorization header.
 *
 * Lifecycle:
 *   initWsClient()        — call in setup() after WiFi is up
 *   handleWsClient()      — call every loop(); pumps the WS state machine
 *   wsEmitEvent(json)     — fire an unsolicited event from anywhere
 */
void initWsClient();
void handleWsClient();
bool wsEmitEvent(const String& json);
bool wsIsConnected();

#endif
