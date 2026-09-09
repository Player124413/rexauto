package com.rexauto.port.gamepad;

/**
 * Java -> native bridge for the on-screen virtual gamepad.
 *
 * The natives live in libmain.so (android_gamepad.cpp) and drive an SDL3
 * VIRTUAL gamepad: SDL routes every state change through the standard
 * gamepad event pipeline, the runtime's SDL input driver picks it up like
 * any physical controller (SDL_EVENT_GAMEPAD_ADDED), and the recompiled
 * game reads it through its regular XInput API. No SDK code is touched.
 *
 * Button indices mirror SDL3's SDL_GAMEPAD_BUTTON_* order 1:1: the native
 * virtual gamepad declares exactly these 16 buttons in this order, so these
 * constants ARE the SDL button indices. Axis handling lives in the native
 * side (SDL half-axis semantics for triggers, full range for sticks).
 *
 * Every method is safe to call at any time from the UI thread: before the
 * native side is attached (or if libmain.so failed to load) they are no-ops.
 *
 * ATTACH POLICY: the virtual gamepad is attached by the native side
 * (rexport::gamepad::EnsureVirtualPadAttached, called from android_main right
 * after the runtime app OnInitialize, i.e. after the SDK input driver has
 * installed its event watch). Java MUST NOT attach earlier: a premature
 * SDL_EVENT_GAMEPAD_ADDED would fire before the driver's watch exists and
 * the pad would never be opened.
 */
public final class PadInputBridge {

    // --- SDL_GAMEPAD_BUTTON_* order (do not reorder: it is the native ABI) ---
    public static final int BTN_A = 0;    // SDL_GAMEPAD_BUTTON_SOUTH
    public static final int BTN_B = 1;    // SDL_GAMEPAD_BUTTON_EAST
    public static final int BTN_X = 2;    // SDL_GAMEPAD_BUTTON_WEST
    public static final int BTN_Y = 3;    // SDL_GAMEPAD_BUTTON_NORTH
    public static final int BTN_BACK = 4;
    public static final int BTN_GUIDE = 5;
    public static final int BTN_START = 6;
    public static final int BTN_L3 = 7;   // SDL_GAMEPAD_BUTTON_LEFT_STICK
    public static final int BTN_R3 = 8;   // SDL_GAMEPAD_BUTTON_RIGHT_STICK
    public static final int BTN_LB = 9;   // SDL_GAMEPAD_BUTTON_LEFT_SHOULDER
    public static final int BTN_RB = 10;  // SDL_GAMEPAD_BUTTON_RIGHT_SHOULDER
    public static final int BTN_DPAD_UP = 11;
    public static final int BTN_DPAD_DOWN = 12;
    public static final int BTN_DPAD_LEFT = 13;
    public static final int BTN_DPAD_RIGHT = 14;
    public static final int BTN_COUNT = 16;

    public static final int STICK_LEFT = 0;
    public static final int STICK_RIGHT = 1;
    public static final int TRIGGER_LEFT = 0;
    public static final int TRIGGER_RIGHT = 1;

    private PadInputBridge() {
    }

    public static void setButton(int button, boolean down) {
        try {
            nativeSetButton(button, down);
        } catch (Throwable ignored) {
            // Native side not attached/loaded: nothing we can do here; the
            // native startup path attaches the pad before the game starts.
        }
    }

    /** x/y in [-1, 1]; +y is DOWN (SDL gamepad convention). */
    public static void setStick(int stick, float x, float y) {
        try {
            nativeSetStick(stick, x, y);
        } catch (Throwable ignored) {
        }
    }

    /** value in [0, 1]. */
    public static void setTrigger(int trigger, float value) {
        try {
            nativeSetTrigger(trigger, value);
        } catch (Throwable ignored) {
        }
    }

    private static native void nativeSetButton(int button, boolean down);

    private static native void nativeSetStick(int stick, float x, float y);

    private static native void nativeSetTrigger(int trigger, float value);
}
