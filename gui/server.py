#!/usr/bin/env python3
"""
rexauto GUI server — serves the web UI and drives the pipeline, streaming stage
status and log lines to the browser over Server-Sent Events.

    python gui/server.py            # opens http://127.0.0.1:7575 in your browser

Routes:
    GET  /                      the UI
    GET  /api/meta?container=   {title, title_id, cover}  (read from the package)
    POST /api/start             {container, name, run}    start the pipeline
    POST /api/stop              stop a running pipeline
    GET  /api/events            SSE stream of {type, ...} events
"""
import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE, getattr(sys, "_MEIPASS", "")):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)
import extract as _extract
import gamepatches as _gamepatches  # noqa: E402
import setup as _setup  # noqa: E402

STAGES = ["extract", "init", "setjmp", "jumptables", "build", "runheal", "run"]
PORT = int(os.environ.get("PORT") or os.environ.get("REXAUTO_GUI_PORT") or "7575")
FROZEN = getattr(sys, "frozen", False)


def index_path():
    base = getattr(sys, "_MEIPASS", HERE)
    for p in (os.path.join(HERE, "index.html"), os.path.join(base, "gui", "index.html"),
              os.path.join(base, "index.html")):
        if os.path.exists(p):
            return p
    return os.path.join(HERE, "index.html")


def _port_dir(name):
    """Onde o port deste projeto mora -- mesma conta que o Ctx do rexauto faz."""
    work = os.environ.get("REXAUTO_WORK", r"C:\Skate3\autoports")
    return os.path.join(work, name, "port")


def pipeline_command(container, name, do_run, patches=None):
    """Frozen app re-invokes itself in pipeline mode; a script run calls rexauto.py."""
    if FROZEN:
        cmd = [sys.executable, "--__pipeline", container, "--name", name]
    else:
        cmd = [sys.executable, "-u", os.path.join(ROOT, "rexauto.py"), container, "--name", name]
    if do_run:
        cmd.append("--run")
    # A selecao da tela e a verdade completa: mandar --no-patches quando ela esta
    # vazia e o que faz DESmarcar na GUI voltar o port ao estado sem patch.
    if patches:
        for n in patches:
            cmd += ["--patch", n]
    elif patches is not None:
        cmd.append("--no-patches")
    return cmd


def _cover_data_url(meta):
    """PNG data URL for the target's cover: the embedded STFS thumbnail, or -- since
    Xbox 360 discs (ISO/GoD/folder) don't embed cover art -- the XboxUnity tile
    fetched by title_id (best-effort, cached, offline-safe)."""
    png = meta.get("cover")
    if not png and meta.get("title_id"):
        try:
            png = _extract.fetch_title_icon(meta["title_id"])
        except Exception:
            png = None
    return ("data:image/png;base64," + base64.b64encode(png).decode()) if png else None


class Hub:
    """Fan-out of pipeline events to all connected SSE clients + run control."""

    def __init__(self):
        self.subs = []
        self.lock = threading.Lock()
        self.proc = None
        self.history = []
        self.stage_idx = -1

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subs.append(q)
            snapshot = list(self.history)
        for ev in snapshot:
            q.put(ev)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def emit(self, ev):
        with self.lock:
            # keep a bounded replay buffer so a late tab catches up
            if ev.get("type") in ("meta", "stage", "status", "done"):
                self.history.append(ev)
                self.history = self.history[-400:]
            subs = list(self.subs)
        for q in subs:
            q.put(ev)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, container, name, do_run, patches=None):
        if self.running():
            self.emit({"type": "log", "level": "warn", "text": "a pipeline is already running"})
            return
        self.history = []
        self.stage_idx = -1
        meta = _extract.read_package_meta(container)
        cover = _cover_data_url(meta)
        self.emit({"type": "meta", "title": meta.get("title") or name,
                   "title_id": meta.get("title_id"), "cover": cover, "format": meta.get("format"),
                   "container": container, "name": name})
        self.emit({"type": "stage", "stage": "extract", "status": "pending"})
        threading.Thread(target=self._run, args=(container, name, do_run, patches), daemon=True).start()

    def stop(self):
        if self.running():
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.emit({"type": "log", "level": "warn", "text": "stopped by user"})
            self.emit({"type": "done", "ok": False, "message": "stopped"})

    def _run(self, container, name, do_run, patches=None):
        cmd = pipeline_command(container, name, do_run, patches)
        self.emit({"type": "status", "text": "starting pipeline"})
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1,
                                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        except Exception as ex:
            self.emit({"type": "done", "ok": False, "message": "launch failed: %s" % ex})
            return
        proc = self.proc
        ok = True
        try:
            for line in proc.stdout:
                self._parse(line.rstrip("\n"))
                if "Traceback (most recent call last)" in line or "did not converge" in line:
                    ok = False
                if "SDK MISMATCH" in line:
                    # A rexglue/ left by an older release. Setup replaces it;
                    # nothing else in the GUI does.
                    self.emit({"type": "log", "level": "warn",
                               "text": "the ReXGlue SDK installed here is not the one this "
                                       "release was tested with -> Setup -> ReXGlue SDK"})
            if proc.wait() != 0:
                ok = False
        except Exception:
            ok = False
        finally:
            if self.proc is proc:
                self.proc = None  # clear so the next game can start (only if still ours)
        # mark the active stage done if the run ended cleanly
        if ok and 0 <= self.stage_idx < len(STAGES):
            self.emit({"type": "stage", "stage": STAGES[self.stage_idx], "status": "done"})
        self.emit({"type": "done", "ok": ok,
                   "message": "build ready" if ok else "stopped (see log)"})

    def _parse(self, line):
        text = line
        tag = "[rexauto] "
        if text.startswith(tag):
            body = text[len(tag):]
            if body.startswith("=== stage: "):
                stage = body[len("=== stage: "):].rstrip(" =").strip()
                if stage in STAGES:
                    if 0 <= self.stage_idx < len(STAGES):
                        self.emit({"type": "stage", "stage": STAGES[self.stage_idx], "status": "done"})
                    self.stage_idx = STAGES.index(stage)
                    self.emit({"type": "stage", "stage": stage, "status": "running"})
                    self.emit({"type": "status", "text": _STAGE_HEADLINE.get(stage, stage)})
                return
            if body.startswith("skip ") and "(done)" in body:
                st = body.split()[1]
                if st in STAGES:
                    self.emit({"type": "stage", "stage": st, "status": "done"})
                return
            if body.startswith("@"):                 # live progress, headline only
                prog = body[1:]
                self.emit({"type": "status", "text": prog})
                m = re.search(r"(\d+)\s*/\s*(\d+)", prog)   # any N/M -> progress bar
                if m:
                    self.emit({"type": "buildprogress",
                               "done": int(m.group(1)), "total": int(m.group(2))})
                return
            # headline-worthy status lines
            self.emit({"type": "status", "text": body})
            lvl = "info"
            low = body.lower()
            if "fail" in low or "error" in low or "could not" in low or "warning" in low:
                lvl = "warn"
            if "ok" in low or "recovered" in low or "converged" in low or "registered" in low:
                lvl = "good"
            self.emit({"type": "log", "level": lvl, "text": body})
            return
        # raw tool output (rexglue / cmake / ida)
        if text.strip():
            self.emit({"type": "log", "level": "dim", "text": text})


_STAGE_HEADLINE = {
    "extract": "Extracting game from container",
    "init": "Scaffolding ReXGlue project",
    "setjmp": "Detecting setjmp/longjmp",
    "jumptables": "Recovering jump tables (IDA)",
    "build": "Recompiling + self-healing boundaries",
    "runheal": "Booting + registering functions",
    "run": "Launching",
}

HUB = Hub()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(index_path(), "rb") as f:
                return self._send(200, "text/html; charset=utf-8", f.read())
        if u.path == "/api/meta":
            q = parse_qs(u.query)
            container = (q.get("container") or [""])[0]
            meta = _extract.read_package_meta(container)
            cover = _cover_data_url(meta)
            return self._send(200, "application/json",
                              json.dumps({"title": meta.get("title"),
                                          "title_id": meta.get("title_id"), "cover": cover,
                                          "format": meta.get("format")}))
        if u.path == "/api/patches":
            q = parse_qs(u.query)
            name = (q.get("name") or [""])[0].strip()
            if not name:
                return self._send(200, "application/json",
                                  json.dumps({"error": "sem nome de projeto", "patches": []}))
            # Some titles publish several catalogue files for the same ID (Forza
            # Horizon ships retail and an "(E3 Demo)" one) and the addresses are
            # not interchangeable. Picking one here remembers it next to the port.
            variant = (q.get("variant") or [""])[0].strip()
            if variant:
                _gamepatches.write_variant(_port_dir(name), variant)
            return self._send(200, "application/json",
                              json.dumps(_gamepatches.catalog(_port_dir(name))))
        if u.path == "/api/deps":
            return self._send(200, "application/json", json.dumps({"items": _setup.deps_status()}))
        if u.path == "/api/events":
            return self._sse()
        return self._send(404, "text/plain", "not found")

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if u.path == "/api/start":
            container = data.get("container", "").strip().strip('"')
            name = (data.get("name") or "").strip()
            if not name or name == "game":
                # Derive a real project name from the package title / file name
                # instead of the generic 'game'.
                meta = _extract.read_package_meta(container)
                name = _extract.project_name_from_title(
                    meta.get("title") or _extract.title_from_filename(container))
            # Refuse to start a run that cannot finish. Without this the UI
            # happily ran extract/xctd/init/setjmp on a machine with no
            # toolchain -- minutes of work, and on a title with transparent
            # compression a rewritten game folder -- before dying at the build.
            blocked = [d for d in _setup.deps_status()
                       if not d["found"] and d["key"] in
                       ("rexglue", "clang", "vcvars")]
            if blocked:
                for d in blocked:
                    HUB.emit({"type": "log", "level": "err",
                              "text": "missing: %s -- %s" % (d["name"], d["note"])})
                HUB.emit({"type": "log", "level": "err",
                          "text": "not starting. Open Setup and install the %d "
                                  "missing tool(s); nothing has been written."
                                  % len(blocked)})
                return self._send(200, "application/json", json.dumps(
                    {"ok": False, "missing": [d["key"] for d in blocked]}))
            for d in _setup.deps_status():
                if not d["found"] and d["key"] in ("python", "idat"):
                    HUB.emit({"type": "log", "level": "warn",
                              "text": "%s missing -- jump-table recovery will be "
                                      "SKIPPED and this title loses static bctr "
                                      "recovery entirely" % d["name"]})
            # Same warning the CLI preflight gives: a small pagefile plus a game or a
            # browser open beside the pipeline starves codegen, and it dies as exit
            # 0xE06D7363 with nothing in the log. Forza Horizon lost four builds in
            # six minutes to Hogwarts Legacy running in the background.
            try:
                import rexauto as _rx
                _mem = _rx._commit_warning(_rx._commit_state())
                if _mem:
                    HUB.emit({"type": "log", "level": "warn", "text": "memory: " + _mem})
            except Exception:
                pass
            HUB.start(container, name, bool(data.get("run")), data.get("patches"))
            return self._send(200, "application/json", json.dumps({"ok": True}))
        if u.path == "/api/patches":
            name = (data.get("name") or "").strip()
            wanted = data.get("patches") or []
            variant = (data.get("variant") or "").strip()
            if variant:
                _gamepatches.write_variant(_port_dir(name), variant)
            try:
                r = _gamepatches.apply(_port_dir(name), wanted)
            except Exception as e:
                return self._send(200, "application/json",
                                  json.dumps({"ok": False, "error": str(e)}))
            if r["needs_rebuild"]:
                HUB.emit({"type": "log", "level": "warn",
                          "text": "patch(es) gravado(s), mas escrevem em .text -- "
                                  "rode o pipeline de novo para virarem codigo nativo"})
            r["ok"] = True
            return self._send(200, "application/json", json.dumps(r))
        if u.path == "/api/stop":
            HUB.stop()
            return self._send(200, "application/json", json.dumps({"ok": True}))
        if u.path == "/api/setup":
            target = data.get("target", "all")
            threading.Thread(target=_setup.run, args=(target, HUB.emit), daemon=True).start()
            return self._send(200, "application/json", json.dumps({"ok": True}))
        return self._send(404, "text/plain", "not found")

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = HUB.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(("data: %s\n\n" % json.dumps(ev)).encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)


def make_server():
    """Bind the first free port from PORT upward; returns (server, port)."""
    last = None
    for p in range(PORT, PORT + 30):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError as ex:
            last = ex
            continue
        # app.py (the WebView2 window) calls this and never main(): the SDK
        # check has to live here or the frozen exe skips it -- which is
        # exactly what the first 2.36.1 build did.
        threading.Thread(target=_install_sdk_if_needed, daemon=True).start()
        return srv, p
    raise SystemExit("no free port for the GUI server: %s" % last)


def _install_sdk_if_needed():
    """Setup, unasked: a missing ReXGlue SDK, or one from another release, is
    replaced with this release's before the user can start anything. The only
    manual step left is the one that needs a licence (IDA)."""
    row = next((d for d in _setup.deps_status() if d["key"] == "rexglue"), None)
    if not row or row["found"]:
        return
    HUB.emit({"type": "log", "level": "warn",
              "text": "ReXGlue SDK: %s" % row["detail"]})
    HUB.emit({"type": "log", "level": "info",
              "text": "installing the ReXGlue SDK this release was tested with..."})
    _setup.run("rexglue", HUB.emit)


def main():
    srv, port = make_server()
    url = "http://127.0.0.1:%d" % port
    print("rexauto GUI -> %s" % url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
