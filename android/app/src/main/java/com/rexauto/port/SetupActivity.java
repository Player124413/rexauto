package com.rexauto.port;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.res.AssetManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import android.database.Cursor;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.io.FileWriter;
import java.io.InputStream;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * The launcher. Knows exactly which game this APK was recompiled for
 * (title + title ID baked in at build time via BuildConfig), so it can tell
 * the user what to drop in and refuse the wrong disc. Takes an ISO, a bare
 * default.xex or an already-extracted folder, unpacks into the app's own
 * storage (no permissions needed), then offers graphics settings and Play.
 */
public class SetupActivity extends Activity {
    private static final int REQ_PICK_FILE = 1;
    private static final int REQ_PICK_TREE = 2;
    private static final int REQ_PICK_DRIVER = 3;

    private TextView status;
    private ProgressBar progress;
    private Button playBtn, pickBtn, pickDirBtn, gfxBtn, driverBtn, resetBtn;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile boolean busy;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        float dp = getResources().getDisplayMetrics().density;
        int pad = (int) (20 * dp);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, pad);
        root.setBackgroundColor(Color.BLACK);

        // --- header: cover + "<Title>" / "Android Edition" -----------------
        LinearLayout header = new LinearLayout(this);
        header.setOrientation(LinearLayout.HORIZONTAL);
        header.setGravity(Gravity.CENTER_VERTICAL);
        Bitmap cover = loadCover();
        if (cover != null) {
            ImageView iv = new ImageView(this);
            iv.setImageBitmap(cover);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams((int) (72 * dp), (int) (72 * dp));
            lp.rightMargin = pad / 2;
            iv.setLayoutParams(lp);
            header.addView(iv);
        }
        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        TextView title = new TextView(this);
        title.setText(BuildConfig.GAME_TITLE);
        title.setTextSize(24);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setTextColor(Color.WHITE);
        titles.addView(title);
        TextView edition = new TextView(this);
        edition.setText(getString(R.string.edition, BuildConfig.TITLE_ID));
        edition.setTextSize(14);
        edition.setTextColor(0xFFB0B0B0);
        titles.addView(edition);
        header.addView(titles);
        root.addView(header);

        TextView help = new TextView(this);
        help.setText(getString(R.string.setup_help, BuildConfig.GAME_TITLE, BuildConfig.TITLE_ID));
        help.setTextSize(14);
        help.setTextColor(0xFFDDDDDD);
        help.setPadding(0, pad / 2, 0, pad / 2);
        root.addView(help);

        status = new TextView(this);
        status.setTextSize(14);
        status.setTextColor(0xFFFFD27F);
        root.addView(status);

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setVisibility(View.GONE);
        progress.setMax(1000);
        root.addView(progress);

        playBtn = button(root, R.string.play, v -> launchGame());
        pickBtn = button(root, R.string.pick_iso, v -> pickFile());
        pickDirBtn = button(root, R.string.pick_folder, v -> pickFolder());
        gfxBtn = button(root, R.string.graphics, v -> showGraphicsDialog());
        driverBtn = button(root, R.string.gpu_driver, v -> showDriverDialog());
        resetBtn = button(root, R.string.reset, v -> reset());

        ScrollView sv = new ScrollView(this);
        sv.setBackgroundColor(Color.BLACK);
        sv.addView(root);
        setContentView(sv);
        refresh();
    }

    private Button button(LinearLayout parent, int text, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setOnClickListener(l);
        parent.addView(b);
        return b;
    }

    private Bitmap loadCover() {
        try (InputStream in = getAssets().open("cover.png", AssetManager.ACCESS_STREAMING)) {
            return BitmapFactory.decodeStream(in);
        } catch (Exception e) {
            return null;
        }
    }

    private void refresh() {
        boolean ready = GameFiles.hasValidGameRoot(this);
        playBtn.setVisibility(ready ? View.VISIBLE : View.GONE);
        gfxBtn.setVisibility(View.VISIBLE);
        if (!busy) {
            status.setText(ready ? getString(R.string.status_ready, GameFiles.configuredGameRoot(this))
                                 : getString(R.string.status_idle));
        }
    }

    // --- input pickers ------------------------------------------------------
    private void pickFile() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"application/octet-stream", "application/x-iso9660-image", "*/*"});
        startActivityForResult(i, REQ_PICK_FILE);
    }

    private void pickFolder() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivityForResult(i, REQ_PICK_TREE);
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (res != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        if (req == REQ_PICK_FILE) importFile(uri);
        else if (req == REQ_PICK_TREE) importTree(uri);
        else if (req == REQ_PICK_DRIVER) importDriver(uri);
    }

    private void setBusy(boolean b) {
        busy = b;
        pickBtn.setEnabled(!b);
        pickDirBtn.setEnabled(!b);
        driverBtn.setEnabled(!b);
        resetBtn.setEnabled(!b);
        playBtn.setEnabled(!b);
        progress.setVisibility(b ? View.VISIBLE : View.GONE);
        progress.setIndeterminate(true);
    }

    private void post(String s) { ui.post(() -> status.setText(s)); }

    private void postProgress(String s, long done, long total) {
        ui.post(() -> {
            progress.setIndeterminate(total <= 0);
            if (total > 0) progress.setProgress((int) (done * 1000 / total));
            status.setText(String.format(Locale.US, "%s  %d / %d MB", s, done >> 20, total >> 20));
        });
    }

    /** ISO or bare default.xex picked with the document picker. */
    private void importFile(Uri uri) {
        setBusy(true);
        String name = displayName(uri);
        post(getString(R.string.status_reading, name));
        new Thread(() -> {
            File dest = GameFiles.gameDir(this);
            try {
                IsoExtractor iso = IsoExtractor.open(getContentResolver(), uri);
                File xex;
                if (iso != null) {
                    dest.mkdirs();
                    xex = iso.extractAll(dest, this::postProgress);
                    iso.close();
                    if (xex == null) throw new Exception(getString(R.string.err_no_xex_in_iso));
                } else if ((xex = importPackage(uri, name, dest)) != null) {
                    // CON/LIVE/PIRS handled
                } else {
                    // not a disc: accept a raw default.xex
                    try (InputStream probe = getContentResolver().openInputStream(uri)) {
                        if (GameFiles.xexTitleId(probe) == null)
                            throw new Exception(getString(R.string.err_not_iso, name));
                    }
                    xex = new File(dest, "default.xex");
                    try (InputStream in = getContentResolver().openInputStream(uri)) {
                        IsoExtractor.copyStream(in, xex);
                    }
                }
                finish(xex);
            } catch (Exception e) {
                fail(e.getMessage());
            }
        }, "import").start();
    }

    /**
     * CON / LIVE / PIRS picked as a single file: STFS (XBLA, arcade titles) is
     * unpacked entry by entry; a single-file Games-on-Demand image goes through
     * the GDFX path. A multi-part GoD needs its ".data" folder, so the user is
     * pointed at the folder picker. Returns null when the file is not a package.
     */
    private File importPackage(Uri uri, String name, File dest) throws Exception {
        java.nio.ByteBuffer hdr;
        android.os.ParcelFileDescriptor pfd = getContentResolver().openFileDescriptor(uri, "r");
        if (pfd == null) return null;
        java.nio.channels.FileChannel ch = new java.io.FileInputStream(pfd.getFileDescriptor()).getChannel();
        hdr = StfsExtractor.readHeader(ch);
        if (hdr == null) { ch.close(); pfd.close(); return null; }
        dest.mkdirs();
        if (StfsExtractor.volumeType(hdr) == StfsExtractor.VOLUME_SVOD) {
            ch.close(); pfd.close();
            if (hdr.getInt(0x39D) > 1)
                throw new Exception(getString(R.string.err_god_needs_folder, name));
            StfsExtractor.Found f = new StfsExtractor.Found(uri, name, hdr, null);
            post(getString(R.string.status_reading, name + " (Games on Demand)"));
            IsoExtractor god = StfsExtractor.openSvod(getContentResolver(), f, null);
            try {
                File xex = god.extractAll(dest, this::postProgress);
                if (xex == null) throw new Exception(getString(R.string.err_no_xex_in_iso));
                return xex;
            } finally { god.close(); }
        }
        post(getString(R.string.status_reading, name + " (STFS)"));
        try {
            File xex = new StfsExtractor.Stfs(ch, hdr).extractAll(dest, this::postProgress);
            if (xex == null) throw new Exception(getString(R.string.err_no_xex_in_stfs, name));
            return xex;
        } finally { ch.close(); pfd.close(); }
    }

    /** Already-extracted folder picked with the tree picker: copy it in. */
    private void importTree(Uri tree) {
        setBusy(true);
        post(getString(R.string.status_copying));
        new Thread(() -> {
            File dest = GameFiles.gameDir(this);
            try {
                dest.mkdirs();
                File xex;
                StfsExtractor.Found pkg = StfsExtractor.findInTree(getContentResolver(), tree, DocumentsContract.getTreeDocumentId(tree), 0);
                if (pkg != null && pkg.volumeType() == StfsExtractor.VOLUME_SVOD) {
                    // Games on Demand dump: <id>/<header> + <header>.data/Data0000..
                    post(getString(R.string.status_reading, pkg.name + " (Games on Demand)"));
                    IsoExtractor god = StfsExtractor.openSvod(getContentResolver(), pkg, tree);
                    try { xex = god.extractAll(dest, this::postProgress); } finally { god.close(); }
                    if (xex == null) throw new Exception(getString(R.string.err_no_xex_in_iso));
                } else if (pkg != null) {
                    post(getString(R.string.status_reading, pkg.name + " (STFS)"));
                    android.os.ParcelFileDescriptor pfd = getContentResolver().openFileDescriptor(pkg.headerDoc, "r");
                    java.nio.channels.FileChannel ch = new java.io.FileInputStream(pfd.getFileDescriptor()).getChannel();
                    try { xex = new StfsExtractor.Stfs(ch, pkg.hdr).extractAll(dest, this::postProgress); }
                    finally { ch.close(); pfd.close(); }
                    if (xex == null) throw new Exception(getString(R.string.err_no_xex_in_stfs, pkg.name));
                } else {
                    long[] counter = {0};
                    copyTree(tree, DocumentsContract.getTreeDocumentId(tree), dest, counter, 0);
                    xex = findXex(dest, 0);
                }
                if (xex == null) throw new Exception(getString(R.string.err_no_xex_in_folder));
                finish(xex);
            } catch (Exception e) {
                fail(e.getMessage());
            }
        }, "import-tree").start();
    }

    private void copyTree(Uri tree, String docId, File dest, long[] counter, int depth) throws Exception {
        if (depth > 12) return;
        Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, docId);
        List<String[]> rows = new ArrayList<>();
        try (Cursor c = getContentResolver().query(children, new String[]{
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE}, null, null, null)) {
            while (c != null && c.moveToNext()) rows.add(new String[]{c.getString(0), c.getString(1), c.getString(2)});
        }
        for (String[] r : rows) {
            String name = r[1] == null ? "file" : r[1].replace("/", "_");
            File out = new File(dest, name);
            if (DocumentsContract.Document.MIME_TYPE_DIR.equals(r[2])) {
                out.mkdirs();
                copyTree(tree, r[0], out, counter, depth + 1);
            } else {
                Uri doc = DocumentsContract.buildDocumentUriUsingTree(tree, r[0]);
                try (InputStream in = getContentResolver().openInputStream(doc)) {
                    IsoExtractor.copyStream(in, out);
                }
                counter[0]++;
                post(getString(R.string.status_copied, counter[0], name));
            }
        }
    }

    private static File findXex(File dir, int depth) {
        if (depth > 4) return null;
        File[] kids = dir.listFiles();
        if (kids == null) return null;
        for (File f : kids) if (f.isFile() && f.getName().equalsIgnoreCase("default.xex")) return f;
        for (File f : kids) if (f.isDirectory()) { File x = findXex(f, depth + 1); if (x != null) return x; }
        return null;
    }

    /** Title-ID gate + write game_root.txt. */
    private void finish(File xex) throws Exception {
        String got = GameFiles.xexTitleId(xex);
        String want = BuildConfig.TITLE_ID;
        if (want != null && !want.isEmpty() && !want.equals("00000000") && got != null && !got.equalsIgnoreCase(want)) {
            String other = TitleDb.lookup(this, got);
            throw new Exception(getString(R.string.err_wrong_game,
                    other != null ? other : ("Title ID " + got), BuildConfig.GAME_TITLE, want));
        }
        File root = xex.getParentFile();
        try (PrintWriter w = new PrintWriter(new FileWriter(GameFiles.configFile(this), false))) {
            w.println(root.getAbsolutePath());
        }
        new GraphicsSettings(this).write(this);
        ui.post(() -> {
            setBusy(false);
            refresh();
            Toast.makeText(this, R.string.status_done, Toast.LENGTH_SHORT).show();
        });
    }

    private void fail(String msg) {
        ui.post(() -> {
            setBusy(false);
            status.setText(msg == null ? getString(R.string.err_generic) : msg);
            refresh();
        });
    }

    private String displayName(Uri uri) {
        try (Cursor c = getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst()) return c.getString(0);
        } catch (Exception ignored) { }
        return uri.getLastPathSegment();
    }

    private void reset() {
        new AlertDialog.Builder(this)
                .setMessage(R.string.reset_confirm)
                .setPositiveButton(android.R.string.ok, (d, w) -> {
                    GameFiles.configFile(this).delete();
                    deleteRecursively(GameFiles.gameDir(this));
                    refresh();
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    private static void deleteRecursively(File f) {
        File[] kids = f.listFiles();
        if (kids != null) for (File k : kids) deleteRecursively(k);
        //noinspection ResultOfMethodCallIgnored
        f.delete();
    }

    // --- graphics settings --------------------------------------------------
    // --- custom GPU driver (Turnip) ----------------------------------------
    private void showDriverDialog() {
        GpuDriver gd = new GpuDriver(this);
        List<GpuDriver.Info> list = gd.installed();
        String[] names = new String[list.size() + 1];
        names[0] = getString(R.string.gpu_driver_system);
        int checked = 0;
        for (int i = 0; i < list.size(); i++) {
            GpuDriver.Info d = list.get(i);
            names[i + 1] = d.name + (d.version.isEmpty() ? "" : "  (" + d.version + ")");
            if (d.slug.equals(gd.selected())) checked = i + 1;
        }
        final int[] pick = {checked};
        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle(R.string.gpu_driver)
                .setSingleChoiceItems(names, checked, (d, w) -> pick[0] = w)
                .setPositiveButton(android.R.string.ok, (d, w) -> {
                    try {
                        gd.select(pick[0] == 0 ? null : list.get(pick[0] - 1).slug);
                        Toast.makeText(this, pick[0] == 0 ? getString(R.string.gpu_driver_system) : list.get(pick[0] - 1).name, Toast.LENGTH_SHORT).show();
                    } catch (Exception e) { Toast.makeText(this, e.toString(), Toast.LENGTH_LONG).show(); }
                })
                .setNeutralButton(R.string.gpu_driver_import, (d, w) -> {
                    Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                    i.addCategory(Intent.CATEGORY_OPENABLE);
                    i.setType("*/*");
                    i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"application/zip", "application/x-zip-compressed", "application/octet-stream"});
                    startActivityForResult(i, REQ_PICK_DRIVER);
                })
                .setNegativeButton(android.R.string.cancel, null);
        if (!GpuDriver.isAdreno()) {
            b.setMessage(R.string.gpu_driver_not_adreno);
        }
        AlertDialog dlg = b.create();
        dlg.getListView().setOnItemLongClickListener((p, v, pos, id) -> {
            if (pos == 0) return true;
            GpuDriver.Info d = list.get(pos - 1);
            new AlertDialog.Builder(this).setMessage(getString(R.string.gpu_driver_remove, d.name))
                    .setPositiveButton(android.R.string.ok, (dd, ww) -> {
                        try { gd.remove(d.slug); } catch (Exception e) { Toast.makeText(this, e.toString(), Toast.LENGTH_LONG).show(); }
                        dlg.dismiss();
                        showDriverDialog();
                    }).setNegativeButton(android.R.string.cancel, null).show();
            return true;
        });
        dlg.show();
    }

    private void importDriver(Uri uri) {
        setBusy(true);
        String name = displayName(uri);
        post(getString(R.string.status_reading, name));
        new Thread(() -> {
            try {
                GpuDriver gd = new GpuDriver(this);
                GpuDriver.Info info = gd.importZip(getContentResolver(), uri, name);
                gd.select(info.slug);
                ui.post(() -> {
                    setBusy(false);
                    refresh();
                    Toast.makeText(this, getString(R.string.gpu_driver_installed, info.name), Toast.LENGTH_LONG).show();
                    showDriverDialog();
                });
            } catch (Exception e) {
                fail(e.getMessage());
            }
        }, "import-driver").start();
    }

    private void showGraphicsDialog() {
        GraphicsSettings gs = new GraphicsSettings(this);
        float dp = getResources().getDisplayMetrics().density;
        int pad = (int) (16 * dp);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(pad, pad, pad, pad);

        label(box, R.string.gfx_scale);
        Spinner scale = new Spinner(this);
        scale.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item,
                new String[]{"1x (720p, fastest)", "2x (1440p)", "3x (2160p)"}));
        scale.setSelection(gs.resolutionScale() - 1);
        box.addView(scale);

        label(box, R.string.gfx_effect);
        Spinner effect = new Spinner(this);
        String[] effects = {"bilinear", "cas", "fsr"};
        effect.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item,
                new String[]{"Bilinear (cheapest)", "AMD CAS (sharpen)", "AMD FSR (upscale)"}));
        for (int i = 0; i < effects.length; i++) if (effects[i].equals(gs.presentEffect())) effect.setSelection(i);
        box.addView(effect);

        label(box, R.string.gfx_video_mode);
        Spinner mode = new Spinner(this);
        int[][] modes = {{1280, 720}, {1920, 1080}, {1024, 576}, {960, 540}};
        mode.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item,
                new String[]{"1280x720 (default)", "1920x1080 (heavy)", "1024x576 (lighter)", "960x540 (lightest)"}));
        for (int i = 0; i < modes.length; i++) if (modes[i][0] == gs.videoWidth()) mode.setSelection(i);
        box.addView(mode);

        CheckBox vsync = new CheckBox(this);
        vsync.setText(R.string.gfx_vsync);
        vsync.setChecked(gs.vsync());
        box.addView(vsync);

        CheckBox letterbox = new CheckBox(this);
        letterbox.setText(R.string.gfx_letterbox);
        letterbox.setChecked(gs.letterbox());
        box.addView(letterbox);

        CheckBox tolerant = new CheckBox(this);
        tolerant.setText(R.string.gfx_tolerant);
        tolerant.setChecked(gs.tolerant());
        box.addView(tolerant);

        label(box, R.string.gfx_extra);
        EditText extra = new EditText(this);
        extra.setHint("key=value");
        extra.setText(gs.extra());
        extra.setMinLines(2);
        box.addView(extra);

        ScrollView sv = new ScrollView(this);
        sv.addView(box);
        new AlertDialog.Builder(this)
                .setTitle(R.string.graphics)
                .setView(sv)
                .setPositiveButton(android.R.string.ok, (d, w) -> {
                    gs.setResolutionScale(scale.getSelectedItemPosition() + 1);
                    gs.setPresentEffect(effects[effect.getSelectedItemPosition()]);
                    int[] m = modes[mode.getSelectedItemPosition()];
                    gs.setVideoMode(m[0], m[1]);
                    gs.setVsync(vsync.isChecked());
                    gs.setLetterbox(letterbox.isChecked());
                    gs.setTolerant(tolerant.isChecked());
                    gs.setExtra(extra.getText().toString());
                    try { gs.write(this); } catch (Exception e) { Toast.makeText(this, e.toString(), Toast.LENGTH_LONG).show(); }
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
    }

    private void label(LinearLayout parent, int text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setPadding(0, 12, 0, 4);
        parent.addView(t);
    }

    private void launchGame() {
        try { new GraphicsSettings(this).write(this); } catch (Exception ignored) { }
        startActivity(new Intent(this, MainActivity.class));
    }
}
