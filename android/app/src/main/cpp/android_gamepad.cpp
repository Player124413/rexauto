/**
 * android_gamepad.cpp - SDL3 virtual gamepad fed by the Java touch overlay.
 *
 * The on-screen gamepad (com.rexauto.port.gamepad.*) sends its state
 * through the JNI functions below. They write into a VIRTUAL SDL joystick
 * declared as SDL_JOYSTICK_TYPE_GAMEPAD, so SDL generates standard gamepad
 * events and the runtime's SDL input driver treats the overlay exactly like
 * a physical controller (it opens the device on SDL_EVENT_GAMEPAD_ADDED and
 * feeds the game's XInput API). Zero SDK code is modified by this file.
 *
 * Attach timing is load bearing: SDL_AttachVirtualJoystick() fires
 * SDL_EVENT_GAMEPAD_ADDED once, and the SDK input driver only receives
 * events posted AFTER it installs its SDL_AddEventWatch (inside
 * SDLInputDriver::OnWindowAvailable, which runs during the app's
 * OnInitialize via ReXApp::ConstructRuntime -> InputSystem::AttachWindow).
 * EnsureVirtualPadAttached() is therefore called from android_main right
 * AFTER app->OnInitialize() returns - never earlier, never from Java.
 *
 * Axis conventions (verified against SDL3's virtual gamepad mapping in
 * SDL_virtualjoystick.c + SDL_gamepad.c binding ranges):
 *   - Sticks: raw axis = value * 32767, +Y is DOWN (screen convention
 *     matches SDL's gamepad convention directly).
 *   - Triggers: the virtual mapping binds plain full-range axes to trigger
 *     outputs [0, 32767], i.e. raw -32768 maps to trigger 0 and raw +32767
 *     to trigger 1. Raw = lerp(-32768, +32767, value).
 *
 * @added  deivid22srk, 2026 - Android port
 */

#include "android_gamepad.h"

#include <SDL3/SDL_gamepad.h>
#include <SDL3/SDL_init.h>
#include <SDL3/SDL_joystick.h>

#include <jni.h>

#include <algorithm>
#include <mutex>

#include <rex/logging.h>

namespace rexport::gamepad {

namespace {

constexpr int kNumButtons = 16;  // SDL_GAMEPAD_BUTTON_* order, see PadInputBridge
constexpr int kNumAxes = 6;      // LX LY RX RY LT RT
constexpr Uint32 kButtonMask = 0xFFFFu;  // buttons 0..15 valid
constexpr Uint32 kAxisMask = 0x003Fu;    // axes 0..5 valid
// Microsoft/Xbox 360 identity: makes SDL report an xbox360-type gamepad and
// keeps the driver's logs recognizable.
constexpr Uint16 kVendorId = 0x045E;
constexpr Uint16 kProductId = 0x028E;

std::mutex g_mutex;
SDL_Joystick* g_joystick = nullptr;
SDL_JoystickID g_instance = 0;

Sint16 StickRaw(float value) {
  return static_cast<Sint16>(
      std::lround(std::clamp(value, -1.0f, 1.0f) * 32767.0f));
}

Sint16 TriggerRaw(float value) {
  // Raw axis range is [-32768, 32767]; the gamepad layer maps that linearly
  // onto trigger outputs [0, 32767] (see file comment).
  return static_cast<Sint16>(
      std::lround(std::clamp(value, 0.0f, 1.0f) * 65535.0f) - 32768);
}

bool AttachLocked() {
  if (g_joystick) {
    return true;
  }
  // The gamepad subsystem pulls the joystick subsystem in with it.
  if (!SDL_InitSubSystem(SDL_INIT_GAMEPAD)) {
    REXLOG_ERROR("virtual gamepad: SDL_InitSubSystem(SDL_INIT_GAMEPAD) failed: {}",
                 SDL_GetError());
    return false;
  }

  SDL_VirtualJoystickDesc desc;
  SDL_INIT_INTERFACE(&desc);
  desc.type = SDL_JOYSTICK_TYPE_GAMEPAD;
  desc.naxes = kNumAxes;
  desc.nbuttons = kNumButtons;
  desc.button_mask = kButtonMask;
  desc.axis_mask = kAxisMask;
  desc.vendor_id = kVendorId;
  desc.product_id = kProductId;
  desc.name = "Dante's Inferno Touch Pad";

  g_instance = SDL_AttachVirtualJoystick(&desc);
  if (!g_instance) {
    REXLOG_ERROR("virtual gamepad: SDL_AttachVirtualJoystick failed: {}",
                 SDL_GetError());
    return false;
  }
  g_joystick = SDL_OpenJoystick(g_instance);
  if (!g_joystick) {
    REXLOG_ERROR("virtual gamepad: SDL_OpenJoystick({}) failed: {}", g_instance,
                 SDL_GetError());
    SDL_DetachVirtualJoystick(g_instance);
    g_instance = 0;
    return false;
  }
  REXLOG_INFO("virtual gamepad: attached (instance {}, {} buttons, {} axes)",
              static_cast<unsigned>(g_instance), kNumButtons, kNumAxes);
  return true;
}

}  // namespace

bool EnsureVirtualPadAttached() {
  std::lock_guard<std::mutex> guard(g_mutex);
  return AttachLocked();
}

}  // namespace rexport::gamepad

// ---------------------------------------------------------------------------
// JNI entry points (called from the UI thread by the Java overlay)
// ---------------------------------------------------------------------------

extern "C" {

JNIEXPORT jboolean JNICALL
Java_com_rexauto_port_gamepad_PadInputBridge_nativeSetButton(
    JNIEnv* /*env*/, jclass /*clazz*/, jint button, jboolean down) {
  std::lock_guard<std::mutex> guard(rexport::gamepad::g_mutex);
  if (!rexport::gamepad::g_joystick || button < 0 ||
      button >= rexport::gamepad::kNumButtons) {
    return JNI_FALSE;
  }
  SDL_SetJoystickVirtualButton(rexport::gamepad::g_joystick,
                               static_cast<int>(button), down == JNI_TRUE);
  return JNI_TRUE;
}

JNIEXPORT jboolean JNICALL
Java_com_rexauto_port_gamepad_PadInputBridge_nativeSetStick(
    JNIEnv* /*env*/, jclass /*clazz*/, jint stick, jfloat x, jfloat y) {
  std::lock_guard<std::mutex> guard(rexport::gamepad::g_mutex);
  if (!rexport::gamepad::g_joystick || stick < 0 || stick > 1) {
    return JNI_FALSE;
  }
  const int base = (stick == 0) ? 0 : 2;
  SDL_SetJoystickVirtualAxis(rexport::gamepad::g_joystick, base + 0,
                             rexport::gamepad::StickRaw(x));
  SDL_SetJoystickVirtualAxis(rexport::gamepad::g_joystick, base + 1,
                             rexport::gamepad::StickRaw(y));
  return JNI_TRUE;
}

JNIEXPORT jboolean JNICALL
Java_com_rexauto_port_gamepad_PadInputBridge_nativeSetTrigger(
    JNIEnv* /*env*/, jclass /*clazz*/, jint trigger, jfloat value) {
  std::lock_guard<std::mutex> guard(rexport::gamepad::g_mutex);
  if (!rexport::gamepad::g_joystick || trigger < 0 || trigger > 1) {
    return JNI_FALSE;
  }
  const int axis = (trigger == 0) ? 4 : 5;
  SDL_SetJoystickVirtualAxis(rexport::gamepad::g_joystick, axis,
                             rexport::gamepad::TriggerRaw(value));
  return JNI_TRUE;
}

}  // extern "C"
