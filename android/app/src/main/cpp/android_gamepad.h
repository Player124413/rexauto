/**
 * android_gamepad.h - SDL3 virtual gamepad for the on-screen touch overlay.
 *
 * @added  deivid22srk, 2026 - Android port
 */

#pragma once

namespace rexport::gamepad {

/**
 * Attaches the virtual gamepad (idempotent, thread-safe). MUST be called
 * only after the runtime app's OnInitialize() returned - see the timing
 * comment in android_gamepad.cpp.
 *
 * @return true when the virtual pad is live.
 */
bool EnsureVirtualPadAttached();

}  // namespace rexport::gamepad
