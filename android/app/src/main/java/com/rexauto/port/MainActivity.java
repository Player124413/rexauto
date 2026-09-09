package com.rexauto.port;

import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;

import com.rexauto.port.gamepad.PadSettings;
import com.rexauto.port.gamepad.VirtualPadView;

import org.libsdl.app.SDLActivity;

/** SDL3 activity hosting the recompiled game (SDL is linked statically into libmain.so). */
public class MainActivity extends SDLActivity {
    private VirtualPadView mGamepad;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        if (!GameFiles.hasValidGameRoot(this)) {
            startActivity(new Intent(this, SetupActivity.class));
            finish();
            return;
        }
        super.onCreate(savedInstanceState);
        if (PadSettings.get(this).enabled()) {
            mGamepad = VirtualPadView.install(this);
        }
    }

    @Override
    protected void onPause() {
        if (mGamepad != null) mGamepad.onHostPause();
        super.onPause();
    }

    @Override
    protected String[] getLibraries() {
        return new String[]{"main"};
    }

    public static ParcelFileDescriptor openContentFd(String uri, String mode) {
        SDLActivity self = mSingleton;
        if (self == null || uri == null) return null;
        try {
            return self.getContentResolver().openFileDescriptor(Uri.parse(uri), mode == null ? "r" : mode);
        } catch (Exception e) {
            return null;
        }
    }
}
