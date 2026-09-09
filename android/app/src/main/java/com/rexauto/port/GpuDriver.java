package com.rexauto.port;

import android.content.ContentResolver;
import android.content.Context;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * Custom Vulkan driver packs (Turnip / newer Adreno blobs) - the same zip
 * format Yuzu, Skyline, Dolphin and Winlator use: a {@code meta.json} with
 * {@code name}, {@code libraryName} (e.g. libvulkan_freedreno.so) and one or
 * more .so files. Installed under {@code <internal files>/gpu_driver/<slug>/};
 * the native side reads {@code gpu_driver/driver.txt} and loads the library
 * through libadrenotools before the runtime starts.
 *
 * Only Qualcomm Adreno GPUs can load a replacement driver (the loader hooks
 * the vendor blob); on anything else the option is shown but disabled.
 */
public final class GpuDriver {
    public static final class Info {
        public final String slug, name, description, library, vendor, version;
        Info(String slug, String name, String description, String library, String vendor, String version) {
            this.slug = slug; this.name = name; this.description = description; this.library = library; this.vendor = vendor; this.version = version;
        }
        @Override public String toString() { return name; }
    }

    private static final String PREF = "gpu_driver";
    private final Context ctx;
    private final SharedPreferences prefs;

    public GpuDriver(Context ctx) {
        this.ctx = ctx;
        prefs = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }

    public static boolean isAdreno() {
        String soc = Build.VERSION.SDK_INT >= 31 ? (Build.SOC_MANUFACTURER + " " + Build.SOC_MODEL) : "";
        String hw = (Build.HARDWARE + " " + Build.BOARD + " " + soc).toLowerCase();
        return hw.contains("qcom") || hw.contains("qualcomm") || hw.contains("msm") || hw.contains("sm8") || hw.contains("sm7") || hw.contains("sm6") || hw.contains("sdm") || hw.contains("kona") || hw.contains("lahaina") || hw.contains("taro") || hw.contains("kalama") || hw.contains("pineapple") || hw.contains("sun");
    }

    public File root() { return new File(ctx.getFilesDir(), "gpu_driver"); }
    private File configFile() { return new File(root(), "driver.txt"); }

    /** Selected driver slug, or null for the system driver. */
    public String selected() { return prefs.getString("selected", null); }

    public List<Info> installed() {
        List<Info> out = new ArrayList<>();
        File[] dirs = root().listFiles(File::isDirectory);
        if (dirs == null) return out;
        for (File d : dirs) {
            if (d.getName().equals("tmp")) continue;
            Info i = readMeta(d);
            if (i != null) out.add(i);
        }
        return out;
    }

    private Info readMeta(File dir) {
        File meta = new File(dir, "meta.json");
        if (!meta.isFile()) return null;
        try (InputStream in = new java.io.FileInputStream(meta)) {
            JSONObject j = new JSONObject(new String(readAll(in), StandardCharsets.UTF_8));
            String lib = j.optString("libraryName", "");
            if (lib.isEmpty() || !new File(dir, lib).isFile()) return null;
            return new Info(dir.getName(), j.optString("name", dir.getName()), j.optString("description", ""), lib,
                    j.optString("vendor", ""), j.optString("driverVersion", j.optString("version", "")));
        } catch (Exception e) {
            return null;
        }
    }

    /** Unpack a driver zip picked with SAF. Returns the installed driver. */
    public Info importZip(ContentResolver cr, Uri uri, String displayName) throws IOException {
        File tmp = new File(root(), "import_" + System.currentTimeMillis());
        if (!tmp.mkdirs() && !tmp.isDirectory()) throw new IOException("mkdir " + tmp);
        List<String> sos = new ArrayList<>();
        boolean hasMeta = false;
        try (InputStream raw = cr.openInputStream(uri);
             ZipInputStream zip = new ZipInputStream(new BufferedInputStream(raw))) {
            ZipEntry e;
            long total = 0;
            while ((e = zip.getNextEntry()) != null) {
                if (e.isDirectory()) continue;
                // flatten: driver packs are flat, but some are zipped inside a folder
                String name = e.getName().replace('\\', '/');
                name = name.substring(name.lastIndexOf('/') + 1);
                if (name.isEmpty() || name.startsWith(".")) continue;
                if (!(name.endsWith(".so") || name.equals("meta.json"))) continue;
                File out = new File(tmp, name);
                try (OutputStream os = new FileOutputStream(out)) {
                    byte[] buf = new byte[1 << 16];
                    int n;
                    while ((n = zip.read(buf)) > 0) { os.write(buf, 0, n); total += n; if (total > (512L << 20)) throw new IOException("driver zip too large"); }
                }
                if (name.equals("meta.json")) hasMeta = true; else sos.add(name);
            }
        } catch (IOException e) {
            deleteTree(tmp);
            throw e;
        }
        if (sos.isEmpty()) { deleteTree(tmp); throw new IOException(ctx.getString(R.string.err_driver_zip, displayName)); }
        if (!hasMeta) {
            // no meta.json: synthesize one around the only/first .so
            String lib = sos.size() == 1 ? sos.get(0) : pickLibrary(sos);
            try (PrintWriter w = new PrintWriter(new FileWriter(new File(tmp, "meta.json")))) {
                w.println(new JSONObject().put("name", stripZip(displayName)).put("libraryName", lib).put("description", "imported without meta.json").toString());
            } catch (Exception e) { throw new IOException(e); }
        }
        Info info = readMeta(tmp);
        if (info == null) { deleteTree(tmp); throw new IOException(ctx.getString(R.string.err_driver_zip, displayName)); }
        String slug = info.name.replaceAll("[^A-Za-z0-9._-]+", "_");
        if (slug.isEmpty()) slug = "driver";
        File dest = new File(root(), slug);
        if (dest.exists()) deleteTree(dest);
        if (!tmp.renameTo(dest)) { deleteTree(tmp); throw new IOException("rename " + tmp + " -> " + dest); }
        // dlopen() needs the files to be non-writable by others; files dir is private already,
        // but make sure the .so is readable + executable for the loader.
        for (File f : dest.listFiles()) { f.setReadable(true, false); f.setExecutable(true, false); }
        return readMeta(dest);
    }

    private static String pickLibrary(List<String> sos) {
        for (String s : sos) if (s.contains("freedreno") || s.contains("turnip")) return s;
        for (String s : sos) if (s.startsWith("libvulkan")) return s;
        return sos.get(0);
    }

    private static String stripZip(String n) {
        if (n == null) return "driver";
        int i = n.lastIndexOf('.');
        return i > 0 ? n.substring(0, i) : n;
    }

    /** Select a driver (null = system) and write the native config. */
    public void select(String slug) throws IOException {
        root().mkdirs();
        if (slug == null) {
            prefs.edit().remove("selected").apply();
            configFile().delete();
            return;
        }
        Info info = readMeta(new File(root(), slug));
        if (info == null) throw new IOException("driver " + slug + " is not installed");
        prefs.edit().putString("selected", slug).apply();
        try (PrintWriter w = new PrintWriter(new FileWriter(configFile(), false))) {
            w.println(slug + "/" + info.library);
        }
    }

    public void remove(String slug) throws IOException {
        if (slug.equals(selected())) select(null);
        deleteTree(new File(root(), slug));
    }

    private static void deleteTree(File f) {
        File[] kids = f.listFiles();
        if (kids != null) for (File k : kids) deleteTree(k);
        f.delete();
    }

    private static byte[] readAll(InputStream in) throws IOException {
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        byte[] b = new byte[8192];
        int n;
        while ((n = in.read(b)) > 0) bo.write(b, 0, n);
        return bo.toByteArray();
    }
}
