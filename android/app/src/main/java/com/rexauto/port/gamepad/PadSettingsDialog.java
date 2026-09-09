package com.rexauto.port.gamepad;

import android.app.Activity;
import android.app.AlertDialog;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.LinearLayout;
import android.widget.SeekBar;
import android.widget.Switch;
import android.widget.TextView;

import com.rexauto.port.R;

/**
 * In-game settings for the virtual gamepad: size, opacity, haptics and a
 * "hide overlay" action. Fully programmatic (no XML layout), dark themed to
 * match the gamepad's glass style, and centered so it stays reachable in
 * landscape. Changes persist immediately via PadSettings; the onChanged
 * callback lets the overlay re-layout while the dialog stays open.
 */
final class PadSettingsDialog {

    /** One labeled percent slider bound to a clamped settings value. */
    private static final class SliderRow {
        final TextView label;
        final SeekBar bar;
        final float min;
        final float max;
        final int labelRes;
        final Activity activity;

        SliderRow(Activity activity, float density, int labelRes,
                  float min, float max, float initial,
                  ExtraListener extra) {
            this.activity = activity;
            this.min = min;
            this.max = max;
            this.labelRes = labelRes;

            final LinearLayout box = new LinearLayout(activity);
            box.setOrientation(LinearLayout.VERTICAL);
            box.setPadding(0, (int) (10 * density), 0, 0);

            label = new TextView(activity);
            label.setTextSize(14);
            box.addView(label);

            bar = new SeekBar(activity);
            bar.setMax(100);
            box.addView(bar);

            bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
                @Override
                public void onProgressChanged(SeekBar seekBar, int progress,
                                              boolean fromUser) {
                    final float value = fromProgress(progress);
                    // getString(res, arg) applies the %1$d%% format properly.
                    label.setText(activity.getString(labelRes, Math.round(value * 100)));
                    if (fromUser && extra != null) {
                        extra.onProgress(progress, true);
                    }
                }

                @Override
                public void onStartTrackingTouch(SeekBar seekBar) {
                }

                @Override
                public void onStopTrackingTouch(SeekBar seekBar) {
                }
            });
            setValue(initial);

            view = box;
        }

        final View view;

        float fromProgress(int progress) {
            return min + (max - min) * (progress / 100f);
        }

        void setValue(float value) {
            final int progress = Math.round((value - min) / (max - min) * 100f);
            bar.setProgress(progress);
            label.setText(activity.getString(labelRes, Math.round(value * 100)));
        }
    }

    /** Single-method hook for the caller's on-change handling. */
    private interface ExtraListener {
        void onProgress(int progress, boolean fromUser);
    }

    interface OnChanged {
        void onPadSettingsChanged();
    }

    private PadSettingsDialog() {
    }

    static void show(Activity activity, PadSettings settings, OnChanged onChanged) {
        final float density = activity.getResources().getDisplayMetrics().density;
        final int pad = (int) (20 * density);

        final LinearLayout root = new LinearLayout(activity);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad / 2);

        final TextView title = new TextView(activity);
        title.setText(R.string.pad_settings_title);
        title.setTextSize(18);
        title.setPadding(0, 0, 0, (int) (10 * density));
        root.addView(title);

        final SliderRow size = new SliderRow(activity, density, R.string.pad_size,
                PadSettings.SCALE_MIN, PadSettings.SCALE_MAX, settings.scale(),
                (progress, fromUser) -> {
                    if (fromUser) {
                        settings.setScale(PadSettings.SCALE_MIN
                                + (PadSettings.SCALE_MAX - PadSettings.SCALE_MIN)
                                * (progress / 100f));
                        onChanged.onPadSettingsChanged();
                    }
                });
        root.addView(size.view);

        final SliderRow opacity = new SliderRow(activity, density, R.string.pad_opacity,
                PadSettings.OPACITY_MIN, PadSettings.OPACITY_MAX, settings.opacity(),
                (progress, fromUser) -> {
                    if (fromUser) {
                        settings.setOpacity(PadSettings.OPACITY_MIN
                                + (PadSettings.OPACITY_MAX - PadSettings.OPACITY_MIN)
                                * (progress / 100f));
                        onChanged.onPadSettingsChanged();
                    }
                });
        root.addView(opacity.view);

        final Switch haptics = new Switch(activity);
        haptics.setText(R.string.pad_haptics);
        haptics.setTextSize(15);
        haptics.setPadding(0, (int) (14 * density), 0, (int) (6 * density));
        haptics.setChecked(settings.haptics());
        haptics.setOnCheckedChangeListener((CompoundButton b, boolean checked) -> {
            settings.setHaptics(checked);
            onChanged.onPadSettingsChanged();
        });
        root.addView(haptics);

        final AlertDialog dialog = new AlertDialog.Builder(activity,
                android.R.style.Theme_DeviceDefault_Dialog_Alert)
                .setView(root)
                .setCancelable(true)
                .create();

        // Buttons row: reset defaults + hide overlay.
        final LinearLayout buttons = new LinearLayout(activity);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        final Button reset = new Button(activity);
        reset.setText(R.string.pad_reset);
        reset.setOnClickListener(v -> {
            settings.resetToDefaults();
            haptics.setChecked(settings.haptics());
            size.setValue(settings.scale());
            opacity.setValue(settings.opacity());
            onChanged.onPadSettingsChanged();
        });
        buttons.addView(reset, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        final Button hide = new Button(activity);
        hide.setText(R.string.pad_hide);
        hide.setOnClickListener(v -> {
            settings.setHidden(true);
            dialog.dismiss();
            onChanged.onPadSettingsChanged();
        });
        buttons.addView(hide, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        root.addView(buttons);

        // Center the dialog on landscape screens.
        if (dialog.getWindow() != null) {
            dialog.getWindow().setGravity(Gravity.CENTER);
        }
        dialog.show();
    }
}
