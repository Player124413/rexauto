#!/usr/bin/env python3
"""rexauto Windows launcher -- the desktop twin of the Android launcher.

Lives next to the recompiled `<name>.exe` (built into `<Title> Launcher.exe`
by the workflow via PyInstaller, but also runs as a plain script):

  1. Game files: pick an ISO / XBLA-STFS / GoD package / default.xex / an
     already-extracted folder -> unpacked into  assets\  beside the exe
     (extract.py does the work; same engine as the pipeline).
  2. Graphics & performance: presets, render scale, vsync, FPS cap, upscaler,
     guest video mode, letterbox, tolerant dispatcher, extra cvars.
     Saved in launcher.json next to the exe; handed to the runtime as
     --key=value flags (and env.* -> environment) exactly like on Android.
  3. Play.

Zero absolute paths: everything is relative to the launcher's folder, so the
port folder can be moved / zipped / dropped into a Winlator prefix as-is.
"""
import json
import os
import subprocess
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
sys.path.insert(0, HERE)
if getattr(sys, "frozen", False):
    sys.path.insert(0, getattr(sys, "_MEIPASS", HERE))

try:
    import extract as _extract  # bundled by PyInstaller (--hidden-import) or beside the script
except Exception:  # pragma: no cover
    _extract = None

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_JSON = os.path.join(HERE, "launcher.json")
PORT_JSON = os.path.join(HERE, "port.json")  # written by CI: name/title/title_id/gpu_plugin
ASSETS = os.path.join(HERE, "assets")
USERDATA = os.path.join(HERE, "userdata")

DEFAULTS = {
    "preset": "balanced",          # a PC has headroom the phone lacks
    "resolution_scale": 1,
    "vsync": True,
    "fps_cap": 0,
    "present_effect": "bilinear",
    "video_mode": "1280x720",
    "letterbox": True,
    "fullscreen": False,
    "tolerant": True,
    "extra": "",
}
PRESETS = ["performance", "balanced", "accuracy"]
EFFECTS = ["bilinear", "cas", "fsr"]
VIDEO_MODES = ["1280x720", "1920x1080", "1024x576", "960x540"]
FPS_CAPS = ["0", "30", "60", "120"]
STR = {
    "performance": "Performance (weak PC / Winlator)",
    "balanced": "Balanced (default)",
    "accuracy": "Accuracy (SDK defaults)",
}


# ---------------------------------------------------------------- port info
def load_port_info():
    info = {"name": None, "title": None, "title_id": None, "gpu_plugin": None}
    try:
        info.update(json.load(open(PORT_JSON, encoding="utf-8")))
    except Exception:
        pass
    if not info["name"]:
        me = os.path.basename(sys.executable).lower() if getattr(sys, "frozen", False) else ""
        for fn in sorted(os.listdir(HERE)):
            fl = fn.lower()
            if fl.endswith(".exe") and fl != me and "launcher" not in fl and not fl.startswith("unins"):
                info["name"] = fn[:-4]
                break
    if not info["gpu_plugin"]:
        for fn in os.listdir(HERE):
            if fn.lower().startswith("rexgpu-") and fn.lower().endswith(".dll"):
                info["gpu_plugin"] = fn[len("rexgpu-"):-len(".dll")]
                break
    if not info["title"]:
        info["title"] = (info["name"] or "Xbox 360 game").replace("_", " ").title()
    return info


def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.load(open(APP_JSON, encoding="utf-8")))
    except Exception:
        pass
    return s


def save_settings(s):
    with open(APP_JSON, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


def find_game_root():
    """Same search the exe itself does: assets/, game/, data/, ./, one level of nesting."""
    def has(p):
        return any(os.path.isfile(os.path.join(p, n)) for n in ("default.xex", "Default.xex", "DEFAULT.XEX"))
    for sub in ("assets", "game", "data", "."):
        p = os.path.normpath(os.path.join(HERE, sub))
        if has(p):
            return p
    for sub in ("assets", "game", "data"):
        base = os.path.join(HERE, sub)
        if os.path.isdir(base):
            for d in sorted(os.listdir(base)):
                p = os.path.join(base, d)
                if os.path.isdir(p) and has(p):
                    return p
    return None


# ---------------------------------------------------------------- cvars
def cvars_from(s):
    """Settings -> ordered {cvar: value}; mirrors GraphicsSettings.asCvars()."""
    m = {}
    scale = int(s.get("resolution_scale", 1))
    m["resolution_scale"] = str(scale)
    m["draw_resolution_scale_x"] = str(scale)
    m["draw_resolution_scale_y"] = str(scale)
    m["vsync"] = "true" if s.get("vsync", True) else "false"
    m["present_effect"] = s.get("present_effect", "bilinear")
    try:
        w, h = s.get("video_mode", "1280x720").lower().split("x")
        m["video_mode_width"], m["video_mode_height"] = str(int(w)), str(int(h))
    except Exception:
        m["video_mode_width"], m["video_mode_height"] = "1280", "720"
    m["present_letterbox"] = "true" if s.get("letterbox", True) else "false"
    m["fullscreen"] = "true" if s.get("fullscreen", False) else "false"
    p = s.get("preset", "balanced")
    if p != "accuracy":
        m["readback_memexport"] = "false"
        m["vulkan_readback_memexport"] = "false"
        m["vulkan_readback_resolve"] = "false"
        m["async_shader_compilation"] = "true"
        m["log_level"] = "warning"
        m["log_high_frequency_kernel_calls"] = "false"
        m["vulkan_log_debug_messages"] = "false"
        m["gpu_debug_markers"] = "false"
    if p == "performance":
        m["anisotropic_override"] = "0"
        m["occlusion_query_enable"] = "false"
        m["native_2x_msaa"] = "false"
        m["gamma_render_target_as_unorm16"] = "false"
        m["texture_cache_memory_limit_soft"] = "384"
        m["texture_cache_memory_limit_hard"] = "768"
    elif p == "balanced":
        m["anisotropic_override"] = "4"  # 16x
    if int(s.get("fps_cap", 0) or 0) > 0:
        m["env.REX_FPS_CAP"] = str(int(s["fps_cap"]))
    if s.get("tolerant", True):
        m["env.REX_HEAL_DISCOVER"] = "1"
    for line in (s.get("extra") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        m[k.strip()] = v.strip()
    return m


def launch(info, s, log):
    exe = os.path.join(HERE, (info.get("name") or "") + ".exe")
    if not info.get("name") or not os.path.isfile(exe):
        raise RuntimeError("game exe not found next to the launcher")
    root = find_game_root()
    if not root:
        raise RuntimeError("no game files yet -- add them first")
    env = dict(os.environ)
    args = [exe, "--game_data_root=%s" % root]
    for k, v in cvars_from(s).items():
        if k.startswith("env."):
            env[k[4:]] = v
        else:
            args.append("--%s=%s" % (k, v))
    if info.get("gpu_plugin"):
        env.setdefault("REX_GPU_PLUGIN", info["gpu_plugin"])
        args.append("--gpu_plugin=%s" % info["gpu_plugin"])
    os.makedirs(USERDATA, exist_ok=True)
    log("launch: " + " ".join(args[1:]))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, cwd=HERE, env=env, creationflags=flags, close_fds=True)


# ---------------------------------------------------------------- UI
class App(tk.Tk):
    BG, FG, ACC, MUTED, CARD = "#0f1117", "#e6e6e6", "#7c5cff", "#8a8f9c", "#171a23"

    def __init__(self):
        super().__init__()
        self.info = load_port_info()
        self.s = load_settings()
        self.title("%s -- rexauto" % self.info["title"])
        self.configure(bg=self.BG)
        self.geometry("720x640")
        self.minsize(640, 560)
        try:
            ico = os.path.join(HERE, "launcher.ico")
            if os.path.isfile(ico):
                self.iconbitmap(ico)
        except Exception:
            pass
        self._style()
        self._build()
        self.refresh()

    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=self.BG, foreground=self.FG, fieldbackground=self.CARD, bordercolor=self.CARD)
        st.configure("TFrame", background=self.BG)
        st.configure("Card.TFrame", background=self.CARD)
        st.configure("TLabel", background=self.BG, foreground=self.FG)
        st.configure("Card.TLabel", background=self.CARD, foreground=self.FG)
        st.configure("Muted.TLabel", background=self.CARD, foreground=self.MUTED)
        st.configure("H.TLabel", background=self.BG, foreground=self.FG, font=("Segoe UI", 16, "bold"))
        st.configure("TButton", padding=6)
        st.configure("Play.TButton", background=self.ACC, foreground="white", font=("Segoe UI", 12, "bold"), padding=10)
        st.map("Play.TButton", background=[("active", "#6a4bf0"), ("disabled", "#3a3f4d")])
        st.configure("TCheckbutton", background=self.CARD, foreground=self.FG)
        st.configure("TCombobox", fieldbackground=self.CARD, background=self.CARD)
        st.configure("TLabelframe", background=self.CARD, foreground=self.FG)
        st.configure("TLabelframe.Label", background=self.CARD, foreground=self.MUTED)

    def _build(self):
        top = ttk.Frame(self, padding=(16, 14, 16, 6))
        top.pack(fill="x")
        self.cover_img = None
        cover = os.path.join(HERE, "cover.png")
        if os.path.isfile(cover):
            try:
                img = tk.PhotoImage(file=cover)
                f = max(1, int(max(img.width(), img.height()) / 96 + 0.999))
                self.cover_img = img.subsample(f, f)
                tk.Label(top, image=self.cover_img, bg=self.BG, bd=0).pack(side="left", padx=(0, 14))
            except Exception:
                self.cover_img = None
        tt = ttk.Frame(top)
        tt.pack(side="left", fill="x", expand=True)
        ttk.Label(tt, text=self.info["title"], style="H.TLabel").pack(anchor="w")
        sub = "Title ID %s  ·  %s.exe" % (self.info.get("title_id") or "--------", self.info.get("name") or "?")
        ttk.Label(tt, text=sub, foreground=self.MUTED).pack(anchor="w")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=6)
        self.tab_game = ttk.Frame(nb, style="Card.TFrame", padding=14)
        self.tab_gfx = ttk.Frame(nb, style="Card.TFrame", padding=14)
        nb.add(self.tab_game, text="  Game files  ")
        nb.add(self.tab_gfx, text="  Graphics & performance  ")
        self._build_game()
        self._build_gfx()

        bottom = ttk.Frame(self, padding=(16, 4, 16, 14))
        bottom.pack(fill="x")
        self.status = ttk.Label(bottom, text="", foreground=self.MUTED)
        self.status.pack(side="left", fill="x", expand=True)
        self.play_btn = ttk.Button(bottom, text="▶  Play", style="Play.TButton", command=self.on_play)
        self.play_btn.pack(side="right")

    def _build_game(self):
        t = self.tab_game
        self.game_lbl = ttk.Label(t, text="", style="Card.TLabel", wraplength=640, justify="left")
        self.game_lbl.pack(anchor="w", pady=(0, 10))
        row = ttk.Frame(t, style="Card.TFrame")
        row.pack(anchor="w", pady=(0, 6))
        ttk.Button(row, text="Choose ISO / XBLA package / default.xex…", command=self.on_pick_file).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Choose folder (GoD / extracted)…", command=self.on_pick_folder).pack(side="left")
        ttk.Label(t, text="Files are unpacked into  assets\\  next to the exe. Drop-in also works: copy the game folder\n"
                          "(default.xex + everything beside it) into assets\\ yourself.", style="Muted.TLabel",
                  justify="left").pack(anchor="w", pady=(4, 8))
        self.prog = ttk.Progressbar(t, mode="indeterminate")
        self.prog.pack(fill="x", pady=(0, 6))
        self.logbox = tk.Text(t, height=12, bg="#0b0d13", fg="#b9bfcc", insertbackground=self.FG, bd=0,
                              font=("Consolas", 9), wrap="word", state="disabled")
        self.logbox.pack(fill="both", expand=True)

    def _build_gfx(self):
        t = self.tab_gfx
        s = self.s
        self.v_preset = tk.StringVar(value=s["preset"])
        self.v_scale = tk.StringVar(value=str(s["resolution_scale"]))
        self.v_vsync = tk.BooleanVar(value=bool(s["vsync"]))
        self.v_fps = tk.StringVar(value=str(s["fps_cap"]))
        self.v_eff = tk.StringVar(value=s["present_effect"])
        self.v_mode = tk.StringVar(value=s["video_mode"])
        self.v_lb = tk.BooleanVar(value=bool(s["letterbox"]))
        self.v_fs = tk.BooleanVar(value=bool(s["fullscreen"]))
        self.v_tol = tk.BooleanVar(value=bool(s["tolerant"]))

        def row(r, label, widget):
            ttk.Label(t, text=label, style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=4, padx=(0, 12))
            widget.grid(row=r, column=1, sticky="w", pady=4)

        row(0, "Preset", ttk.Combobox(t, textvariable=self.v_preset, values=PRESETS, state="readonly", width=16))
        row(1, "Render scale", ttk.Combobox(t, textvariable=self.v_scale, values=["1", "2", "3"], state="readonly", width=6))
        row(2, "FPS cap", ttk.Combobox(t, textvariable=self.v_fps, values=FPS_CAPS, state="readonly", width=6))
        row(3, "Upscaler", ttk.Combobox(t, textvariable=self.v_eff, values=EFFECTS, state="readonly", width=10))
        row(4, "Guest video mode", ttk.Combobox(t, textvariable=self.v_mode, values=VIDEO_MODES, width=10))
        row(5, "V-Sync", ttk.Checkbutton(t, variable=self.v_vsync))
        row(6, "Letterbox (keep aspect)", ttk.Checkbutton(t, variable=self.v_lb))
        row(7, "Fullscreen", ttk.Checkbutton(t, variable=self.v_fs))
        row(8, "Tolerant dispatcher", ttk.Checkbutton(t, variable=self.v_tol))
        ttk.Label(t, text="1 = native 720p (fastest); 2 = 1440p; 3 = 2160p.  Tolerant = log-and-return on an\n"
                          "unregistered indirect call instead of crashing (recommended).", style="Muted.TLabel",
                  justify="left").grid(row=9, column=0, columnspan=2, sticky="w", pady=(6, 8))
        ttk.Label(t, text="Extra cvars (key=value per line)", style="Card.TLabel").grid(row=10, column=0, columnspan=2, sticky="w")
        self.extra = tk.Text(t, height=5, bg="#0b0d13", fg="#b9bfcc", insertbackground=self.FG, bd=0, font=("Consolas", 9))
        self.extra.insert("1.0", s.get("extra", ""))
        self.extra.grid(row=11, column=0, columnspan=2, sticky="nsew", pady=(2, 8))
        t.grid_columnconfigure(1, weight=1)
        t.grid_rowconfigure(11, weight=1)
        ttk.Button(t, text="Save settings", command=self.collect_and_save).grid(row=12, column=0, sticky="w")
        ttk.Button(t, text="Open userdata folder", command=lambda: os.startfile(USERDATA) if os.path.isdir(USERDATA) else None
                   ).grid(row=12, column=1, sticky="w")

    # ---------------------------------------------------------------- actions
    def collect_and_save(self):
        try:
            self.s.update({
                "preset": self.v_preset.get(), "resolution_scale": int(self.v_scale.get()),
                "vsync": self.v_vsync.get(), "fps_cap": int(self.v_fps.get() or 0),
                "present_effect": self.v_eff.get(), "video_mode": self.v_mode.get().strip() or "1280x720",
                "letterbox": self.v_lb.get(), "fullscreen": self.v_fs.get(), "tolerant": self.v_tol.get(),
                "extra": self.extra.get("1.0", "end").strip(),
            })
            save_settings(self.s)
            self.set_status("settings saved")
        except Exception as ex:
            messagebox.showerror("Settings", str(ex))

    def log(self, msg):
        def _do():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", str(msg) + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, _do)

    def set_status(self, msg):
        self.after(0, lambda: self.status.configure(text=msg))

    def refresh(self):
        root = find_game_root()
        if root:
            self.game_lbl.configure(text="✅ Game files: %s" % os.path.relpath(root, HERE))
            self.play_btn.state(["!disabled"])
        else:
            self.game_lbl.configure(text="No game files yet. Pick your dump below -- it unpacks itself into assets\\.")
            self.play_btn.state(["disabled"])
        if not self.info.get("name"):
            self.set_status("game exe not found next to the launcher")

    def on_pick_file(self):
        p = filedialog.askopenfilename(title="Xbox 360 dump", filetypes=[
            ("Xbox 360 game", "*.iso *.xex *.zip *"), ("All files", "*")])
        if p:
            self.start_extract(p)

    def on_pick_folder(self):
        p = filedialog.askdirectory(title="GoD package or extracted game folder")
        if p:
            self.start_extract(p)

    def start_extract(self, src):
        if _extract is None:
            messagebox.showerror("Extract", "extract.py is missing next to the launcher")
            return
        self.prog.start(12)
        self.set_status("unpacking…")
        threading.Thread(target=self._extract, args=(src,), daemon=True).start()

    def _extract(self, src):
        try:
            os.makedirs(ASSETS, exist_ok=True)
            if os.path.isdir(src) and _extract._find_default_xex(src):
                # extracted folder: copy in (the exe must find it beside itself)
                import shutil
                xex = _extract._find_default_xex(src)
                gdir = os.path.dirname(xex)
                self.log("copying game folder %s -> assets\\" % gdir)
                for r, _d, files in os.walk(gdir):
                    rel = os.path.relpath(r, gdir)
                    dst = os.path.normpath(os.path.join(ASSETS, rel))
                    os.makedirs(dst, exist_ok=True)
                    for fn in files:
                        shutil.copy2(os.path.join(r, fn), os.path.join(dst, fn))
            else:
                _extract.extract_container(src, ASSETS, log=self.log)
            # cover from an STFS header if we have none yet
            try:
                if not os.path.isfile(os.path.join(HERE, "cover.png")):
                    meta = _extract.read_package_meta(src) or {}
                    if meta.get("cover"):
                        open(os.path.join(HERE, "cover.png"), "wb").write(meta["cover"])
            except Exception:
                pass
            self.log("done.")
            self.set_status("game files ready")
        except SystemExit as ex:
            self.log("ERROR: %s" % ex)
            self.set_status("extract failed")
        except Exception as ex:
            self.log("ERROR: %s\n%s" % (ex, traceback.format_exc()))
            self.set_status("extract failed")
        finally:
            self.after(0, self.prog.stop)
            self.after(0, self.refresh)

    def on_play(self):
        self.collect_and_save()
        try:
            launch(self.info, self.s, self.log)
            self.set_status("running… (log: userdata\\)")
        except Exception as ex:
            messagebox.showerror("Play", str(ex))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--cvars":  # debug helper
        print(json.dumps(cvars_from(load_settings()), indent=2))
        return
    App().mainloop()


if __name__ == "__main__":
    main()
