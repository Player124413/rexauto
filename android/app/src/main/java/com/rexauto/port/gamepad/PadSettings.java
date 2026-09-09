package com.rexauto.port.gamepad;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * Persistent settings for the on-screen virtual gamepad.
 *
 * Backed by its own SharedPreferences file ("pad_settings") so the game
 * settings file (used by SetupActivity) stays untouched. Values are clamped
 * to their documented ranges on load - a corrupted or hand-edited store can
 * never push the overlay off-screen or make it untouchable.
 */
public final class PadSettings {

    private static final String PREFS_FILE = "pad_settings";
    private static final String KEY_ENABLED = "enabled";
    private static final String KEY_HIDDEN = "hidden";
    private static final String KEY_SCALE = "scale";
    private static final String KEY_OPACITY = "opacity";
    private static final String KEY_HAPTICS = "haptics";

    /** Overlay master switch. */
    public static final boolean ENABLED_DEFAULT = true;
    /** Collapsed to the restore tab (per-session convenience, persisted). */
    public static final boolean HIDDEN_DEFAULT = false;
    /** Geometry multiplier applied to every control. */
    public static final float SCALE_MIN = 0.75f;
    public static final float SCALE_MAX = 1.20f;
    public static final float SCALE_DEFAULT = 1.00f;
    /** Global alpha multiplier on top of the theme's own per-element alphas. */
    public static final float OPACITY_MIN = 0.35f;
    public static final float OPACITY_MAX = 1.00f;
    public static final float OPACITY_DEFAULT = 0.80f;
    /** Haptic tick on button/stick activation. */
    public static final boolean HAPTICS_DEFAULT = true;

    private static final Object sLock = new Object();
    private static SharedPreferences sPrefs;

    private boolean enabled = ENABLED_DEFAULT;
    private boolean hidden = HIDDEN_DEFAULT;
    private float scale = SCALE_DEFAULT;
    private float opacity = OPACITY_DEFAULT;
    private boolean haptics = HAPTICS_DEFAULT;

    private PadSettings() {
    }

    /** Loads (or returns) the process-wide settings. Safe to call repeatedly. */
    public static PadSettings get(Context context) {
        synchronized (sLock) {
            if (sPrefs == null) {
                sPrefs = context.getApplicationContext()
                        .getSharedPreferences(PREFS_FILE, Context.MODE_PRIVATE);
            }
        }
        PadSettings s = new PadSettings();
        s.enabled = sPrefs.getBoolean(KEY_ENABLED, ENABLED_DEFAULT);
        s.hidden = sPrefs.getBoolean(KEY_HIDDEN, HIDDEN_DEFAULT);
        s.scale = clamp(sPrefs.getFloat(KEY_SCALE, SCALE_DEFAULT), SCALE_MIN, SCALE_MAX);
        s.opacity = clamp(sPrefs.getFloat(KEY_OPACITY, OPACITY_DEFAULT), OPACITY_MIN, OPACITY_MAX);
        s.haptics = sPrefs.getBoolean(KEY_HAPTICS, HAPTICS_DEFAULT);
        return s;
    }

    private static float clamp(float v, float min, float max) {
        if (Float.isNaN(v)) {
            return min; // corrupted store: deterministic, sane value
        }
        return v < min ? min : (v > max ? max : v);
    }

    public boolean enabled() {
        return enabled;
    }

    public boolean hidden() {
        return hidden;
    }

    public float scale() {
        return scale;
    }

    public float opacity() {
        return opacity;
    }

    public boolean haptics() {
        return haptics;
    }

    public PadSettings setEnabled(boolean value) {
        enabled = value;
        persist().putBoolean(KEY_ENABLED, value).apply();
        return this;
    }

    public PadSettings setHidden(boolean value) {
        hidden = value;
        persist().putBoolean(KEY_HIDDEN, value).apply();
        return this;
    }

    public PadSettings setScale(float value) {
        scale = clamp(value, SCALE_MIN, SCALE_MAX);
        persist().putFloat(KEY_SCALE, scale).apply();
        return this;
    }

    public PadSettings setOpacity(float value) {
        opacity = clamp(value, OPACITY_MIN, OPACITY_MAX);
        persist().putFloat(KEY_OPACITY, opacity).apply();
        return this;
    }

    public PadSettings setHaptics(boolean value) {
        haptics = value;
        persist().putBoolean(KEY_HAPTICS, value).apply();
        return this;
    }

    /** Restores every tunable to its default (keeps enabled/hidden state). */
    public PadSettings resetToDefaults() {
        setScale(SCALE_DEFAULT);
        setOpacity(OPACITY_DEFAULT);
        setHaptics(HAPTICS_DEFAULT);
        return this;
    }

    private SharedPreferences.Editor persist() {
        return sPrefs.edit();
    }
}
