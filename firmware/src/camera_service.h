#ifndef CAMERA_SERVICE_H
#define CAMERA_SERVICE_H

#include <stdint.h>
#include <stddef.h>

/**
 * Stack-chan Camera Service
 * Uses GC0308 sensor on M5Stack CoreS3
 * Captures RGB565 frames and converts to JPEG
 */

// Initialize camera (call in setup after M5.begin)
bool initCamera();

// True once initCamera succeeded (and no deinit since).
bool isCameraReady();

// Grab one frame and downsample to an outW×outH pseudo-luma grid (high
// byte of each RGB565 pixel ≈ red+green brightness). Cheap enough to call
// at 10 Hz — used by tracker_service for frame-diff motion tracking.
bool captureLuma(uint8_t* out, int outW, int outH);

// Capture a JPEG snapshot
// Returns true on success, sets outBuf and outLen
// Caller must free(*outBuf) after use
bool captureJpeg(uint8_t** outBuf, size_t* outLen, int quality = 80);

// Deinitialize camera (to free DMA/I2C resources if needed)
void deinitCamera();

#endif
