package com.rexauto.port;

import android.content.Context;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

/** Title ID -> game name, from the bundled tools/xbox360_titles.json asset. */
public final class TitleDb {
    private static JSONObject db;

    private TitleDb() { }

    public static synchronized String lookup(Context ctx, String titleId) {
        if (titleId == null) return null;
        try {
            if (db == null) {
                try (InputStream in = ctx.getAssets().open("xbox360_titles.json")) {
                    ByteArrayOutputStream bo = new ByteArrayOutputStream();
                    byte[] buf = new byte[1 << 16];
                    int n;
                    while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
                    db = new JSONObject(bo.toString("UTF-8"));
                }
            }
            return db.optString(titleId.toUpperCase(), null);
        } catch (Exception e) {
            return null;
        }
    }
}
