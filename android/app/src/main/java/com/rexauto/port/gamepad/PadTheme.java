package com.rexauto.port.gamepad;

import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;

/**
 * Visual language of the virtual gamepad: a dark "glass" system with
 * per-control accent colors (classic Xbox face palette), thin light strokes,
 * soft shadows and bright pressed states. Everything is drawn with vector
 * primitives (Paint/Path) - no bitmaps - so the pad stays crisp on any
 * screen density and costs ~0 memory.
 *
 * All Paints are pre-allocated here and mutated in place while drawing
 * (setColor/setAlpha are trivially cheap): zero allocation per frame.
 */
final class PadTheme {

    // --- Palette -------------------------------------------------------------
    static final int GLASS_FILL = 0xD9121620;        // dark glass body
    static final int GLASS_FILL_PRESSED = 0xF01C2432;
    static final int STROKE = 0x59FFFFFF;            // idle hairline
    static final int STROKE_PRESSED = 0xD9FFFFFF;    // pressed ring
    static final int SHADOW = 0x40000000;            // drop shadow
    static final int TEXT = 0xF2FFFFFF;              // labels
    static final int CAP_FILL = 0x26FFFFFF;          // stick cap sheen
    static final int CAP_EDGE = 0x8CFFFFFF;          // stick cap ring
    static final int GLOW = 0x4DFFFFFF;              // pressed glow fill

    // Classic Xbox face-button accents.
    static final int ACCENT_A = 0xFF7ED957;          // green
    static final int ACCENT_B = 0xFFFF5A5F;          // red
    static final int ACCENT_X = 0xFF4FC3F7;          // blue
    static final int ACCENT_Y = 0xFFFFD54F;          // yellow
    static final int ACCENT_NEUTRAL = 0xFFB8C4D6;    // dpad/menu/shoulders

    // --- Paints (reused for every frame) -------------------------------------
    final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    final Paint stroke = new Paint(Paint.ANTI_ALIAS_FLAG);
    final Paint glyph = new Paint(Paint.ANTI_ALIAS_FLAG);
    final Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
    final Paint shadow = new Paint(Paint.ANTI_ALIAS_FLAG);

    // Scratch objects for shape building (never mutated across draws).
    final RectF rect = new RectF();
    final Path path = new Path();
    final android.graphics.Matrix matrix = new android.graphics.Matrix();
    final Paint.FontMetrics textMetrics = new Paint.FontMetrics();

    PadTheme() {
        stroke.setStyle(Paint.Style.STROKE);
        glyph.setStyle(Paint.Style.FILL);
        shadow.setStyle(Paint.Style.FILL);
        text.setColor(TEXT);
        text.setTypeface(Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD));
        text.setTextAlign(Paint.Align.CENTER);
    }

    /** Combines a base color with the user opacity multiplier. */
    static int withOpacity(int color, float opacity) {
        final int a = (int) ((color >>> 24) * opacity);
        return (a << 24) | (color & 0x00FFFFFF);
    }

    /** Accent color for a control id. */
    static int accentOf(int id) {
        switch (id) {
            case PadGeometry.ID_A: return ACCENT_A;
            case PadGeometry.ID_B: return ACCENT_B;
            case PadGeometry.ID_X: return ACCENT_X;
            case PadGeometry.ID_Y: return ACCENT_Y;
            default: return ACCENT_NEUTRAL;
        }
    }

    /** On-screen label for a control id (face letters use accent color). */
    static String labelOf(int id) {
        switch (id) {
            case PadGeometry.ID_A: return "A";
            case PadGeometry.ID_B: return "B";
            case PadGeometry.ID_X: return "X";
            case PadGeometry.ID_Y: return "Y";
            case PadGeometry.ID_LB: return "LB";
            case PadGeometry.ID_RB: return "RB";
            case PadGeometry.ID_LT: return "LT";
            case PadGeometry.ID_RT: return "RT";
            default: return "";
        }
    }
}
