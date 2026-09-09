package com.rexauto.port.gamepad;

/**
 * Layout engine for the on-screen virtual gamepad.
 *
 * PURE JAVA (no android.* imports) on purpose: the JVM unit tests
 * (scripts/check-pad-geometry.sh) sweep a matrix of screen sizes, aspect
 * ratios and user scale settings and assert that every control stays inside
 * the safe area and that no two visual shapes ever overlap.
 *
 * Design rules:
 *  - Every size derives from u = contentHeight * userScale, so the pad scales
 *    proportionally from phones to tablets. X positions anchor to the LEFT or
 *    RIGHT content edge in u units (never in width fractions), which keeps
 *    thumb clusters at constant reach across aspect ratios.
 *  - Two regimes: WIDE (content width >= 1.75u, phones in landscape) puts
 *    Start/Back at the bottom center; NARROW (tablets, 4:3) moves them to the
 *    top center row where nothing collides.
 *  - Hit zones are LARGER than the visuals (thumb slop) and MAY overlap each
 *    other; VirtualPadView resolves an ambiguous touch to the nearest control
 *    center (nearest-wins), so a generous zone never steals a press from the
 *    control the user is actually aiming at. Visual shapes, on the other
 *    hand, never overlap - that is what the tests enforce.
 *  - Gaps BETWEEN controls deliberately pass touches through to the game
 *    (SDL touch events): there are no controls there, and the game ignores
 *    them. "No dead zones" refers to every control being comfortably
 *    reachable and unobstructed, not to 100% screen coverage.
 */
public final class PadGeometry {

    // --- Control ids (stable contract with VirtualPadView) ------------------
    public static final int ID_STICK_L = 0;
    public static final int ID_STICK_R = 1;
    public static final int ID_DPAD = 2;
    public static final int ID_A = 3;
    public static final int ID_B = 4;
    public static final int ID_X = 5;
    public static final int ID_Y = 6;
    public static final int ID_LB = 7;
    public static final int ID_RB = 8;
    public static final int ID_LT = 9;
    public static final int ID_RT = 10;
    public static final int ID_BACK = 11;
    public static final int ID_START = 12;
    public static final int ID_MENU = 13;   // settings gear (top center)
    public static final int ID_TAB = 14;    // restore tab (hidden mode only)

    // --- Control types -------------------------------------------------------
    public static final int TYPE_STICK = 0;   // analog stick (circle + cap)
    public static final int TYPE_DPAD = 1;    // 4-way cross
    public static final int TYPE_BUTTON = 2;  // round button
    public static final int TYPE_PILL = 3;    // rounded rect (bumpers/triggers)

    // --- Proportion constants (fractions of u = contentHeight * scale) -------
    public static final float R_STICK = 0.125f;   // visual radius of a stick
    public static final float HIT_STICK = 1.25f;  // hit radius multiplier
    public static final float R_BTN = 0.075f;     // ABXY + small round buttons
    public static final float HIT_BTN = 1.45f;
    public static final float R_DPAD = 0.135f;    // cross half-length
    public static final float HIT_DPAD = 1.30f;
    public static final float R_SMALL = 0.042f;   // Start/Back
    public static final float HIT_SMALL = 1.60f;
    public static final float R_GEAR = 0.034f;    // settings gear
    public static final float HIT_GEAR = 1.80f;
    public static final float PILL_HW = 0.105f;   // bumper/trigger half width
    public static final float PILL_HH = 0.048f;   // bumper/trigger half height
    public static final float HIT_PILL = 1.35f;

    // Layout anchors. X/Y offsets marked uA are reach anchors (unscaled
    // content height); *_S offsets scale with the controls (uS).
    private static final float DPAD_X = 0.28f;          // uA, from left edge
    private static final float DPAD_Y = 0.53f;          // uA, from bottom (preferred)
    private static final float STICK_X_WIDE = 0.545f;   // uA
    private static final float STICK_X_NARROW = 0.44f;  // uA
    private static final float ABXY_SPREAD = 0.145f;    // uS, center-to-center
    private static final float SMALL_GAP = 0.13f;       // uA, Start/Back from center
    private static final float TOPROW_Y = 0.265f;       // uA, Start/Back (narrow)
    /**
     * WIDE regime switch: contentWidth >= (WIDE_BASE + WIDE_PER_SCALE*sCap)*uA.
     * Derived from Start/Back vs stick clearance; 0.06uA safety margin.
     */
    public static final float WIDE_BASE = 1.41f;
    public static final float WIDE_PER_SCALE = 0.334f;
    /** Effective scale caps per regime (physical fit limits). */
    public static final float SCALE_MAX_WIDE = 1.20f;
    public static final float SCALE_MAX_NARROW = 1.00f;

    /** One laid-out control. All values are absolute pixels. */
    public static final class Control {
        public final int id;
        public final int type;
        public final float cx;
        public final float cy;
        public final float radius;   // circle radius (0 for pills)
        public final float halfW;    // pill half width (0 for circles)
        public final float halfH;    // pill half height (0 for circles)
        public final float hitRadius;

        Control(int id, int type, float cx, float cy,
                float radius, float halfW, float halfH, float hitRadius) {
            this.id = id;
            this.type = type;
            this.cx = cx;
            this.cy = cy;
            this.radius = radius;
            this.halfW = halfW;
            this.halfH = halfH;
            this.hitRadius = hitRadius;
        }

        public float visualLeft() {
            return type == TYPE_PILL ? cx - halfW : cx - radius;
        }

        public float visualRight() {
            return type == TYPE_PILL ? cx + halfW : cx + radius;
        }

        public float visualTop() {
            return type == TYPE_PILL ? cy - halfH : cy - radius;
        }

        public float visualBottom() {
            return type == TYPE_PILL ? cy + halfH : cy + radius;
        }
    }

    private PadGeometry() {
    }

    /**
     * Builds the full layout for a viewport.
     *
     * Two unit systems keep the layout correct at every scale:
     *   - uA = contentHeight: ANCHOR positions (thumb-reach distances that
     *     must not move when the user resizes controls).
     *   - uS = contentHeight * sEff: control SIZES (and size-driven edge
     *     offsets such as the shoulder row and the face-cluster anchor).
     * The effective scale is capped per regime (WIDE 1.2, NARROW 1.0): the
     * shoulder/face/stick stack physically cannot grow beyond ~1.2x on a
     * 16:9 phone or ~1.0x on a 4:3 tablet, and an honest cap beats a broken
     * layout. (PadSettings.SCALE_MAX mirrors the WIDE cap.)
     *
     * @param w viewport width in pixels
     * @param h viewport height in pixels
     * @param insetL/T/R/B display-cutout / system-bar safe insets (pixels)
     * @param scale user scale multiplier (PadSettings.SCALE_*)
     */
    public static Control[] build(int w, int h,
                                  int insetL, int insetT, int insetR, int insetB,
                                  float scale) {
        final float left = insetL;
        final float top = insetT;
        final float right = w - insetR;
        final float bottom = h - insetB;
        final float cw = Math.max(1f, right - left);
        final float ch = Math.max(1f, bottom - top);
        final float uA = ch;
        final float sCap = Math.min(scale, SCALE_MAX_WIDE);
        final boolean wide = cw >= (WIDE_BASE + WIDE_PER_SCALE * sCap) * uA;
        float sEff = wide ? sCap : Math.min(scale, SCALE_MAX_NARROW);
        // Aspect-ratio fit cap: on near-square viewports the left/right
        // clusters would collide no matter what, so the pad shrinks instead
        // (0.94 = stick spread + radii + gap; 1.05 = bumper row spread).
        final float aspect = cw / ch;
        final float sFit = Math.min((aspect - 0.94f) / 0.25f, (aspect - 0.06f) / 1.05f);
        sEff = Math.max(0.25f, Math.min(sEff, sFit));
        final float uS = ch * sEff;

        final Control[] out = new Control[14];

        // --- Top shoulder row (size-anchored to the edges) --------------------
        final float rowCy = (PILL_HH + 0.05f) * uS;
        out[ID_LT] = pill(ID_LT, left + 0.14f * uS, top + rowCy, uS);
        out[ID_LB] = pill(ID_LB, left + 0.42f * uS, top + rowCy, uS);
        out[ID_RB] = pill(ID_RB, right - 0.42f * uS, top + rowCy, uS);
        out[ID_RT] = pill(ID_RT, right - 0.14f * uS, top + rowCy, uS);

        // --- Settings gear (top center) ---------------------------------------
        out[ID_MENU] = new Control(ID_MENU, TYPE_BUTTON,
                left + cw / 2f, top + rowCy,
                R_GEAR * uS, 0f, 0f, HIT_GEAR * R_GEAR * uS);

        // --- Analog sticks (reach-anchored horizontally, bottom-anchored) -----
        final float stickX = wide ? STICK_X_WIDE : STICK_X_NARROW;
        final float stickBottom = wide ? 0.035f : 0.055f;
        final float stickCy = bottom - (R_STICK + stickBottom) * uS;
        out[ID_STICK_L] = new Control(ID_STICK_L, TYPE_STICK,
                left + stickX * uA, stickCy,
                R_STICK * uS, 0f, 0f, HIT_STICK * R_STICK * uS);
        out[ID_STICK_R] = new Control(ID_STICK_R, TYPE_STICK,
                right - stickX * uA, stickCy,
                R_STICK * uS, 0f, 0f, HIT_STICK * R_STICK * uS);

        // --- Face cluster (Xbox layout: Y top, B right, A bottom, X left) ------
        // The cluster sits higher on wide screens (the right stick drops to
        // 0.965*bottom there) and lower on narrow ones, keeping >=0.04u
        // separation from the stick in both regimes.
        final float abxyCx = right - 0.25f * uS;
        final float abxyCy = bottom - (wide ? 0.37f : 0.42f) * uS;
        final float spread = ABXY_SPREAD * uS;
        final float rBtn = R_BTN * uS;
        final float hitBtn = HIT_BTN * R_BTN * uS;
        out[ID_Y] = new Control(ID_Y, TYPE_BUTTON, abxyCx, abxyCy - spread,
                rBtn, 0f, 0f, hitBtn);
        out[ID_B] = new Control(ID_B, TYPE_BUTTON, abxyCx + spread, abxyCy,
                rBtn, 0f, 0f, hitBtn);
        out[ID_A] = new Control(ID_A, TYPE_BUTTON, abxyCx, abxyCy + spread,
                rBtn, 0f, 0f, hitBtn);
        out[ID_X] = new Control(ID_X, TYPE_BUTTON, abxyCx - spread, abxyCy,
                rBtn, 0f, 0f, hitBtn);

        // --- D-pad (upper left; clamped into its feasible vertical band) ------
        final float dpadCy = dpadBandCenter(wide, left, bottom, uA, uS,
                stickX, stickCy);
        out[ID_DPAD] = new Control(ID_DPAD, TYPE_DPAD,
                left + DPAD_X * uA, dpadCy,
                R_DPAD * uS, 0f, 0f, HIT_DPAD * R_DPAD * uS);

        // --- Start / Back (bottom center on wide, top center row on narrow) ---
        final float midX = left + cw / 2f;
        final float smallY = wide
                ? bottom - (R_SMALL + 0.073f) * uS
                : top + TOPROW_Y * uA;
        out[ID_BACK] = new Control(ID_BACK, TYPE_BUTTON,
                midX - SMALL_GAP * uA, smallY,
                R_SMALL * uS, 0f, 0f, HIT_SMALL * R_SMALL * uS);
        out[ID_START] = new Control(ID_START, TYPE_BUTTON,
                midX + SMALL_GAP * uA, smallY,
                R_SMALL * uS, 0f, 0f, HIT_SMALL * R_SMALL * uS);

        return out;
    }

    /**
     * D-pad vertical position: the preferred anchor (DPAD_Y), clamped into
     * the feasible band between the shoulder row and the left stick, so the
     * cross never collides with either at any scale.
     */
    private static float dpadBandCenter(boolean wide, float left, float bottom,
                                        float uA, float uS, float stickX,
                                        float stickCy) {
        final float dpadR = R_DPAD * uS;
        final float stickR = R_STICK * uS;
        final float minCy = (PILL_HH * 2f + 0.05f) * uS + dpadR + 0.02f * uA;
        final float dx = (stickX - DPAD_X) * uA;
        final float need = dpadR + stickR + 0.02f * uA;
        float maxCy = stickCy;
        if (dx < need) {
            final float dyMin = (float) Math.sqrt(need * need - dx * dx);
            maxCy = stickCy - dyMin;
        }
        final float preferred = bottom - 0.53f * uA;
        return Math.max(minCy, Math.min(preferred, maxCy));
    }

    /** Restore tab shown when the pad is hidden (right edge, vertically centered). */
    public static Control buildTab(int w, int h,
                                   int insetL, int insetT, int insetR, int insetB,
                                   float scale) {
        final float u = Math.max(1f, h - insetT - insetB) * scale;
        final float halfW = 0.020f * u;
        final float halfH = 0.075f * u;
        return new Control(ID_TAB, TYPE_PILL,
                w - insetR - halfW, insetT + (h - insetT - insetB) / 2f,
                0f, halfW, halfH, 1.6f * halfH);
    }

    private static Control pill(int id, float cx, float cy, float u) {
        return new Control(id, TYPE_PILL, cx, cy, 0f,
                PILL_HW * u, PILL_HH * u, HIT_PILL * Math.max(PILL_HW, PILL_HH) * u);
    }

    /** True when the point is inside a control's touch hit zone. */
    public static boolean hitContains(Control c, float x, float y) {
        final float dx = x - c.cx;
        final float dy = y - c.cy;
        if (c.type == TYPE_PILL) {
            // Expand the rect to the hit radius so corner slop matches the
            // visual pill shape (rounded corners) closely enough for thumbs.
            final float ex = c.halfW + (c.hitRadius - Math.max(c.halfW, c.halfH)) * 0.9f;
            final float ey = c.halfH + (c.hitRadius - Math.max(c.halfW, c.halfH)) * 0.9f;
            return dx >= -ex && dx <= ex && dy >= -ey && dy <= ey;
        }
        return dx * dx + dy * dy <= c.hitRadius * c.hitRadius;
    }

    /** True when the point is inside a control's VISUAL shape (sticks/dpad). */
    public static boolean visualContains(Control c, float x, float y) {
        final float dx = x - c.cx;
        final float dy = y - c.cy;
        if (c.type == TYPE_PILL) {
            return dx >= -c.halfW && dx <= c.halfW && dy >= -c.halfH && dy <= c.halfH;
        }
        return dx * dx + dy * dy <= c.radius * c.radius;
    }

    /** Squared distance between two points (test helper). */
    public static float dist2(float ax, float ay, float bx, float by) {
        final float dx = ax - bx;
        final float dy = ay - by;
        return dx * dx + dy * dy;
    }

    /**
     * Nearest-wins hit resolution shared by the view and the JVM tests: among
     * all controls whose touch zone contains the point, the closest center
     * wins. Sticks additionally require the point to be inside their VISUAL
     * circle (their zones are small; the cap is what a thumb aims at), while
     * buttons/pills/d-pad use their generous zones.
     *
     * @return the control id, or -1 when no control is hit (the view then
     *         ignores that finger; it does not reach the game surface).
     */
    public static int hitResolve(Control[] controls, float x, float y) {
        // Stick priority: a touch inside a stick's VISUAL circle always
        // belongs to that stick, even when a face button's generous zone also
        // covers the point (a dodge flick landing on the stick edge must
        // never fire the A button - nearest-wins alone would, because the
        // zones legitimately overlap by design).
        for (Control c : controls) {
            if (c != null && c.type == TYPE_STICK && visualContains(c, x, y)) {
                return c.id;
            }
        }
        int best = -1;
        float bestD2 = Float.MAX_VALUE;
        for (Control c : controls) {
            if (c == null || !hitContains(c, x, y)) {
                continue;
            }
            if (c.type == TYPE_STICK && !visualContains(c, x, y)) {
                continue;
            }
            final float d2 = dist2(x, y, c.cx, c.cy);
            if (d2 < bestD2) {
                bestD2 = d2;
                best = c.id;
            }
        }
        return best;
    }
}
