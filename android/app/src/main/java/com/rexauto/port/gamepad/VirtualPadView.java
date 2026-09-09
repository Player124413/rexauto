package com.rexauto.port.gamepad;

import android.app.Activity;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.os.SystemClock;
import android.util.Log;
import android.view.DisplayCutout;
import android.view.HapticFeedbackConstants;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowInsets;

/**
 * The on-screen virtual gamepad overlay.
 *
 * A transparent View stacked ON TOP of the SDL surface inside SDLActivity's
 * layout (installed by MainActivity after super.onCreate). Responsibilities:
 *
 *  - INPUT: a small multi-touch state machine. Every finger captures at most
 *    one control (stick-priority + nearest-wins among the controls whose hit
 *    zone contains the touch point; sticks and the d-pad only capture near
 *    their visual shape). The overlay CONSUMES the whole gesture - touches on
 *    empty space are ignored rather than passed through, so a resting finger
 *    can never claim the stream and starve later fingers on real controls.
 *    State changes go straight to PadInputBridge (JNI), which feeds an SDL3
 *    virtual gamepad; there is no polling and no queue, so input latency is
 *    one event dispatch.
 *  - RENDERING: vector-drawn "glass" controls (no bitmaps), with press
 *    animations, analog trigger fill bars, live stick deflection and d-pad
 *    direction highlights. Everything is pre-allocated: zero allocations per
 *    frame or per event.
 *  - SETTINGS: the small gear opens PadSettingsDialog (scale/opacity/haptics/
 *    hide). Hidden mode collapses the pad to a slim restore tab on the right
 *    edge, so the overlay can never become unrecoverable.
 *
 * Threading: constructed/used entirely on the UI thread. The JNI setters are
 * internally safe, and SDL3's virtual joystick API is thread-safe.
 */
public class VirtualPadView extends View {

    private static final String TAG = "VirtualPad";

    // D-pad direction bits (mDpadMask).
    private static final int DP_UP = 1;
    private static final int DP_DOWN = 2;
    private static final int DP_LEFT = 4;
    private static final int DP_RIGHT = 8;

    /** Finger must travel beyond this fraction of the d-pad radius to engage. */
    private static final float DPAD_ENGAGE = 0.35f;
    /** D-pad sector-change hysteresis (degrees) against touch jitter. */
    private static final float DPAD_HYST_DEG = 4f;
    /** Radial deadzone fraction of the stick radius. */
    private static final float STICK_DEADZONE = 0.06f;
    /** Press in/out animation length (ms). */
    private static final long PRESS_ANIM_MS = 90;
    /** Analog trigger ramp on press (ms). */
    private static final long TRIGGER_RAMP_MS = 70;
    /** Pressed visual scale. */
    private static final float PRESS_SCALE = 0.94f;

    /** One finger's capture state. */
    private static final class Pointer {
        int control = -1;   // PadGeometry.ID_* or -1 when not captured
        float x, y;
    }

    private final PadTheme mTheme = new PadTheme();
    private final SparsePointerArray mPointers = new SparsePointerArray(8);

    private PadSettings mSettings;
    private PadGeometry.Control[] mControls = new PadGeometry.Control[0];
    private PadGeometry.Control mTab;

    private final float[] mStick = new float[4];      // lx ly rx ry (sent state)
    private final float[] mCapX = new float[2];       // visual cap offsets (px)
    private final float[] mCapY = new float[2];
    private final float[] mTrigger = new float[2];    // sent trigger values
    private final long[] mTriggerAt = new long[2];    // ramp start time, 0 = idle
    private final boolean[] mPressed = new boolean[14];  // visual pressed per control
    private final boolean[] mDownSent = new boolean[14]; // button edge guard
    private final long[] mPressAt = new long[14];        // last press/release anchor
    private final float[] mEase = new float[14];         // animated 0..1 per control
    private int mDpadMask;
    private boolean mHidden;
    private final int[] mInset = new int[4];             // L T R B safe insets

    private boolean mTicking;
    private final Runnable mTick = new Runnable() {
        @Override
        public void run() {
            tickAnimations();
        }
    };

    /** Pre-allocated pointer pool (Android reports at most 10 pointers). */
    private final Pointer[] mPool = new Pointer[10];
    private int mPoolSize;

    /** Minimal int->Pointer map with array semantics (no autoboxing). */
    private static final class SparsePointerArray {
        private int[] keys;
        private Pointer[] values;
        private int size;

        SparsePointerArray(int capacity) {
            keys = new int[capacity];
            values = new Pointer[capacity];
        }

        Pointer get(int key) {
            for (int i = 0; i < size; i++) {
                if (keys[i] == key) {
                    return values[i];
                }
            }
            return null;
        }

        void put(int key, Pointer value) {
            for (int i = 0; i < size; i++) {
                if (keys[i] == key) {
                    values[i] = value;
                    return;
                }
            }
            if (size == keys.length) {
                int[] nk = new int[size * 2];
                Pointer[] nv = new Pointer[size * 2];
                System.arraycopy(keys, 0, nk, 0, size);
                System.arraycopy(values, 0, nv, 0, size);
                keys = nk;
                values = nv;
            }
            keys[size] = key;
            values[size] = value;
            size++;
        }

        void remove(int key) {
            for (int i = 0; i < size; i++) {
                if (keys[i] == key) {
                    System.arraycopy(keys, i + 1, keys, i, size - i - 1);
                    System.arraycopy(values, i + 1, values, i, size - i - 1);
                    size--;
                    return;
                }
            }
        }

        void clear() {
            size = 0;
        }

        int keyAt(int i) {
            return keys[i];
        }

        int size() {
            return size;
        }
    }

    public VirtualPadView(Context context) {
        super(context);
        mSettings = PadSettings.get(context);
        mHidden = mSettings.hidden();
        // The pad is a pure overlay: never steals focus, never shows keys.
        setFocusable(false);
        setClickable(true);
        setImportantForAccessibility(IMPORTANT_FOR_ACCESSIBILITY_NO);
    }

    /**
     * Installs the overlay on top of the SDL surface. Called by MainActivity
     * (an SDLActivity subclass, hence able to reach the layout) right after
     * super.onCreate, when the SDL layout exists but before the game window
     * starts rendering. Returns the installed view (for pause-time resets)
     * or null when the layout was unavailable.
     */
    public static VirtualPadView install(Activity activity) {
        android.view.ViewGroup layout;
        try {
            layout = (android.view.ViewGroup) org.libsdl.app.SDLActivity.getContentView();
        } catch (Throwable t) {
            Log.w(TAG, "SDL layout unavailable - virtual gamepad disabled", t);
            return null;
        }
        if (layout == null) {
            Log.w(TAG, "SDL layout is null - virtual gamepad disabled");
            return null;
        }
        android.view.ViewGroup.LayoutParams lp = new android.view.ViewGroup.LayoutParams(
                android.view.ViewGroup.LayoutParams.MATCH_PARENT,
                android.view.ViewGroup.LayoutParams.MATCH_PARENT);
        VirtualPadView view = new VirtualPadView(activity);
        layout.addView(view, lp);
        Log.i(TAG, "virtual gamepad overlay installed");
        return view;
    }

    // -------------------------------------------------------------------------
    // Layout / lifecycle
    // -------------------------------------------------------------------------

    @Override
    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        readInsets();
        rebuild();
    }

    @Override
    protected void onSizeChanged(int w, int h, int oldw, int oldh) {
        super.onSizeChanged(w, h, oldw, oldh);
        readInsets();
        rebuild();
    }

    private void readInsets() {
        mInset[0] = mInset[1] = mInset[2] = mInset[3] = 0;
        WindowInsets insets = getRootWindowInsets();
        DisplayCutout cutout = insets == null ? null : insets.getDisplayCutout();
        if (cutout != null) {
            mInset[0] = cutout.getSafeInsetLeft();
            mInset[1] = cutout.getSafeInsetTop();
            mInset[2] = cutout.getSafeInsetRight();
            mInset[3] = cutout.getSafeInsetBottom();
        }
    }

    /** Rebuilds geometry from current size + settings and clears input. */
    private void rebuild() {
        final int w = getWidth();
        final int h = getHeight();
        if (w <= 0 || h <= 0) {
            return;
        }
        releaseAll();
        mControls = PadGeometry.build(w, h,
                mInset[0], mInset[1], mInset[2], mInset[3], mSettings.scale());
        mTab = PadGeometry.buildTab(w, h,
                mInset[0], mInset[1], mInset[2], mInset[3], mSettings.scale());
        invalidate();
    }

    /** Applies changed settings (from the dialog) and re-renders. */
    void onSettingsChanged() {
        mSettings = PadSettings.get(getContext());
        mHidden = mSettings.hidden();
        rebuild();
    }

    /** Releases everything (sends all-up); used by the host on pause. */
    public void onHostPause() {
        releaseAll();
        invalidate();
    }

    // -------------------------------------------------------------------------
    // Touch handling
    // -------------------------------------------------------------------------

    @Override
    public boolean onTouchEvent(MotionEvent ev) {
        final int action = ev.getActionMasked();
        switch (action) {
            case MotionEvent.ACTION_DOWN:
            case MotionEvent.ACTION_POINTER_DOWN: {
                final int idx = ev.getActionIndex();
                final int pid = ev.getPointerId(idx);
                return onPointerDown(pid, ev.getX(idx), ev.getY(idx));
            }
            case MotionEvent.ACTION_MOVE: {
                final int count = ev.getPointerCount();
                for (int i = 0; i < count; i++) {
                    final int pid = ev.getPointerId(i);
                    Pointer p = mPointers.get(pid);
                    if (p != null && p.control >= 0) {
                        final float x = ev.getX(i);
                        final float y = ev.getY(i);
                        if (x != p.x || y != p.y) {
                            p.x = x;
                            p.y = y;
                            updateControl(p);
                        }
                    }
                }
                return true;
            }
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_POINTER_UP: {
                final int idx = ev.getActionIndex();
                releasePointer(ev.getPointerId(idx));
                return true;
            }
            case MotionEvent.ACTION_CANCEL: {
                releaseAll();
                return true;
            }
            default:
                return super.onTouchEvent(ev);
        }
    }

    private boolean onPointerDown(int pid, float x, float y) {
        // The overlay CONSUMES every touch of a gesture it sees. Rationale:
        // the game reads a gamepad only (touch on the SDL surface drives
        // nothing - mnk is off), while a pass-through policy would break
        // multi-finger input (a finger resting on empty space would claim
        // the gesture stream and every later finger on a real control would
        // be lost). Touches on empty space are simply ignored here.
        if (mHidden) {
            if (mTab != null && PadGeometry.hitContains(mTab, x, y)) {
                mSettings.setHidden(false);
                mSettings = PadSettings.get(getContext());
                mHidden = false;
                rebuild();
                performHaptic();
            }
            return true;
        }

        final int control = hitTest(x, y);
        // A repeated POINTER_DOWN for a live pid replaces its record; recycle
        // the old one so the pool stays bounded.
        Pointer previous = mPointers.get(pid);
        Pointer p = obtainPointer();
        p.control = control;
        p.x = x;
        p.y = y;
        mPointers.put(pid, p);
        if (previous != null) {
            recyclePointer(previous);
        }

        if (control >= 0) {
            mPressed[control] = true;
            mPressAt[control] = SystemClock.uptimeMillis();
            performHaptic();
            updateControl(p);
            startTick();
        }
        return true;
    }

    private Pointer obtainPointer() {
        if (mPoolSize > 0) {
            return mPool[--mPoolSize];
        }
        return new Pointer();
    }

    private void recyclePointer(Pointer p) {
        if (mPoolSize < mPool.length) {
            mPool[mPoolSize++] = p;
        }
    }

    /**
     * Delegates to the shared nearest-wins rule (see PadGeometry.hitResolve).
     */
    private int hitTest(float x, float y) {
        return PadGeometry.hitResolve(mControls, x, y);
    }

    /** Dispatches a captured pointer's current position to its control. */
    private void updateControl(Pointer p) {
        final PadGeometry.Control c = mControls[p.control];
        switch (c.type) {
            case PadGeometry.TYPE_STICK:
                updateStick(p.control, c, p.x, p.y);
                break;
            case PadGeometry.TYPE_DPAD:
                updateDpad(c, p.x, p.y);
                break;
            case PadGeometry.TYPE_PILL:
                updatePill(p.control, c);
                break;
            case PadGeometry.TYPE_BUTTON:
                pressButtonOnce(p.control);
                break;
            default:
                break;
        }
        invalidate();
    }

    private void updateStick(int control, PadGeometry.Control c, float x, float y) {
        final float dx = x - c.cx;
        final float dy = y - c.cy;
        final float len = (float) Math.sqrt(dx * dx + dy * dy);
        final float radius = c.radius;
        float nx = 0f;
        float ny = 0f;
        if (len > 0.0001f) {
            final float raw = Math.min(1f, len / radius);
            final float out = Math.max(0f, (raw - STICK_DEADZONE) / (1f - STICK_DEADZONE));
            nx = dx / len * out;
            ny = dy / len * out;
        }
        final int slot = control == PadGeometry.ID_STICK_L
                ? PadInputBridge.STICK_LEFT : PadInputBridge.STICK_RIGHT;
        mStick[slot * 2] = nx;
        mStick[slot * 2 + 1] = ny;
        mCapX[slot] = dx / Math.max(1f, len) * Math.min(len, radius) * 0.42f;
        mCapY[slot] = dy / Math.max(1f, len) * Math.min(len, radius) * 0.42f;
        PadInputBridge.setStick(slot, nx, ny);
    }

    private void updateDpad(PadGeometry.Control c, float x, float y) {
        final float dx = x - c.cx;
        final float dy = y - c.cy;
        final float len = (float) Math.sqrt(dx * dx + dy * dy);
        int mask = 0;
        if (len >= DPAD_ENGAGE * c.radius) {
            final double angle = Math.toDegrees(Math.atan2(dy, dx));
            mask = sectorMask(angle);
            if (mask != mDpadMask && mDpadMask != 0) {
                // Hysteresis: ignore sector changes closer than DPAD_HYST_DEG
                // to the boundary between the current and the candidate
                // sector, so touch jitter on a boundary doesn't flip buttons.
                final float boundary = boundaryBetween(mDpadMask, mask);
                if (!Float.isNaN(boundary)
                        && Math.abs(angleDiff(angle, boundary)) < DPAD_HYST_DEG) {
                    mask = mDpadMask;
                }
            }
        }
        setDpadMask(mask);
    }

    /** Sector (8-way bitmask) for a touch angle in degrees. */
    private static int sectorMask(double angle) {
        if (angle > -22.5 && angle <= 22.5) {
            return DP_RIGHT;
        }
        if (angle > 22.5 && angle <= 67.5) {
            return DP_RIGHT | DP_DOWN;
        }
        if (angle > 67.5 && angle <= 112.5) {
            return DP_DOWN;
        }
        if (angle > 112.5 && angle <= 157.5) {
            return DP_DOWN | DP_LEFT;
        }
        if (angle > 157.5 || angle <= -157.5) {
            return DP_LEFT;
        }
        if (angle > -157.5 && angle <= -112.5) {
            return DP_LEFT | DP_UP;
        }
        if (angle > -112.5 && angle <= -67.5) {
            return DP_UP;
        }
        return DP_UP | DP_RIGHT;
    }

    /** Center angle (degrees) of a direction bitmask's sector. */
    private static double sectorCenter(int mask) {
        switch (mask) {
            case DP_UP: return -90;
            case DP_UP | DP_RIGHT: return -45;
            case DP_RIGHT: return 0;
            case DP_RIGHT | DP_DOWN: return 45;
            case DP_DOWN: return 90;
            case DP_DOWN | DP_LEFT: return 135;
            case DP_LEFT: return 180;
            case DP_LEFT | DP_UP: return -135;
            default: return Double.NaN;
        }
    }

    /** Boundary angle between two sectors, or NaN when not adjacent. */
    private static float boundaryBetween(int a, int b) {
        final double ca = sectorCenter(a);
        final double cb = sectorCenter(b);
        if (Double.isNaN(ca) || Double.isNaN(cb)) {
            return Float.NaN;
        }
        // Circular midpoint (handles the 180 / -180 wrap).
        final double radA = Math.toRadians(ca);
        final double radB = Math.toRadians(cb);
        final double mid = Math.atan2(
                Math.sin(radA) + Math.sin(radB),
                Math.cos(radA) + Math.cos(radB));
        return (float) Math.toDegrees(mid);
    }

    /** Signed difference a-b wrapped to [-180, 180). */
    private static float angleDiff(double a, double b) {
        float d = (float) (a - b);
        while (d >= 180f) {
            d -= 360f;
        }
        while (d < -180f) {
            d += 360f;
        }
        return d;
    }

    private void setDpadMask(int mask) {
        if (mask == mDpadMask) {
            return;
        }
        final int became = mask & ~mDpadMask;
        final int gone = mDpadMask & ~mask;
        mDpadMask = mask;
        if ((became & DP_UP) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_UP, true);
        }
        if ((became & DP_DOWN) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_DOWN, true);
        }
        if ((became & DP_LEFT) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_LEFT, true);
        }
        if ((became & DP_RIGHT) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_RIGHT, true);
        }
        if ((gone & DP_UP) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_UP, false);
        }
        if ((gone & DP_DOWN) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_DOWN, false);
        }
        if ((gone & DP_LEFT) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_LEFT, false);
        }
        if ((gone & DP_RIGHT) != 0) {
            PadInputBridge.setButton(PadInputBridge.BTN_DPAD_RIGHT, false);
        }
        if (became != 0) {
            performHaptic();
        }
    }

    /** Bumpers press like buttons; triggers start their analog ramp. */
    private void updatePill(int control, PadGeometry.Control c) {
        if (control == PadGeometry.ID_LB) {
            pressButtonOnce(control, PadInputBridge.BTN_LB);
        } else if (control == PadGeometry.ID_RB) {
            pressButtonOnce(control, PadInputBridge.BTN_RB);
        } else if (control == PadGeometry.ID_LT) {
            startTrigger(PadInputBridge.TRIGGER_LEFT);
        } else if (control == PadGeometry.ID_RT) {
            startTrigger(PadInputBridge.TRIGGER_RIGHT);
        }
    }

    private void pressButtonOnce(int control) {
        final int sdlButton;
        switch (control) {
            case PadGeometry.ID_A: sdlButton = PadInputBridge.BTN_A; break;
            case PadGeometry.ID_B: sdlButton = PadInputBridge.BTN_B; break;
            case PadGeometry.ID_X: sdlButton = PadInputBridge.BTN_X; break;
            case PadGeometry.ID_Y: sdlButton = PadInputBridge.BTN_Y; break;
            case PadGeometry.ID_BACK: sdlButton = PadInputBridge.BTN_BACK; break;
            case PadGeometry.ID_START: sdlButton = PadInputBridge.BTN_START; break;
            default: return;
        }
        pressButtonOnce(control, sdlButton);
    }

    private void pressButtonOnce(int control, int sdlButton) {
        if (!mDownSent[control]) {
            mDownSent[control] = true;
            PadInputBridge.setButton(sdlButton, true);
            performHaptic();
        }
    }

    private void startTrigger(int trigger) {
        if (mTriggerAt[trigger] == 0) {
            mTriggerAt[trigger] = SystemClock.uptimeMillis();
        }
    }

    /** Advances trigger ramps; returns true while any ramp is running. */
    private boolean stepTriggers() {
        final long now = SystemClock.uptimeMillis();
        boolean running = false;
        for (int t = 0; t < 2; t++) {
            if (mTriggerAt[t] != 0) {
                final long elapsed = now - mTriggerAt[t];
                final float v = elapsed >= TRIGGER_RAMP_MS
                        ? 1f
                        : (float) elapsed / TRIGGER_RAMP_MS;
                if (v != mTrigger[t]) {
                    mTrigger[t] = v;
                    PadInputBridge.setTrigger(t, v);
                }
                if (v < 1f) {
                    running = true;
                }
            }
        }
        return running;
    }

    /** Advances the press/release ease; returns true while anything animates. */
    private boolean stepEases() {
        final long now = SystemClock.uptimeMillis();
        boolean running = false;
        for (int i = 0; i < mEase.length; i++) {
            final long anchor = mPressAt[i];
            if (anchor == 0) {
                continue;
            }
            final float t = (now - anchor) / (float) PRESS_ANIM_MS;
            final float target = mPressed[i] ? 1f : 0f;
            float e = mPressed[i]
                    ? Math.min(1f, t)
                    : Math.max(0f, 1f - t);
            e = e * (2f - e); // ease-out
            if (e != mEase[i]) {
                mEase[i] = e;
            }
            if (t < 1f) {
                running = true;
            }
        }
        return running;
    }

    private void tickAnimations() {
        final boolean more = stepEases() | stepTriggers();
        invalidate();
        if (more) {
            postOnAnimation(mTick);
        } else {
            mTicking = false;
        }
    }

    private void startTick() {
        if (!mTicking) {
            mTicking = true;
            postOnAnimation(mTick);
        }
    }

    private void releasePointer(int pid) {
        Pointer p = mPointers.get(pid);
        if (p == null) {
            return;
        }
        mPointers.remove(pid);
        if (p.control < 0) {
            recyclePointer(p);
            return;
        }
        if (p.control == PadGeometry.ID_MENU) {
            // The gear acts on RELEASE inside its zone: an accidental brush
            // that slides away never opens the dialog mid-combat.
            mPressed[p.control] = false;
            mPressAt[p.control] = SystemClock.uptimeMillis();
            startTick();
            if (mControls[p.control] != null
                    && PadGeometry.hitContains(mControls[p.control], p.x, p.y)) {
                openSettingsDialog();
            }
            recyclePointer(p);
            return;
        }
        // Another finger may still hold the same control (two-thumb stick
        // handover): only release when the LAST holder lifts.
        boolean stillHeld = false;
        for (int i = 0; i < mPointers.size(); i++) {
            Pointer other = mPointers.get(mPointers.keyAt(i));
            if (other != null && other.control == p.control) {
                stillHeld = true;
                break;
            }
        }
        if (!stillHeld) {
            releaseControl(p.control);
        }
        recyclePointer(p);
    }

    private void releaseControl(int control) {
        if (control < 0 || control >= mPressed.length) {
            return;
        }
        if (control == PadGeometry.ID_MENU) {
            // Visual-only control (opens the dialog); just end the animation.
            mPressed[control] = false;
            mPressAt[control] = SystemClock.uptimeMillis();
            startTick();
            return;
        }
        if (!mPressed[control] && !mDownSent[control]) {
            return;
        }
        mPressed[control] = false;
        mDownSent[control] = false;
        mPressAt[control] = SystemClock.uptimeMillis();
        final PadGeometry.Control c = mControls[control];
        if (c != null) {
            switch (control) {
                case PadGeometry.ID_STICK_L:
                    zeroStick(PadInputBridge.STICK_LEFT);
                    break;
                case PadGeometry.ID_STICK_R:
                    zeroStick(PadInputBridge.STICK_RIGHT);
                    break;
                case PadGeometry.ID_DPAD:
                    setDpadMask(0);
                    break;
                case PadGeometry.ID_LT:
                    stopTrigger(PadInputBridge.TRIGGER_LEFT);
                    break;
                case PadGeometry.ID_RT:
                    stopTrigger(PadInputBridge.TRIGGER_RIGHT);
                    break;
                case PadGeometry.ID_LB:
                    PadInputBridge.setButton(PadInputBridge.BTN_LB, false);
                    break;
                case PadGeometry.ID_RB:
                    PadInputBridge.setButton(PadInputBridge.BTN_RB, false);
                    break;
                case PadGeometry.ID_A:
                    PadInputBridge.setButton(PadInputBridge.BTN_A, false);
                    break;
                case PadGeometry.ID_B:
                    PadInputBridge.setButton(PadInputBridge.BTN_B, false);
                    break;
                case PadGeometry.ID_X:
                    PadInputBridge.setButton(PadInputBridge.BTN_X, false);
                    break;
                case PadGeometry.ID_Y:
                    PadInputBridge.setButton(PadInputBridge.BTN_Y, false);
                    break;
                case PadGeometry.ID_BACK:
                    PadInputBridge.setButton(PadInputBridge.BTN_BACK, false);
                    break;
                case PadGeometry.ID_START:
                    PadInputBridge.setButton(PadInputBridge.BTN_START, false);
                    break;
                default:
                    break; // MENU: visual only
            }
        }
        startTick();
    }

    private void zeroStick(int slot) {
        mStick[slot * 2] = 0f;
        mStick[slot * 2 + 1] = 0f;
        mCapX[slot] = 0f;
        mCapY[slot] = 0f;
        PadInputBridge.setStick(slot, 0f, 0f);
    }

    private void stopTrigger(int trigger) {
        mTriggerAt[trigger] = 0;
        if (mTrigger[trigger] != 0f) {
            mTrigger[trigger] = 0f;
            PadInputBridge.setTrigger(trigger, 0f);
        }
    }

    /**
     * Releases every pointer AND sends the matching all-up stream to the
     * virtual pad, so no button/d-pad direction/trigger survives a pause,
     * cancel or layout rebuild as logically stuck-down on the native side.
     */
    private void releaseAll() {
        for (int i = 0; i < mPressed.length; i++) {
            if (mPressed[i] || mDownSent[i]) {
                releaseControl(i);
            }
        }
        for (int i = 0; i < mPointers.size(); i++) {
            recyclePointer(mPointers.get(mPointers.keyAt(i)));
        }
        mPointers.clear();
        mTriggerAt[0] = mTriggerAt[1] = 0;
        zeroStick(PadInputBridge.STICK_LEFT);
        zeroStick(PadInputBridge.STICK_RIGHT);
        for (int t = 0; t < 2; t++) {
            if (mTrigger[t] != 0f) {
                mTrigger[t] = 0f;
                PadInputBridge.setTrigger(t, 0f);
            }
        }
    }

    private void performHaptic() {
        if (mSettings.haptics()) {
            performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY);
        }
    }

    private void openSettingsDialog() {
        final Context context = getContext();
        if (context instanceof Activity) {
            // onSettingsChanged re-reads the store AND syncs mHidden, so the
            // "hide overlay" action collapses the pad in-session (not only
            // after an app restart).
            PadSettingsDialog.show((Activity) context, mSettings,
                    this::onSettingsChanged);
        }
    }

    // -------------------------------------------------------------------------
    // Rendering (vector-only "glass" style, zero per-frame allocations)
    // -------------------------------------------------------------------------

    @Override
    protected void onDraw(Canvas canvas) {
        if (mHidden) {
            if (mTab != null) {
                drawTab(canvas, mTab);
            }
            return;
        }
        for (PadGeometry.Control c : mControls) {
            if (c == null) {
                continue;
            }
            switch (c.type) {
                case PadGeometry.TYPE_PILL:
                    drawPill(canvas, c);
                    break;
                case PadGeometry.TYPE_DPAD:
                    drawDpad(canvas, c);
                    break;
                case PadGeometry.TYPE_STICK:
                    drawStick(canvas, c);
                    break;
                case PadGeometry.TYPE_BUTTON:
                    drawButton(canvas, c);
                    break;
                default:
                    break;
            }
        }
    }

    /** Scales the canvas around a control's center by its press animation. */
    private void pressScale(Canvas canvas, PadGeometry.Control c, float ease) {
        final float s = 1f - (1f - PRESS_SCALE) * ease;
        if (s != 1f) {
            canvas.translate(c.cx, c.cy);
            canvas.scale(s, s);
            canvas.translate(-c.cx, -c.cy);
        }
    }

    private int glassFill(boolean pressed) {
        return PadTheme.withOpacity(
                pressed ? PadTheme.GLASS_FILL_PRESSED : PadTheme.GLASS_FILL,
                mSettings.opacity());
    }

    private int hairline(boolean pressed) {
        return PadTheme.withOpacity(
                pressed ? PadTheme.STROKE_PRESSED : PadTheme.STROKE,
                mSettings.opacity());
    }

    private void drawShadowCircle(Canvas canvas, float cx, float cy, float r) {
        mTheme.shadow.setColor(PadTheme.withOpacity(PadTheme.SHADOW, mSettings.opacity()));
        canvas.drawCircle(cx, cy + r * 0.07f, r * 1.01f, mTheme.shadow);
    }

    private void drawShadowRoundRect(Canvas canvas, float l, float t, float r, float b) {
        mTheme.shadow.setColor(PadTheme.withOpacity(PadTheme.SHADOW, mSettings.opacity()));
        mTheme.rect.set(l, t + (b - t) * 0.09f, r, b + (b - t) * 0.09f);
        canvas.drawRoundRect(mTheme.rect, (b - t) / 2f, (b - t) / 2f, mTheme.shadow);
    }

    private void drawStick(Canvas canvas, PadGeometry.Control c) {
        final float op = mSettings.opacity();
        final float e = mEase[c.id];
        final float r = c.radius;
        final boolean pressed = mPressed[c.id];
        final int slot = c.id == PadGeometry.ID_STICK_L
                ? PadInputBridge.STICK_LEFT : PadInputBridge.STICK_RIGHT;
        final float capCx = c.cx + mCapX[slot] * (1f - 0.12f * e);
        final float capCy = c.cy + mCapY[slot] * (1f - 0.12f * e);

        canvas.save();
        pressScale(canvas, c, e);
        drawShadowCircle(canvas, c.cx, c.cy, r);

        mTheme.fill.setColor(glassFill(pressed));
        canvas.drawCircle(c.cx, c.cy, r, mTheme.fill);

        mTheme.stroke.setStrokeWidth(Math.max(2f, r * 0.05f));
        mTheme.stroke.setColor(hairline(pressed));
        canvas.drawCircle(c.cx, c.cy, r - mTheme.stroke.getStrokeWidth() / 2f,
                mTheme.stroke);

        // Cap (follows the deflection vector).
        final float capR = r * 0.55f;
        mTheme.fill.setColor(PadTheme.withOpacity(PadTheme.CAP_FILL, op));
        canvas.drawCircle(capCx, capCy, capR, mTheme.fill);
        mTheme.stroke.setStrokeWidth(Math.max(1.5f, r * 0.035f));
        mTheme.stroke.setColor(PadTheme.withOpacity(
                pressed ? PadTheme.STROKE_PRESSED : PadTheme.CAP_EDGE, op));
        canvas.drawCircle(capCx, capCy, capR - mTheme.stroke.getStrokeWidth() / 2f,
                mTheme.stroke);
        canvas.restore();
    }

    private void drawDpad(Canvas canvas, PadGeometry.Control c) {
        final float op = mSettings.opacity();
        final float e = mEase[c.id];
        final float r = c.radius;
        final float armW = r * 0.46f;
        final float corner = armW * 0.30f;
        final int active = mDpadMask;
        final int accent = PadTheme.accentOf(c.id);

        canvas.save();
        pressScale(canvas, c, e);
        drawShadowCircle(canvas, c.cx, c.cy, r);

        // Four arms (drawn from the hub outwards).
        drawArm(canvas, c.cx, c.cy - r * 0.55f, armW, r * 1.10f, corner, true,
                (active & DP_UP) != 0, accent, op);
        drawArm(canvas, c.cx, c.cy + r * 0.55f, armW, r * 1.10f, corner, true,
                (active & DP_DOWN) != 0, accent, op);
        drawArm(canvas, c.cx - r * 0.55f, c.cy, r * 1.10f, armW, corner, false,
                (active & DP_LEFT) != 0, accent, op);
        drawArm(canvas, c.cx + r * 0.55f, c.cy, r * 1.10f, armW, corner, false,
                (active & DP_RIGHT) != 0, accent, op);

        // Hub.
        mTheme.fill.setColor(glassFill(false));
        canvas.drawCircle(c.cx, c.cy, armW * 0.62f, mTheme.fill);
        mTheme.stroke.setStrokeWidth(Math.max(1f, r * 0.03f));
        mTheme.stroke.setColor(hairline(false));
        canvas.drawCircle(c.cx, c.cy, armW * 0.62f, mTheme.stroke);

        // Direction arrows (flip mirrors the triangle for down/right).
        drawArrow(canvas, c.cx, c.cy - r * 0.72f, armW, true, false,
                (active & DP_UP) != 0, accent, op);
        drawArrow(canvas, c.cx, c.cy + r * 0.72f, armW, true, true,
                (active & DP_DOWN) != 0, accent, op);
        drawArrow(canvas, c.cx - r * 0.72f, c.cy, armW, false, false,
                (active & DP_LEFT) != 0, accent, op);
        drawArrow(canvas, c.cx + r * 0.72f, c.cy, armW, false, true,
                (active & DP_RIGHT) != 0, accent, op);
        // (arrow size/contrast: see drawArrow - enlarged per UI/UX review)
        canvas.restore();
    }

    /** One d-pad arm; vertical arms are drawn along Y, horizontal along X. */
    private void drawArm(Canvas canvas, float cx, float cy, float w, float h,
                         float corner, boolean vertical, boolean active,
                         int accent, float op) {
        final float left = vertical ? cx - w / 2f : cx - h / 2f;
        final float top = vertical ? cy - h / 2f : cy - w / 2f;
        mTheme.rect.set(left, top, left + (vertical ? w : h),
                top + (vertical ? h : w));
        mTheme.fill.setColor(glassFill(active));
        canvas.drawRoundRect(mTheme.rect, corner, corner, mTheme.fill);
        mTheme.stroke.setStrokeWidth(Math.max(1f, Math.min(w, h) * 0.08f));
        mTheme.stroke.setColor(active
                ? PadTheme.withOpacity(PadTheme.STROKE_PRESSED, op)
                : PadTheme.withOpacity(accent, op * 0.55f));
        canvas.drawRoundRect(mTheme.rect, corner, corner, mTheme.stroke);
    }

    private void drawArrow(Canvas canvas, float cx, float cy, float armW,
                           boolean vertical, boolean flip, boolean active,
                           int accent, float op) {
        final float half = armW * 0.30f;
        final float len = armW * 0.45f;
        final float tipA = flip ? len : -len;        // apex offset
        final float baseA = flip ? -len * 0.6f : len * 0.6f;  // base offset
        mTheme.path.rewind();
        if (vertical) {
            mTheme.path.moveTo(cx, cy + tipA);
            mTheme.path.lineTo(cx - half, cy + baseA);
            mTheme.path.lineTo(cx + half, cy + baseA);
        } else {
            mTheme.path.moveTo(cx + tipA, cy);
            mTheme.path.lineTo(cx + baseA, cy - half);
            mTheme.path.lineTo(cx + baseA, cy + half);
        }
        mTheme.path.close();
        mTheme.glyph.setColor(PadTheme.withOpacity(active ? PadTheme.TEXT : accent,
                op * (active ? 1f : 0.9f)));
        canvas.drawPath(mTheme.path, mTheme.glyph);
    }

    private void drawButton(Canvas canvas, PadGeometry.Control c) {
        final float op = mSettings.opacity();
        final float e = mEase[c.id];
        final float r = c.radius;
        final boolean pressed = mPressed[c.id];
        final int accent = PadTheme.accentOf(c.id);

        canvas.save();
        pressScale(canvas, c, e);
        drawShadowCircle(canvas, c.cx, c.cy, r);

        if (pressed) {
            mTheme.fill.setColor(PadTheme.withOpacity(PadTheme.GLOW, op));
            canvas.drawCircle(c.cx, c.cy, r, mTheme.fill);
        }
        mTheme.fill.setColor(glassFill(pressed));
        canvas.drawCircle(c.cx, c.cy, r, mTheme.fill);

        mTheme.stroke.setStrokeWidth(Math.max(1.5f, r * 0.09f));
        mTheme.stroke.setColor(PadTheme.withOpacity(accent,
                pressed ? op : op * 0.72f));
        canvas.drawCircle(c.cx, c.cy, r - mTheme.stroke.getStrokeWidth() / 2f,
                mTheme.stroke);

        switch (c.id) {
            case PadGeometry.ID_A:
            case PadGeometry.ID_B:
            case PadGeometry.ID_X:
            case PadGeometry.ID_Y:
                drawCenteredText(canvas, PadTheme.labelOf(c.id), c.cx, c.cy,
                        r * 1.02f, PadTheme.withOpacity(accent, op));
                break;
            case PadGeometry.ID_BACK:
                drawChevrons(canvas, c.cx, c.cy, r, false, accent, op);
                break;
            case PadGeometry.ID_START:
                drawChevrons(canvas, c.cx, c.cy, r, true, accent, op);
                break;
            case PadGeometry.ID_MENU:
                drawGear(canvas, c.cx, c.cy, r * 0.78f,
                        PadTheme.withOpacity(PadTheme.ACCENT_NEUTRAL, op));
                break;
            default:
                break;
        }
        canvas.restore();
    }

    private void drawPill(Canvas canvas, PadGeometry.Control c) {
        final float op = mSettings.opacity();
        final float e = mEase[c.id];
        final float hw = c.halfW;
        final float hh = c.halfH;
        final boolean pressed = mPressed[c.id];
        final int accent = PadTheme.accentOf(c.id);

        canvas.save();
        pressScale(canvas, c, e);
        drawShadowRoundRect(canvas, c.cx - hw, c.cy - hh, c.cx + hw, c.cy + hh);

        if (pressed) {
            mTheme.fill.setColor(PadTheme.withOpacity(PadTheme.GLOW, op));
            mTheme.rect.set(c.cx - hw, c.cy - hh, c.cx + hw, c.cy + hh);
            canvas.drawRoundRect(mTheme.rect, hh, hh, mTheme.fill);
        }
        mTheme.fill.setColor(glassFill(pressed));
        mTheme.rect.set(c.cx - hw, c.cy - hh, c.cx + hw, c.cy + hh);
        canvas.drawRoundRect(mTheme.rect, hh, hh, mTheme.fill);

        mTheme.stroke.setStrokeWidth(Math.max(1.5f, hh * 0.14f));
        mTheme.stroke.setColor(hairline(pressed));
        canvas.drawRoundRect(mTheme.rect, hh, hh, mTheme.stroke);

        // Analog triggers show their value as a fill bar.
        if (c.id == PadGeometry.ID_LT || c.id == PadGeometry.ID_RT) {
            final float value = c.id == PadGeometry.ID_LT
                    ? mTrigger[PadInputBridge.TRIGGER_LEFT]
                    : mTrigger[PadInputBridge.TRIGGER_RIGHT];
            if (value > 0f) {
                final float gap = hh * 0.30f;
                final float fullW = (hw - gap) * 2f;
                mTheme.rect.set(c.cx - hw + gap, c.cy + hh - gap * 2.2f,
                        c.cx - hw + gap + fullW * value, c.cy + hh - gap);
                mTheme.glyph.setColor(PadTheme.withOpacity(accent, op * 0.85f));
                canvas.drawRoundRect(mTheme.rect, gap * 0.8f, gap * 0.8f, mTheme.glyph);
            }
        }

        drawCenteredText(canvas, PadTheme.labelOf(c.id), c.cx, c.cy,
                hh * 0.92f, PadTheme.withOpacity(PadTheme.TEXT, op));
        canvas.restore();
    }

    private void drawTab(Canvas canvas, PadGeometry.Control t) {
        final float op = mSettings.opacity();
        mTheme.fill.setColor(PadTheme.withOpacity(PadTheme.GLASS_FILL, op * 0.55f));
        mTheme.rect.set(t.cx - t.halfW, t.cy - t.halfH, t.cx + t.halfW,
                t.cy + t.halfH);
        canvas.drawRoundRect(mTheme.rect, t.halfW, t.halfW, mTheme.fill);
        mTheme.stroke.setStrokeWidth(1.5f);
        mTheme.stroke.setColor(PadTheme.withOpacity(PadTheme.CAP_EDGE, op * 0.6f));
        canvas.drawRoundRect(mTheme.rect, t.halfW, t.halfW, mTheme.stroke);
        // Grip dots.
        mTheme.glyph.setColor(PadTheme.withOpacity(PadTheme.CAP_EDGE, op * 0.8f));
        final float dot = t.halfW * 0.28f;
        for (int i = -1; i <= 1; i++) {
            canvas.drawCircle(t.cx, t.cy + i * t.halfH * 0.28f, dot, mTheme.glyph);
        }
    }

    /** Double chevrons: right-pointing for Start, left-pointing for Back. */
    private void drawChevrons(Canvas canvas, float cx, float cy, float r,
                              boolean right, int accent, float op) {
        mTheme.stroke.setStrokeWidth(r * 0.16f);
        mTheme.stroke.setStrokeCap(Paint.Cap.ROUND);
        mTheme.stroke.setStrokeJoin(Paint.Join.ROUND);
        mTheme.stroke.setColor(PadTheme.withOpacity(accent, op * 0.9f));
        final float dx = r * 0.34f;
        final float h = r * 0.44f;
        for (int i = 0; i < 2; i++) {
            final float off = (i == 0 ? -dx * 0.5f : dx * 0.6f) * (right ? 1 : -1);
            mTheme.path.rewind();
            mTheme.path.moveTo(cx + off + (right ? -dx * 0.45f : dx * 0.45f), cy - h);
            mTheme.path.lineTo(cx + off - (right ? -dx * 0.45f : dx * 0.45f), cy);
            mTheme.path.lineTo(cx + off + (right ? -dx * 0.45f : dx * 0.45f), cy + h);
            canvas.drawPath(mTheme.path, mTheme.stroke);
        }
    }

    /** Six-tooth gear glyph for the settings control. */
    private void drawGear(Canvas canvas, float cx, float cy, float r, int color) {
        canvas.save();
        canvas.translate(cx, cy);
        mTheme.stroke.setStrokeWidth(r * 0.30f);
        mTheme.stroke.setStrokeCap(Paint.Cap.ROUND);
        mTheme.stroke.setColor(color);
        for (int i = 0; i < 6; i++) {
            canvas.save();
            canvas.rotate(i * 60f);
            canvas.drawLine(0f, -r * 0.52f, 0f, -r * 0.86f, mTheme.stroke);
            canvas.restore();
        }
        mTheme.stroke.setStrokeWidth(r * 0.22f);
        canvas.drawCircle(0f, 0f, r * 0.55f, mTheme.stroke);
        canvas.restore();
    }

    private void drawCenteredText(Canvas canvas, String s, float cx, float cy,
                                  float sizePx, int color) {
        if (s == null || s.isEmpty()) {
            return;
        }
        mTheme.text.setTextSize(sizePx);
        mTheme.text.setColor(color);
        // Two-arg getFontMetrics fills the pre-allocated object: zero alloc
        // per label per frame.
        mTheme.text.getFontMetrics(mTheme.textMetrics);
        final float baseline = cy - (mTheme.textMetrics.ascent
                + mTheme.textMetrics.descent) / 2f;
        canvas.drawText(s, cx, baseline, mTheme.text);
    }

    @Override
    public boolean performClick() {
        // Overlay controls handle touches themselves; keep a11y happy.
        return true;
    }
}
