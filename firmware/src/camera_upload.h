#ifndef CAMERA_UPLOAD_H
#define CAMERA_UPLOAD_H

/**
 * Glue layer between camera_service (JPEG capture) and ws_client (upload to
 * /upload/photo + emit photo_ready event). Kept separate from camera_service
 * so the base capture API stays self-contained and testable.
 */

// Capture a JPEG, multipart-POST it to https://body/upload/photo, emit a
// `photo_ready` WS event with the path the gateway returned. Returns true
// on full success.
bool captureUploadAndNotify();

#endif
