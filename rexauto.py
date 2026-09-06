#!/usr/bin/env python3
"""
rexauto — one shot: Xbox 360 content container -> a recompiled build that boots.

Drop in a container (or an extracted game folder) and it runs the whole pipeline
that otherwise takes a day of by-hand steps:

  1. extract      container -> game folder (default.xex + assets)
  2. init         scaffold a ReXGlue project
  3. jumptables   (if IDA present) recover bctr jump tables -> switch_tables.toml
  4. build+heal   codegen + build; auto-extend any function the recompiler split
                  mid-flow until the build is clean (boundary heal)
  5. run+heal     run; register every "invalid/unregistered function" the
                  dispatcher hits, rebuild, repeat until it stops hitting them
  6. run          launch it

What it does NOT do: fix game-specific GPU/emulation gaps (a title whose vertex
formats or kernel calls the runtime doesn't support yet will boot and run but may
not render or stay up). That is runtime work, not recompilation. rexauto gets you
to a booting, guest-code-executing build automatically; the rest is per title.

    python rexauto.py "<container-or-folder>" --name mygame [--run]

Re-run any time: each stage is skipped if already done. --from <stage> restarts
from a point, --only <stage> runs one. Tool paths come from their usual install
locations, PATH, or the env vars REXGLUE / REXSDK_DIR / IDAT / CLANG / CLANGXX /
VCVARS / JT_REPO.
"""
import argparse
import copy
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import closure as _closure
import extract as _extract
import gamepatches as _gamepatches
import launcher as _launcher
import heal as _heal
import jt_landings as _jt
import codegen_patches as _cgp
import deepextract as _dx

STAGES = ["extract", "xctd", "init", "setjmp", "jumptables", "deepextract", "build", "runheal", "run"]
MAX_BUILD_ATTEMPTS = 12


# --------------------------------------------------------------------------- env
def find_first(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def newest_glob(*patterns):
    hits = []
    for pat in patterns:
        hits.extend(glob.glob(pat))
    return sorted(hits)[-1] if hits else None


_PY_OK_CACHE = {}


def _python_runs(path):
    """True if `path` is really a Python interpreter, proven by running it.

    Not a formality on Windows: the App-execution-alias stub at
    %LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe exists, is on PATH ahead of
    a real install, and answers every launch with "Python was not found..." and
    exit 9009."""
    if not path:
        return False
    if path in _PY_OK_CACHE:
        return _PY_OK_CACHE[path]
    ok = False
    try:
        r = subprocess.run([path, "-c", "import sys; print(sys.version_info[0])"],
                           capture_output=True, text=True, timeout=20)
        ok = r.returncode == 0 and (r.stdout or "").strip().isdigit()
    except Exception:
        ok = False
    _PY_OK_CACHE[path] = ok
    return ok


def _first_working_python(*candidates):
    for c in candidates:
        if c and os.path.exists(c) and _python_runs(c):
            return c
    return None


def detect_env():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    e = os.environ.get
    # When shipped as a packaged release, ReXGlue and the jump-table scripts sit
    # next to the .exe under rexglue/ and xenon-jumptables/. Those take priority.
    app = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else HERE
    def near(*rel):
        return find_first([os.path.join(app, *r.split("/")) for r in rel])
    return {
        "vcvars": e("VCVARS") or newest_glob(
            os.path.join(pf86, "Microsoft Visual Studio", "*", "*", "VC", "Auxiliary",
                         "Build", "vcvars64.bat")),
        "clang": e("CLANG") or shutil.which("clang") or find_first(
            [os.path.join(pf, "LLVM", "bin", "clang.exe")]),
        "clangxx": e("CLANGXX") or shutil.which("clang++") or find_first(
            [os.path.join(pf, "LLVM", "bin", "clang++.exe")]),
        "idat": e("IDAT") or shutil.which("idat") or newest_glob(
            os.path.join(pf, "IDA*", "idat.exe"), os.path.join(pf, "IDA*", "idat64.exe")),
        "sdk": e("REXSDK_DIR") or near("rexglue/sdk")
        or find_first([r"C:\Skate3\rexglue-sdk\out\install\win-amd64",
                       r"C:\Skate3Recomp\rexglue-sdk\out\install\win-amd64"]),
        "rexglue": e("REXGLUE") or near("rexglue/tool/rexglue.exe") or shutil.which("rexglue")
        or newest_glob(r"C:\Skate3\rexglue-sdk\out\win-amd64\*\rexglue.exe",
                       r"C:\Skate3Recomp\rexglue-sdk\out\win-amd64\*\rexglue.exe"),
        # xenon-jumptables ships INSIDE rexauto (vendor/, --add-data'd into the
        # frozen exe and unpacked to sys._MEIPASS). It is not optional in
        # practice: every bctr table it recovers statically is an indirect target
        # resolved at codegen time instead of the run-heal finding it by launching
        # the game and crashing on it. It used to have to be cloned by hand, so
        # the stage recorded {"skipped": "no-repo"} and pushed the whole class
        # onto the play-and-heal loop. JT_REPO still overrides, for a working copy.
        "jt_repo": e("JT_REPO") or near("xenon-jumptables")
        or find_first([p for p in (
            os.path.join(getattr(sys, "_MEIPASS", ""), "xenon-jumptables"),
            os.path.join(HERE, "vendor", "xenon-jumptables"),
            r"C:\xenon-jumptables") if p and os.path.basename(p)]),
        # A real python interpreter for the jump-table scripts (sys.executable is
        # the frozen .exe when packaged, which can't run .py files).
        #
        # Every candidate is RUN before it is accepted. Windows ships an
        # "App execution alias" at %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe
        # that is not an interpreter at all -- it is a redirector that prints
        # "Python was not found..." and exits 9009 -- and shutil.which() finds it
        # first on a machine with a real Python installed elsewhere. Accepting it
        # made the frozen build report Python as present and then fail the
        # jumptables stage with a bare "extract_funcs failed -> skipping jump
        # tables", losing static bctr recovery on every title, silently.
        "python": _first_working_python(
            (None if getattr(sys, "frozen", False) else sys.executable),
            e("PYTHON"), shutil.which("python"), shutil.which("python3"),
            newest_glob(r"C:\Program Files\Python3*\python.exe",
                        r"C:\Python3*\python.exe")),
    }


# ------------------------------------------------------------------------- utils
class Ctx:
    def __init__(self, args, env):
        self.args = args
        self.env = env
        self.name = args.name
        self.work = os.path.join(args.work, args.name)
        os.makedirs(self.work, exist_ok=True)
        self.port = os.path.join(self.work, "port")
        self.manifest = os.path.join(self.port, "%s_manifest.toml" % self.name)
        self.functions = os.path.join(self.port, "%s_functions.toml" % self.name)
        self.switches = os.path.join(self.port, "%s_switch_tables.toml" % self.name)
        self.forced = os.path.join(self.port, "%s_forced_landings.toml" % self.name)
        self.builddir = os.path.join(self.port, "out", "build", "win-amd64-release")
        self.exe = os.path.join(self.builddir, "%s.exe" % self.name)
        self.gen = os.path.join(self.port, "generated", "default")
        self.statefile = os.path.join(self.work, ".rexauto_state")
        self._game_out = os.path.join(self.work, "game")
        ex = self.load_state().get("extract") or {}
        self.game = ex.get("game_dir") or self._game_out
        self.xex = ex.get("xex")
        # auto-title-update: the staged .xexp delta (None for a base-only game, so no
        # behaviour change). codegen+runtime auto-apply it in memory; gabarito_key
        # folds it in so a TU build keeps its own cure set.
        self.tu_xexp = ex.get("tu_xexp")
        # --- timing instrument (rationale in the block below this class) ------
        # The frame stack is allocated ONCE, here, because _module_view hands a
        # companion module a copy.copy(ctx): a per-object stack would give the
        # module its own empty one, its frames would never be subtracted from the
        # enclosing stage's child_s, and `build` would silently re-absorb the
        # companion IDA minutes this instrument exists to split out.
        self._t_stack = []
        self._t_sink = os.path.join(self.work, ".rexauto_timings.jsonl")
        self._t_run = {
            # pid alone is not a unique run id (Windows recycles them, and a
            # resumed pipeline can start in the same second as the one it
            # resumes); the random tail makes "which run measured this stage"
            # answerable without ambiguity, which is the whole point of stamping
            # the run id next to every number.
            "run": "%sZ-%d-%s" % (time.strftime("%Y%m%dT%H%M%S", time.gmtime()),
                                  os.getpid(), os.urandom(2).hex()),
            "started_utc": _utcnow(), "t0": time.perf_counter(), "seq": 0, "done": 0,
            "header": False, "skipped": [], "host": _host_facts(), "argv": sys.argv[1:],
            # Cold vs warm is not a judgement call: the two things that actually
            # decide it are whether the IDA cache was disabled for this run and
            # whether the port already had a checkpoint to resume from. A stage
            # duration without those two cannot be compared against another run's,
            # and an incomparable number is what produces a wrong A/B verdict.
            "no_ida_cache": bool(os.environ.get("REXAUTO_NO_IDA_CACHE")),
            "resumed": os.path.exists(self.statefile),
        }

    def log(self, msg):
        print("[rexauto] %s" % msg, flush=True)

    def load_state(self):
        try:
            return json.load(open(self.statefile)) if os.path.exists(self.statefile) else {}
        except Exception:
            return {}

    def mark(self, stage, data=None):
        st = self.load_state()
        st[stage] = data if data is not None else True
        # Atomic: sibling tmp, fsync, os.replace. The old truncate-then-write
        # (json.dump straight into open(path, "w")) leaves the checkpoint EMPTY for the
        # width of the write, and load_state swallows a parse failure into {} -- so a
        # crash, a kill, or a full disk mid-mark silently costs the port every finished
        # stage, and the next run re-extracts, re-analyses and rebuilds from scratch
        # with no error to explain why. The timings member fattened the file, widening
        # exactly that window. os.replace is atomic on NTFS.
        tmp = self.statefile + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.statefile)

    # --- timing instrument -------------------------------------------------
    def timer(self, stage, phase=None, module=None):
        """Open a timed frame around a stage. Used at CALL SITES, never inside the
        stage functions themselves: stage_build is entered both from main()'s loop
        and from inside stage_runheal's companion auto-detect, and stage_jumptables
        / stage_deepextract are entered both for the entrypoint and for every
        companion module. A frame opened inside the function could not tell those
        entries apart, and the two `build` numbers would be reported as one."""
        return _TimingFrame(self, stage, phase, module)

    def t_op(self, kind, bucket=None, **fields):
        """Time one costly sub-operation: a codegen pass, a build attempt, an IDA
        invocation, a game launch, the .i64 copy. Charges <bucket>_s / <bucket>_n
        onto the innermost open frame AND writes its own sidecar row -- the bucket
        answers "where did this stage's seconds go", the row answers "WHICH attempt
        was the slow one", and in a 12-attempt build ladder or a 20-round heal loop
        those are not the same question."""
        return _TimingOp(self, kind, bucket, fields)

    def t_note(self, **kw):
        """Attach cold/warm context to the innermost open frame (IDA cache hit,
        checkpoint reuse, receipt hit, CMakeCache present). A stage duration with
        no such context is unusable for an A/B: comparing a cache-hit run against
        a cache-miss run and calling the difference a win is the exact mistake the
        measurement protocol forbids."""
        try:
            if self._t_stack:
                self._t_stack[-1]["cold"].update(kw)
        except Exception:
            pass

    def timing_skip(self, stage, status="skip-done", **fields):
        """Record a stage this run did NOT run. Deliberately writes no wall_s: a
        0.0 here would read as "this stage is free", and a fabricated zero is the
        same class of guessed answer the recompiler refuses to emit."""
        if not _timings_enabled():
            return
        try:
            depth = len(self._t_stack)
            if depth == 0:
                # Only main()'s own loop feeds the run summary. A companion
                # module's one-shot skip is a nested event: counting it as a
                # pipeline stage would make a run that died halfway report itself
                # "complete" on FIFA Street and never on joust.
                self._t_run["skipped"].append(stage)
                self._t_run["done"] += 1
            rec = dict(fields)
            rec.update({"stage": stage, "status": status, "depth": depth})
            self._timings_emit("skip", rec)
        except Exception:
            pass

    def timing_run_end(self, selected=None):
        """Close out the run with ONE state write carrying the total wall clock and
        the stages this run skipped. Called from a finally, so a stage that raises
        still leaves a record -- marked "partial", because a total that silently
        omits the stage that blew up is a lie about what the run cost."""
        if not _timings_enabled():
            return
        try:
            total = time.perf_counter() - self._t_run["t0"]
            n = len(selected) if selected is not None else None
            self._timings_state({
                "last": {"run": self._t_run["run"], "at": self._t_run["started_utc"],
                         "total_s": round(total, 3),
                         "status": "complete" if (n is not None and self._t_run["done"] >= n)
                                   else "partial",
                         "stages_selected": list(selected) if selected is not None else None,
                         "skipped": list(self._t_run["skipped"]),
                         "cold": {"no_ida_cache": self._t_run["no_ida_cache"],
                                  "resumed_checkpoint": self._t_run["resumed"]}}})
        except Exception:
            pass

    def _timings_emit(self, kind, rec):
        """One JSON object per line, opened 'a' and closed per record. Append-only
        is the point: an append cannot endanger bytes already written, so a GUI stop
        or a Ctrl-C mid-stage costs at most the row in flight -- unlike the state
        file, which is truncated before it is rewritten."""
        try:
            r = self._t_run
            if not r["header"]:
                r["header"] = True
                hdr = {"kind": "run", "run": r["run"], "seq": 0, "name": self.name,
                       "started_utc": r["started_utc"], "host": r["host"], "argv": r["argv"],
                       "work": self.work, "schema": TIMINGS_SCHEMA,
                       "cold": {"no_ida_cache": r["no_ida_cache"],
                                "resumed_checkpoint": r["resumed"]}}
                with open(self._t_sink, "a", encoding="utf-8") as f:
                    f.write(json.dumps(hdr) + "\n")
            r["seq"] += 1
            row = dict(rec)
            row.update({"kind": kind, "run": r["run"], "seq": r["seq"], "name": self.name})
            with open(self._t_sink, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass

    def _timings_rollup(self, rec):
        """Publish one closed OUTERMOST frame into .rexauto_state["timings"].

        Only the outermost frame for a stage lands here. stage_build is re-entered
        from inside stage_runheal (companion auto-detect), and publishing that
        runheal-internal relink under the key "build" would be a number that
        contradicts its own name -- the nested frames stay in the sidecar, where
        `phase` and `depth` keep them distinguishable.

        A stage timed by an EARLIER run is never overwritten by a later run that
        skipped it: each entry carries the run id that measured it, so a resumed
        pipeline shows last week's real seconds tagged with last week's run rather
        than this run's zero."""
        try:
            self._timings_state({"stages": {rec["stage"]: {
                "status": rec["status"], "wall_s": rec["wall_s"], "self_s": rec["self_s"],
                "child_s": rec["child_s"], "sub": rec["sub"] or None, "cold": rec["cold"] or None,
                "run": self._t_run["run"], "at": rec["t_start_utc"]}}})
        except Exception:
            pass

    def _timings_state(self, patch):
        """Merge a small patch into the single new top-level key "timings".

        Collision-safe by construction: main()'s resume test is state.get(stage)
        for stage in STAGES, "timings" is not a stage, and no code path anywhere
        enumerates this file's keys -- build_parallel is the existing precedent for
        a non-stage key living here.

        Written tmp + os.replace rather than through mark(): mark() truncates the
        file before it rewrites it and load_state() returns {} on ANY exception, so
        a torn write silently converts a fully checkpointed port into a from-scratch
        re-run. An instrument that adds writes to that file without adding that
        guarantee would be buying measurement with checkpoint risk. os.replace only
        ever swaps in a fully-written temp file, so the worst case here is a lost
        timing row, never a lost checkpoint."""
        st = self.load_state()
        t = st.get("timings")
        if not isinstance(t, dict) or t.get("schema") != TIMINGS_SCHEMA:
            t = {"schema": TIMINGS_SCHEMA, "stages": {}}
        t["host"] = self._t_run["host"]
        t["sink"] = os.path.basename(self._t_sink)
        for k, v in patch.items():
            if k == "stages":
                t.setdefault("stages", {}).update(v)
            else:
                t[k] = v
        st["timings"] = t
        tmp = self.statefile + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(st, f, indent=1)
            os.replace(tmp, self.statefile)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


# --------------------------------------------------------------------- timings
# Nothing in this pipeline ever recorded a duration. `time` appeared only as a
# DEADLINE -- do_build's 0.3s progress throttle, run_once's launch window -- and
# not one subtraction was ever stored, so "the IDA pass dominates" and "the heal
# loop is the expensive part" were folklore: unfalsifiable, and by the loop
# charter's own rule (optimizing an unmeasured stage is forbidden) they blocked
# every speed change behind them. This is that instrument. It only ADDS: one new
# top-level state key, one new sidecar file, and stamps that are all no-ops under
# REXAUTO_TIMINGS=0.
#
# WHY IT IS A STACK and not a wrapper around main()'s stage loop:
#   stage_build -> setup_extra_modules -> _codegen_module runs a companion
#   module's ENTIRE IDA pipeline (stage_jumptables + stage_deepextract + two
#   codegens) inside the `build` stage, and stage_runheal re-enters the whole of
#   stage_build on companion auto-detect. A flat timer bills fifadllzf's serial
#   IDA minutes to "clang" and lets the re-entrant build's stamp land on top of
#   the real one. Every frame therefore reports wall_s (inclusive) AND self_s
#   (exclusive of its children), and carries phase/module/depth so two `build`
#   rows are two rows instead of one overwriting the other.
#
# WHY A SKIPPED STAGE IS NOT 0.0s:
#   a zero that means "we never ran it" is a fabricated measurement, and it would
#   poison exactly the comparison the ledger exists for -- a warm re-run would
#   look like a pipeline that got 8x faster. Checkpoint skips record status only,
#   with no wall_s key at all.
#
# WHY EVERY STAMP IS SWALLOWED:
#   the instrument may never be the reason a stage fails. Every sink write, every
#   note and every roll-up is inside try/except, exactly as load_state() already
#   swallows its own reader errors.
TIMINGS_SCHEMA = 1


def _timings_enabled():
    """REXAUTO_TIMINGS=0 turns every stamp into a no-op, so the instrument's own
    overhead is measurable by A/B instead of asserted (it is UNMEASURED until that
    A/B is actually run)."""
    return os.environ.get("REXAUTO_TIMINGS", "1").lower() not in ("0", "no", "off", "false")


def _utcnow():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _host_facts():
    """Read the box at runtime; never write a literal. A hardcoded core count or
    RAM figure records a lie the first time this fleet is timed on a second
    machine, and a timing ledger whose machine line is wrong is worse than no
    ledger at all. Best-effort: an unreadable field is recorded as null, not
    guessed."""
    ram_gb = None
    try:
        import ctypes

        class _MEMSTAT(ctypes.Structure):
            # All NINE MEMORYSTATUSEX fields: the call validates dwLength against
            # its own sizeof and returns ERROR_INVALID_PARAMETER for a struct that
            # is one field short, which silently records ram_gb=null instead of
            # the number -- exactly the kind of quiet nothing this instrument is
            # supposed to stop producing.
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = _MEMSTAT()
        ms.dwLength = ctypes.sizeof(_MEMSTAT)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            ram_gb = round(ms.ullTotalPhys / float(1 << 30), 1)
    except Exception:
        ram_gb = None
    return {"cpus": os.cpu_count(), "ram_gb": ram_gb, "platform": sys.platform}


class _TimingFrame:
    """One timed span. Entering pushes onto the ctx-shared stack; leaving pops it,
    subtracts its children to get self_s, adds its own wall to the parent's
    child_s, appends a sidecar row and -- outermost frame only -- updates the
    .rexauto_state roll-up. It never swallows the body's exception: a stage that
    raised is the most interesting duration on the board (a doomed 174s build is a
    real cost), so the frame records status and re-raises."""

    def __init__(self, ctx, stage, phase=None, module=None):
        self.ctx, self.stage, self.phase, self.module = ctx, stage, phase, module
        self.rec = None
        self.t0 = 0.0

    def __enter__(self):
        if not _timings_enabled():
            return self
        self.rec = {"stage": self.stage, "phase": self.phase, "module": self.module,
                    "depth": len(self.ctx._t_stack), "t_start_utc": _utcnow(),
                    "child_s": 0.0, "sub": {}, "cold": {}}
        self.t0 = time.perf_counter()
        self.ctx._t_stack.append(self.rec)
        return self

    def __exit__(self, et, ev, tb):
        if self.rec is None:
            return False
        try:
            wall = time.perf_counter() - self.t0
            st = self.ctx._t_stack
            # Pop by IDENTITY, and drop anything above us: if a nested frame ever
            # leaked, an instrument that corrupts its own stack must not go on to
            # corrupt the next stage's number too.
            for i in range(len(st) - 1, -1, -1):
                if st[i] is self.rec:
                    del st[i:]
                    break
            # Subtract the UNROUNDED child total, then round -- rounding first
            # made a frame whose children were essentially all of it report
            # self_s = -0.0. Frames here are strictly nested and single-threaded,
            # so children can never really exceed the parent; the clamp is
            # correcting float noise, not hiding a number.
            child_raw = self.rec["child_s"]
            self.rec["wall_s"] = round(wall, 3)
            self.rec["child_s"] = round(child_raw, 3)
            self.rec["self_s"] = round(max(0.0, wall - child_raw), 3)
            self.rec["status"] = ("ok" if et is None
                                  else ("exit" if et is SystemExit else "raised"))
            if st:
                st[-1]["child_s"] += wall
            self.ctx._timings_emit("frame", self.rec)
            if self.rec["depth"] == 0:
                self.ctx._t_run["done"] += 1
                self.ctx._timings_rollup(self.rec)
                self.ctx.log("timing %s: %.1fs wall (%.1fs self)"
                             % (self.stage, self.rec["wall_s"], self.rec["self_s"]))
        except Exception:
            pass
        return False


class _TimingOp:
    """A costly sub-operation inside a frame. Charges its seconds to the INNERMOST
    open frame at the moment it runs, which is what keeps a companion module's IDA
    invocation on the module's frame instead of on the build's."""

    def __init__(self, ctx, kind, bucket, fields):
        self.ctx, self.kind, self.bucket = ctx, kind, bucket
        self.fields = fields
        self.on = _timings_enabled()
        self.t0 = 0.0

    def set(self, **kw):
        self.fields.update(kw)
        return self

    def __enter__(self):
        if self.on:
            self.t0 = time.perf_counter()
        return self

    def __exit__(self, et, ev, tb):
        if not self.on:
            return False
        try:
            wall = time.perf_counter() - self.t0
            st = self.ctx._t_stack
            if st and self.bucket:
                sub = st[-1]["sub"]
                sub["%s_s" % self.bucket] = round(sub.get("%s_s" % self.bucket, 0.0) + wall, 3)
                sub["%s_n" % self.bucket] = sub.get("%s_n" % self.bucket, 0) + 1
            rec = dict(self.fields)
            rec.update({"op": self.kind, "bucket": self.bucket, "wall_s": round(wall, 3),
                        "depth": len(st), "stage": st[-1]["stage"] if st else None,
                        "module": st[-1]["module"] if st else None,
                        "status": "ok" if et is None else "raised"})
            self.ctx._timings_emit("op", rec)
        except Exception:
            pass
        return False


def run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def rexglue(ctx, *xargs, env=None, capture=False):
    verify_sdk_floor(ctx.env)  # hard minimum, deliberately not skippable
    verify_sdk_pin(ctx.env)  # gate SDK use (codegen/init); a pure game run never reaches this
    cmd = [ctx.env["rexglue"]] + list(xargs)
    e = dict(os.environ, **(env or {}))
    # Every rexglue invocation in the pipeline funnels through here -- including
    # every pure-add-gate probe pass, because deepextract's codegen_fn is a lambda
    # over this wrapper -- so one stamp here counts every codegen pass a run pays
    # for, with no second call site to keep in sync.
    with ctx.t_op("rexglue", "codegen", argv=[str(a) for a in xargs],
                  dump_image=bool(env and "REX_DUMP_IMAGE" in env), capture=bool(capture)) as op:
        r = (subprocess.run(cmd, env=e, cwd=ctx.port, capture_output=True, text=True) if capture
             else subprocess.run(cmd, env=e, cwd=ctx.port))
        op.set(rc=r.returncode)
    return r


def add_includes(ctx, names, manifest=None):
    man = manifest or ctx.manifest
    txt = open(man, encoding="utf-8", errors="ignore").read()
    m = re.search(r'includes\s*=\s*\[([^\]]*)\]', txt)
    cur = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    for n in names:
        if n not in cur:
            cur.append(n)
    newline = "includes = [%s]" % ", ".join('"%s"' % c for c in cur)
    if m:
        txt = txt[:m.start()] + newline + txt[m.end():]
    else:
        txt += "\n" + newline + "\n"
    open(man, "w", encoding="utf-8").write(txt)


# ------------------------------------------------------------------------ stages
def stage_extract(ctx):
    xex, game_dir = _extract.extract_container(ctx.args.container, ctx._game_out, log=ctx.log)
    ctx.game, ctx.xex = game_dir, xex
    info = {"xex": xex, "game_dir": game_dir}
    # Generic auto-title-update. detect_title_update stages a matching XEX delta
    # (default.xexp) beside the base default.xex in the game dir. rexglue's loader
    # auto-applies a co-located "<base>+p" delta IN MEMORY -- gated by cvar
    # xex_apply_patches (default on) -- at BOTH codegen (before the analysis
    # snapshot) and runtime, so we recompile AND run the exact patched version the
    # user has, with no separate patch step and no SDK change. ctx.xex stays the
    # base xex; gabarito_key folds the .xexp in so the TU build keeps its own cure
    # set. Strictly additive: no TU -> ctx.xex is the base xex and codegen input is
    # byte-identical to before (regression-gate proven).
    if not getattr(ctx.args, "no_title_update", False):
        tu_xexp = _extract.detect_title_update(game_dir, ctx.args.container, xex, log=ctx.log)
        if tu_xexp:
            ctx.tu_xexp = tu_xexp
            info["tu_xexp"] = tu_xexp
            ctx.log("title-update staged (%s) -- codegen + runtime auto-apply it in memory"
                    % os.path.basename(tu_xexp))
    ctx.mark("extract", info)


def stage_xctd(ctx):
    """Pre-decompress XCTD (XCompress LZXTDECODE 0F F5 12 ED) assets in place.
    On real hardware the KERNEL transparently decompresses these; our runtime's
    XctdCompressionInformation stub makes the game take its "not compressed"
    path, so serving plaintext is exactly what it expects. No-op (0 files) for
    every title that doesn't use it -- fleet regression-free by construction.
    Runs BEFORE init/codegen so the whole pipeline sees the final game dir.
    Proved on Captain America: Super Soldier (asset wall -> gameplay); same
    format ships in Alien: Isolation, Monkey Island 2 SE, XCOM."""
    import xctd as _xctd
    game = ctx.game or ctx._game_out
    if not os.path.isdir(game):
        raise SystemExit("[rexauto] xctd: no game dir at %s -- run extract first" % game)
    backup = os.path.join(ctx.work, "xctd_originals")
    n = _xctd.rip_inplace(game, backup, ctx.env, log=ctx.log)
    ctx.mark("xctd", {"files": n})


def stage_init(ctx):
    if os.path.exists(ctx.manifest):
        ctx.log("project already initialised")
    else:
        xex = ctx.xex or os.path.join(ctx.game, "default.xex")
        if not os.path.exists(xex):
            raise SystemExit("default.xex not found (%s) — run the extract stage first" % xex)
        r = run([ctx.env["rexglue"], "init", "--project-name", ctx.name,
                 "--xex-path", xex, "--game-root", ctx.game, "--project-root", ctx.port])
        if r.returncode != 0 or not os.path.exists(ctx.manifest):
            raise SystemExit("rexglue init failed (rc=%s, no manifest at %s)"
                             % (getattr(r, "returncode", "?"), ctx.manifest))
    if not os.path.exists(ctx.functions):
        _heal.write_overrides(ctx.functions, {})
    add_includes(ctx, ["%s_functions.toml" % ctx.name])
    ctx.mark("init")


def _tail_idalog(ctx, idalog, stop):
    """Stream the IDA pass's [xjt] progress lines to the UI while it runs."""
    seen = 0
    while not stop.is_set():
        try:
            if os.path.exists(idalog):
                lines = open(idalog, errors="ignore").read().splitlines()
                for l in lines[seen:]:
                    if "[xjt]" in l:
                        msg = l.split("[xjt]", 1)[1].strip()
                        if msg.startswith("progress "):
                            msg = msg[9:]
                        if any(k in msg for k in ("defining", "scanning", "analyzing",
                                                  "round", "functions=")):
                            ctx.log("@jump tables: " + msg)
                seen = len(lines)
        except OSError:
            pass
        time.sleep(0.4)


def stage_setjmp(ctx):
    """Detect the statically-linked CRT setjmp/longjmp routines and record their
    guest addresses in the manifest, so codegen emits ppc_setjmp/ppc_longjmp at
    those call sites.

    Xbox 360 C++ exception handling is linked straight into the title. longjmp is
    a *non-local* jump (mass-restore of GPR/FPR/VMX + the stack pointer from a
    jmp_buf, then blr). The recompiler turns blr into a plain C++ `return`, so
    without these addresses set, a guest longjmp returns to its immediate caller,
    the caller skips its epilogue, a non-volatile register is left corrupted and
    the title crashes at startup (a near-null write). Detecting and configuring
    them is what lets exception-using titles boot. Titles that don't use C++
    exceptions have no signature and are left untouched."""
    try:
        import detect_setjmp as _dj
    except Exception as ex:
        ctx.log("detect_setjmp unavailable -> skipping setjmp/longjmp detection (%s)" % ex)
        return ctx.mark("setjmp", {"skipped": "no-module"})
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    # Force a FRESH dump. The guard we must scan is the one codegen actually
    # recompiles, and when a title update is staged (ctx.tu_xexp) rexglue's loader
    # auto-applies the co-located default.xexp delta IN MEMORY at codegen time
    # (cvar xex_apply_patches; user_module.cpp ApplyPatch) -- so the image codegen
    # dumps here is the PATCHED (TU) image, whose setjmp/longjmp guard differs from
    # the base. A stale skate3_image.bin left over from an EARLIER run that predates
    # the .xexp staging would be the un-patched BASE image; scanning it writes the
    # retail guard address, which doesn't even exist in the TU generated set, so
    # ppc_setjmp lands at a no-op site and the title needs hand-fixing. Delete any
    # leftover first so a pre-TU image can never be reused, and so the no-dump guard
    # below can't silently pass on a stale file when codegen fails to re-dump.
    # NO-OP for non-TU titles: ctx.tu_xexp is None -> codegen loads only the base
    # xex -> the re-dumped image is byte-identical to before, and codegen OUTPUT is
    # untouched (this only deletes/rewrites a throwaway analysis dump).
    try:
        if os.path.exists(image):
            os.remove(image)
    except OSError as ex:
        ctx.log("could not remove stale image dump %s (%s) -- continuing; "
                "codegen truncates+overwrites it anyway" % (image, ex))
    tu = getattr(ctx, "tu_xexp", None)
    ctx.log("scanning %s image for setjmp/longjmp (C++ exception support)"
            % ("PATCHED (title-update) " if tu else ""))
    try:
        blob = do_codegen(ctx, env={"REX_DUMP_IMAGE": image}, level="trace")
    except SdkMismatch:
        raise  # the wrong SDK is not a failed dump: nothing downstream may run
    except SystemExit as ex:
        ctx.log("codegen for image dump failed -> skipping setjmp detection (%s)" % ex)
        return ctx.mark("setjmp", {"skipped": "codegen-fail"})
    if not os.path.exists(image):
        ctx.log("image dump produced nothing (rexglue lacks the dump-image patch) "
                "-> skipping setjmp detection")
        return ctx.mark("setjmp", {"skipped": "no-dump"})
    bm = re.search(r"base=0x([0-9A-Fa-f]+), size=0x([0-9A-Fa-f]+)", blob)
    base = int(bm.group(1), 16) if bm else 0x82000000
    image_end = base + (int(bm.group(2), 16) if bm else 0x900000)
    secs = re.findall(r"section '([^']+)' at 0x([0-9A-Fa-f]+) size 0x([0-9A-Fa-f]+) exec=(\w+)", blob)
    exec_secs = [(int(a, 16), int(a, 16) + int(sz, 16))
                 for _, a, sz, ex in secs if ex.lower() in ("true", "1")]
    if not exec_secs:
        ctx.log("could not parse exec sections -> skipping setjmp detection")
        return ctx.mark("setjmp", {"skipped": "no-sections"})
    # Hand the freshly-dumped image + parsed ranges to the jumptables stage, which
    # runs immediately after and would otherwise re-run an IDENTICAL image-dump
    # codegen (~46s on GTA-SA, ~4min on GTA V) purely to reproduce this same file.
    # The image dump is the raw decompressed sections (project_recompiler.cpp:251),
    # independent of setjmp/functions.toml, so it is byte-identical between the two
    # stages -- reuse is safe. Only set when running in-process this session; a
    # `--from jumptables` run has no stash and re-dumps as before.
    ctx._jt_image = {"image": image, "base": base, "image_end": image_end,
                     "exec_secs": exec_secs}
    try:
        res = _dj.detect(image, exec_secs, base)
    except Exception as ex:
        ctx.log("setjmp detection error -> skipping (%s)" % ex)
        return ctx.mark("setjmp", {"skipped": "detect-error"})
    lj, sj = res.get("longjmp_address"), res.get("setjmp_address")
    if lj is None:
        ctx.log("no setjmp/longjmp signature found (title likely uses no C++ exceptions) -> ok")
        return ctx.mark("setjmp", {"found": False})
    if sj is None:
        ctx.log("longjmp 0x%X found but setjmp ambiguous (%s) -> need both; skipping write" % (lj, res))
        return ctx.mark("setjmp", {"longjmp": "0x%X" % lj, "setjmp": "ambiguous"})
    _dj.write_addresses(ctx.manifest, longjmp=lj, setjmp=sj)
    ctx.log("setjmp/longjmp detected on %s image -> setjmp=0x%X longjmp=0x%X (written to manifest)"
            % ("PATCHED" if tu else "base", sj, lj))
    ctx.mark("setjmp", {"setjmp": "0x%X" % sj, "longjmp": "0x%X" % lj,
                        "image": "patched" if tu else "base"})


def stage_jumptables(ctx):
    if not ctx.env["idat"]:
        ctx.log("IDA not found -> skipping jump-table recovery (built-in switch handling "
                "still applies; boundary/runtime heal still run)")
        return ctx.mark("jumptables", {"skipped": "no-ida"})
    if not ctx.env["jt_repo"]:
        ctx.log("xenon-jumptables repo not found -> skipping jump-table recovery")
        return ctx.mark("jumptables", {"skipped": "no-repo"})
    if not ctx.env["python"]:
        ctx.log("no python interpreter for the jump-table scripts -> skipping (set PYTHON)")
        return ctx.mark("jumptables", {"skipped": "no-python"})
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    reuse = getattr(ctx, "_jt_image", None)
    if reuse and reuse.get("image") == image and os.path.exists(image):
        # The setjmp stage (which ran this same session) already produced this exact
        # image + ranges from an identical codegen. Skip the redundant re-dump.
        ctx.log("reusing image + section ranges from the setjmp stage "
                "(identical codegen; skipping the redundant image-dump pass)")
        ctx.t_note(image_dump="reused-from-setjmp")
        base, image_end, exec_secs = reuse["base"], reuse["image_end"], reuse["exec_secs"]
    else:
        ctx.t_note(image_dump="paid")
        ctx.log("dumping decompressed image + reading section ranges")
        try:
            blob = do_codegen(ctx, env={"REX_DUMP_IMAGE": image}, level="trace")
        except SdkMismatch:
            raise
        except SystemExit as ex:
            ctx.log("codegen (for image dump) failed -> skipping jump tables (%s)" % ex)
            return ctx.mark("jumptables", {"skipped": "codegen-fail"})
        if not os.path.exists(image):
            ctx.log("image dump produced nothing (rexglue likely lacks the dump-image patch) "
                    "-> skipping jump tables")
            return ctx.mark("jumptables", {"skipped": "no-dump"})
        bm = re.search(r"base=0x([0-9A-Fa-f]+), size=0x([0-9A-Fa-f]+)", blob)
        base = int(bm.group(1), 16) if bm else 0x82000000
        image_end = base + (int(bm.group(2), 16) if bm else 0x900000)
        secs = re.findall(r"section '([^']+)' at 0x([0-9A-Fa-f]+) size 0x([0-9A-Fa-f]+) exec=(\w+)", blob)
        exec_secs = [(int(a, 16), int(a, 16) + int(sz, 16))
                     for _, a, sz, ex in secs if ex.lower() in ("true", "1")]
        # With [[modules]] in the manifest the trace lists every module's
        # sections. The jump tables recovered here are the ENTRYPOINT's (the
        # companions get theirs through their own module view), so a section
        # outside its image is another module's, not a parse error. Skate 3:
        # the eawebkit sections at 0x88... pushed the range to 0x8849CAD0, the
        # sanity check refused it, and the title lost static bctr recovery.
        _foreign = [(a, e) for a, e in exec_secs if not (base <= a < e <= image_end)]
        if _foreign:
            exec_secs = [(a, e) for a, e in exec_secs if base <= a < e <= image_end]
            ctx.log("  %d exec section(s) outside the entrypoint image (0x%X..0x%X) belong "
                    "to companion module(s) -> not part of its range"
                    % (len(_foreign), base, image_end))
        if not exec_secs:
            ctx.log("WARNING: could not parse exec sections from the rexglue trace (log format may "
                    "have changed) -> skipping jump tables")
            return ctx.mark("jumptables", {"skipped": "no-sections"})
    text_start, text_end = min(s for s, _ in exec_secs), max(e for _, e in exec_secs)
    if not (base <= text_start < text_end <= image_end):
        ctx.log("WARNING: parsed section range looks wrong (0x%X..0x%X in 0x%X..0x%X) -> skipping"
                % (text_start, text_end, base, image_end))
        return ctx.mark("jumptables", {"skipped": "bad-range"})
    funcs = os.path.join(ctx.work, "%s_functions_list.txt" % ctx.name)
    rf = run([ctx.env["python"], os.path.join(ctx.env["jt_repo"], "src", "extract_funcs.py"),
              ctx.gen, "-o", funcs])
    if rf.returncode != 0 or not os.path.exists(funcs):
        # Say WHY. A bare "extract_funcs failed" is how a broken python
        # interpreter cost every title its static bctr recovery without anyone
        # being able to tell from the log what had gone wrong.
        why = ((rf.stderr or rf.stdout or "").strip().splitlines() or ["no output"])[-1]
        ctx.log("extract_funcs failed (rc=%s, python=%s): %s"
                % (rf.returncode, ctx.env["python"], why[:200]))
        ctx.log("  -> skipping jump tables (static bctr recovery is LOST for this title)")
        return ctx.mark("jumptables", {"skipped": "extract-funcs-fail"})
    cfg = os.path.join(ctx.work, "%s_jt.json" % ctx.name)
    out_json = os.path.join(ctx.work, "jumptables.json")
    json.dump({"image": image, "image_base": hex(base), "image_end": hex(image_end),
               "text_start": hex(text_start), "text_end": hex(text_end), "output": out_json,
               "functions": funcs, "format": "rexglue", "toml": ctx.switches}, open(cfg, "w"))
    # --- global IDA cache: identical image => identical analysis --------------
    # The IDA pass is the pipeline's one 100%-serial single-core sink (minutes on
    # a big title) and is fully determined by (image bytes, analysis code). A
    # re-port of the same game (budokai3's fresh regen re-paid a byte-identical
    # analysis) or a wiped work dir should never re-run it. Keyed by
    # sha256(image) + section ranges + the xenon-jumptables revision; the cached
    # artifacts are the switch_tables.toml AND the .i64 (which deepextract
    # reuses, so a hit accelerates that stage too). REXAUTO_NO_IDA_CACHE=1 opts out.
    ida_i64 = image + ".elf.i64"
    cache_hit = False
    cache_dir = None
    if not os.environ.get("REXAUTO_NO_IDA_CACHE"):
        # Key the cache on the CONTENT of the scripts that determine the
        # analysis, not the repo revision: a xenon-jumptables commit that
        # doesn't touch the analysis code (closure_cert, extract_funcs, docs,
        # lint) used to invalidate the whole fleet's cached .i64 analyses --
        # minutes of serial IDA per module re-paid for nothing (three tooling
        # commits on 10/jul forced fifadllzf 29MB + halo's 4 waves modules to
        # re-analyze). Falls back to the git rev if the files are unreadable.
        jt_rev = ""
        try:
            import hashlib as _hl
            h = _hl.sha256()
            for s in ("ida_jumptables.py", "deep_extract.py", "recover.py", "gen_toml.py"):
                p = os.path.join(ctx.env["jt_repo"], "src", s)
                if os.path.exists(p):
                    h.update(open(p, "rb").read())
            jt_rev = h.hexdigest()
        except Exception:
            try:
                r = run(["git", "-C", ctx.env["jt_repo"], "rev-parse", "HEAD"],
                        capture_output=True, text=True)
                jt_rev = (r.stdout or "").strip()
            except Exception:
                pass
        # The function list SEEDS the analysis (cfg "functions"), so it is an
        # analysis input too: today it went 0 -> 101426 entries for fifadllzf,
        # and a hit keyed without it would have replayed the 0-seed analysis.
        key = "%s-%x-%x-%s-%s" % (_sha256(image)[:20], text_start, text_end, jt_rev[:12],
                                  _sha256(funcs)[:12])
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "ida", key)
        c_toml, c_i64 = os.path.join(cache_dir, "switch_tables.toml"), os.path.join(cache_dir, "image.i64")
        if os.path.exists(c_toml) and os.path.exists(c_i64):
            shutil.copyfile(c_toml, ctx.switches)
            shutil.copyfile(c_i64, ida_i64)
            n = open(ctx.switches).read().count("[[switch_tables]]")
            add_includes(ctx, ["%s_switch_tables.toml" % ctx.name])
            ctx.log("jump tables from IDA cache: %d tables (identical image analyzed "
                    "before; delete rexauto/cache/ida to force re-analysis)" % n)
            ctx.t_note(ida_cache="hit")
            return ctx.mark("jumptables", {"tables": n, "cache": True})
    idalog = out_json + ".idalog.txt"
    try:
        if os.path.exists(idalog):
            os.remove(idalog)
    except OSError:
        pass
    ctx.log("recovering jump tables (IDA)")
    ctx.t_note(ida_cache=("disabled" if os.environ.get("REXAUTO_NO_IDA_CACHE") else "miss"))
    stop = threading.Event()
    threading.Thread(target=_tail_idalog, args=(ctx, idalog, stop), daemon=True).start()
    # recover.py wall, NOT idat wall: this one process also builds the ELF and
    # generates the toml. Naming it "ida_s" without saying so would attribute
    # ELF-construction seconds to auto-analysis and send the next iteration
    # optimizing the wrong thing. The finer split already exists inside the run --
    # ida_jumptables.py prints cumulative [xjt] laps into <out>.idalog.txt, which
    # _tail_idalog is already re-emitting here.
    with ctx.t_op("ida", "ida", tool="recover.py",
                  covers="idat + ELF build + toml gen", idalog=os.path.basename(idalog)) as op:
        rr = run([ctx.env["python"], os.path.join(ctx.env["jt_repo"], "src", "recover.py"),
                  cfg, "--ida", ctx.env["idat"]])
        op.set(rc=rr.returncode)
    stop.set()
    if rr.returncode != 0 or not os.path.exists(ctx.switches):
        ctx.log("jump-table recovery failed -> continuing without it")
        return ctx.mark("jumptables", {"skipped": "recover-fail"})
    n = open(ctx.switches).read().count("[[switch_tables]]")
    add_includes(ctx, ["%s_switch_tables.toml" % ctx.name])
    if cache_dir and os.path.exists(ida_i64):
        try:
            os.makedirs(cache_dir, exist_ok=True)
            shutil.copyfile(ctx.switches, os.path.join(cache_dir, "switch_tables.toml"))
            shutil.copyfile(ida_i64, os.path.join(cache_dir, "image.i64"))
        except OSError as ex:
            ctx.log("  (IDA cache write skipped: %s)" % ex)
    ctx.log("jump tables recovered: %d" % n)
    ctx.mark("jumptables", {"tables": n})


def write_build_bat(ctx, parallel=None):
    # A clang-OOM lesson (see the "LLVM ERROR: out of memory" handlers) is
    # PERSISTENT: once a port's giant TUs prove they can't take the default
    # 18 concurrent frontends, every later bat regeneration -- heal-loop,
    # re-runs, module builds sharing the work dir -- inherits the reduced -j
    # instead of re-discovering the crash. Explicit `parallel` still wins.
    if parallel is None:
        parallel = ctx.load_state().get("build_parallel")
    bat = os.path.join(ctx.work, "_build.bat")
    sdk = ctx.env["sdk"].replace("\\", "/")
    # RelWithDebInfo by default: same optimization as Release but with symbols +
    # line info, so a crash in the recompiled code points straight at the generated
    # sub_XXXX + line -- the heal/gate debug loop's biggest pain. Codegen is
    # unaffected (the build type never changes generated/), so it's zero-regression
    # for the codegen gate. Set REXAUTO_BUILD_TYPE=Release for a stripped, smaller
    # distribution build.
    build_type = os.environ.get("REXAUTO_BUILD_TYPE", "RelWithDebInfo")
    configure = ('cmake --preset win-amd64-release -DCMAKE_BUILD_TYPE=%s '
                 # map imported libs (spdlog/fmt) to their Release variant under
                 # RelWithDebInfo, else CMake links spdlogd.lib (_ITERATOR_DEBUG_LEVEL=2)
                 # against our IDL=0 objects -> lld-link /failifmismatch. Harmless for Release.
                 '-DCMAKE_MAP_IMPORTED_CONFIG_RELWITHDEBINFO=Release -DCMAKE_C_COMPILER="%s" '
                 '-DCMAKE_CXX_COMPILER="%s" -DCMAKE_PREFIX_PATH="%s" '
                 '-Drexglue_DIR="%s/lib/cmake/rexglue"'
                 % (build_type, ctx.env["clang"].replace("\\", "/"),
                    ctx.env["clangxx"].replace("\\", "/"), sdk, sdk))
    # Perf win #4 (strip per-round reconfigure): every heal round used to pay a
    # full `cmake --preset` (~5-15s) even though nothing about the configuration
    # changed. The bat now configures only when the build dir has no
    # CMakeCache.txt; a CHANGE in configure inputs (build type, compilers, SDK
    # path) is detected here python-side via a stamp file and forces a fresh
    # configure by deleting the cache. Output-neutral: the configure command is
    # byte-identical when it does run.
    bdir = os.path.join(ctx.port, "out", "build", "win-amd64-release")
    stamp = os.path.join(ctx.work, "_configure.stamp")
    old = open(stamp, encoding="utf-8").read() if os.path.exists(stamp) else None
    if old != configure:
        try:
            os.remove(os.path.join(bdir, "CMakeCache.txt"))
        except OSError:
            pass
        open(stamp, "w", encoding="utf-8").write(configure)
    lines = [
        "@echo off",
        'call "%s" >nul' % ctx.env["vcvars"],
        'cd /d "%s"' % ctx.port,
        'if not exist "out\\build\\win-amd64-release\\CMakeCache.txt" (',
        "  " + configure,
        ")",
        "cmake --build out/build/win-amd64-release --parallel%s -- -k 0" % (
            " %d" % parallel if parallel else ""),
        # capture the build's errorlevel BEFORE echo resets it -- `echo RC=...`
        # used to be the last command, so the bat's exit code was ALWAYS 0 and
        # a failed heal-round rebuild silently relaunched the stale exe (the
        # Gears of War 3 ghost-target loop).
        "set BUILDRC=%errorlevel%",
        "echo RC=%BUILDRC%",
        "exit /b %BUILDRC%",
    ]
    open(bat, "w").write("\r\n".join(lines) + "\r\n")
    return bat


def _gen_snapshot(ctx):
    """Per generated file: md5 + mtime, plus the line set of headers (every TU
    depends on the shared init header, so we need to reason about its diff)."""
    snap = {}
    for p in glob.glob(os.path.join(ctx.gen, "*.cpp")) + glob.glob(os.path.join(ctx.gen, "*.h")):
        try:
            data = open(p, "rb").read()
            lines = set(data.decode("utf-8", "ignore").splitlines()) if p.endswith(".h") else None
            snap[p] = (hashlib.md5(data).digest(), os.path.getmtime(p), lines)
        except OSError:
            pass
    return snap


def _gen_restore_unchanged(ctx, snap):
    """Restore the mtime of regenerated files that didn't really change, so ninja
    skips recompiling them. NOTE: an earlier version also kept the old timestamp
    on the shared init header when its only diff was added DECLARE_REX_FUNC lines
    ("a new extern can't change a compiled TU"). That is UNSOUND with the PCH:
    clang validates the precompiled header against the header's CONTENT/size, so
    a content-changed header with an old mtime leaves the PCH stale and every
    subsequent compile fails ("modified since the precompiled header was built")
    -- which, combined with the always-0 build-bat exit code, made heal rounds
    silently relaunch a stale exe (Gears of War 3 ghost-target loop). A changed
    header now always keeps its new mtime: the PCH and its TUs rebuild."""
    units = 0
    for p, (h, mt, _oldlines) in snap.items():
        try:
            if not os.path.exists(p):
                continue
            data = open(p, "rb").read()
            if hashlib.md5(data).digest() == h:
                os.utime(p, (mt, mt))
                units += 1
        except OSError:
            pass
    headers = 0
    if units or headers:
        ctx.log("  incremental rebuild: reused %d unit(s)%s"
                % (units, " + %d header(s)" % headers if headers else ""))
    # The warm-build signal was already computed here and only ever logged. Return
    # it so a build's seconds can be read next to "how many TUs ninja was allowed
    # to skip" -- without that pairing a warm build and a cold one are the same
    # number with two different meanings.
    return units


def _normalize_toml_newlines(ctx):
    """Repair doubled carriage returns (\\r\\r\\n) in the per-project tomls.
    A text-mode writer handed a string that already contained \\r\\n produces
    them (seen once in the wild: a frozen-exe jumptables run corrupted
    switch_tables.toml, and rexglue's toml parser hard-fails on \\r\\r).
    Byte-preserving for healthy files: only rewrites when \\r\\r is present."""
    for p in (ctx.functions, ctx.switches, ctx.forced, ctx.manifest):
        try:
            if p and os.path.exists(p):
                raw = open(p, "rb").read()
                if b"\r\r" in raw:
                    open(p, "wb").write(raw.replace(b"\r\r\n", b"\r\n").replace(b"\r\r", b"\r\n"))
                    ctx.log("repaired doubled line endings in %s" % os.path.basename(p))
        except OSError:
            pass


def _normalize_function_overrides(ctx):
    """Make functions.toml satisfy the invariants rexglue enforces, before it is
    ever handed over.

    rexglue refuses the WHOLE config on the first overlapping boundary and
    codegen aborts, so one bad `end` costs a title its entire cure set -- Forza
    Horizon lost a 549s build that way. The guard in heal.write_overrides_full
    only fires when a pass writes; a config that arrived overlapping (from an
    older rexauto, or a gabarito) reaches codegen untouched. Normalise here so
    the first invocation is safe too.
    """
    try:
        if not (ctx.functions and os.path.exists(ctx.functions)):
            return
        full = _heal.load_overrides_full(ctx.functions)
        # Nothing may sit on a save/restore helper table: the SDK lets a config
        # entry outrank its helper detection, and the price is every caller's
        # intrinsic (see _helper_tables). Drop such entries wherever they came
        # from -- gap fill, a gabarito, a hand edit -- before rexglue sees them.
        stray = sorted(a for a in full if _in_helper(ctx, a))
        for a in stray:
            del full[a]
        if stray:
            _heal.write_overrides_full(ctx.functions, full)
            ctx.log("  dropped %d function override(s) inside save/restore helper tables: %s"
                    % (len(stray), ", ".join("0x%08X" % a for a in stray)))
        # One call only: dict(full) is a SHALLOW copy, so probing with it would
        # mutate the very entries the real pass is about to count.
        n = _heal.clamp_overlapping_ends(full)
        if n:
            _heal.write_overrides_full(ctx.functions, full)
            ctx.log("  normalised %d overlapping/orphaned boundary entr(ies) in %s"
                    % (n, os.path.basename(ctx.functions)))
    except (OSError, ValueError) as e:
        ctx.log("  could not normalise %s (%s)" % (os.path.basename(ctx.functions), e))


# Windows raises a C++ exception out of `new` when the COMMIT limit is hit, and
# an uncaught std::bad_alloc leaves the process as exit 0xE06D7363 with nothing
# on stderr. Commit is not RAM: this machine has 31 GB of RAM and a 16 MB
# pagefile, so the limit is the RAM itself, and Hogwarts Legacy (7.8 GB) plus a
# browser was enough to push a 1.7 GB codegen over it -- Forza Horizon died four
# times in six minutes that way, at the exact seconds the system logged
# Resource-Exhaustion-Detector events, and passed every time afterwards.
CXX_EXCEPTION_RC = {0xE06D7363, 0xE06D7363 - (1 << 32)}


def _commit_state():
    """(limit_gb, free_gb, pagefile_mb, [(name, gb), ...]) or None.

    Best-effort via PowerShell/CIM, so it works in the frozen build too and never
    blocks a run: any failure returns None and the caller says nothing.
    """
    ps = (
        "$o=Get-CimInstance Win32_OperatingSystem;"
        "$pf=(Get-CimInstance Win32_PageFileUsage|Measure-Object AllocatedBaseSize -Sum).Sum;"
        "$top=Get-Process|Sort-Object PrivateMemorySize64 -Descending|Select-Object -First 3|"
        "ForEach-Object{$_.ProcessName+'='+[math]::Round($_.PrivateMemorySize64/1GB,1)};"
        "'{0}|{1}|{2}|{3}' -f ($o.TotalVirtualMemorySize/1MB),($o.FreeVirtualMemory/1MB),"
        "[int]($pf),($top -join ',')")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=20)
        lim, free, pf, top = r.stdout.strip().split("|", 3)
        procs = [(t.split("=")[0], float(t.split("=")[1])) for t in top.split(",") if "=" in t]
        return float(lim), float(free), int(float(pf or 0)), procs
    except Exception:
        return None


def _commit_warning(state, need_gb=6.0):
    """One sentence a user can act on, or None when memory is fine."""
    if not state:
        return None
    lim, free, pf, procs = state
    hogs = ", ".join("%s (%.1f GB)" % (n, g) for n, g in procs if g >= 1.0)
    if free < need_gb:
        return ("only %.1f GB of commit free (limit %.1f GB, pagefile %d MB)%s -- a codegen "
                "that runs out of memory dies as exit 0xE06D7363 with no message. Close the "
                "big ones or enlarge the pagefile."
                % (free, lim, pf, ("; biggest: " + hogs) if hogs else ""))
    if pf < 1024:
        return ("pagefile is %d MB, so the commit limit is the physical RAM (%.1f GB) -- a "
                "game or browser open beside the pipeline can starve codegen. %.1f GB free now."
                % (pf, lim, free))
    return None



def do_codegen(ctx, env=None, level="error"):
    """Run codegen, auto-registering unresolved tail-call targets (codegen's
    Validate phase reports them) until it passes. Returns the captured output
    (at trace level it carries the section ranges the jumptables stage needs)."""
    parent = getattr(ctx, "_native_parent", None)
    if parent is not None:
        if env and "REX_DUMP_IMAGE" in env:
            # An image dump wants THIS module's bytes, and a project codegen dumps
            # the entrypoint's: run the module's own manifest once and hand back
            # its output whatever the verdict -- the dump lands before analysis,
            # which is allowed to fail (its computed branches are what the dump
            # is for).
            r = rexglue(ctx, "--log-level", level, "codegen", "--ignore-stamp", ctx.manifest,
                        env=env, capture=True)
            return (r.stdout or "") + (r.stderr or "")
        # A companion is emitted by the PROJECT codegen, from the [[modules]]
        # entry _declare_native_modules mirrors out of its files: ReXGlue v0.10.0
        # has no symbol_prefix, so recompiling it alone and linking it into the
        # same exe -- the 0.8.2 way -- cannot even link (two PPCImageConfig).
        return do_codegen(parent, env=env, level=level)
    _normalize_toml_newlines(ctx)
    _normalize_function_overrides(ctx)
    mods = extra_modules(ctx)
    if mods:
        _declare_native_modules(ctx, mods)
    snap = _gen_snapshot(ctx)
    for _ in range(10):
        r = rexglue(ctx, "--log-level", level, "codegen", ctx.manifest, env=env, capture=True)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            # heal unregistered bctr switch-on-ctr landings the SDK left as an
            # indirect dispatch (would FATAL at runtime); a re-codegen converges
            # them. No-op (returns 0) for a title whose switches all resolve.
            if _jt.heal(ctx, log=ctx.log):
                continue
            # splice any declarative post-codegen source patches (e.g. the skate3
            # FOV / ultrawide-frustum render hooks) once codegen has converged and
            # before compile. No <name>_codegen_patches.toml -> no-op (byte-identical).
            _cgp.apply(ctx, log=ctx.log)
            # Surface what the image patches did. rexglue reports them on its own
            # output, which is captured and normally dropped on success -- so a
            # patch REFUSED because the game dump is a different build than the
            # one the patch was written against would vanish silently and the run
            # would look like a clean patched build. It is not: the port comes out
            # unpatched. Deduped because codegen runs several times per build.
            for _l in out.splitlines():
                _i = _l.find("[[image_patch]]")
                if _i < 0:
                    continue
                _msg = _l[_i + len("[[image_patch]]"):].strip()
                if _msg in getattr(ctx, "_patch_warned", ()):
                    continue
                ctx._patch_warned = set(getattr(ctx, "_patch_warned", set())) | {_msg}
                ctx.log("  image patch REFUSED: %s" % _msg)
                ctx.log("  -> it is NOT in this build; it was written against a "
                        "different dump of the game.")
            # Built-in correctness repair, not a per-title patch: SDK 0.8.2 lowers
            # an in-place vpkuwus/vpkuhus so its narrow writes clobber the wide
            # reads of the same register. Fixed upstream in 0.10.0; we ship a
            # prebuilt 0.8.2, so it is repaired in the emitted C++. Byte-identical
            # for any title that never packs in place.
            if not os.environ.get("REXAUTO_NO_VPACK_FIX"):
                _cgp.fix_vector_pack_inplace(ctx.gen, log=ctx.log)
                # NOTE: do NOT "fix" lvebx/lvehx/lvewx here. They look like a bug
                # -- the SDK lowers all three as a full 16-byte lvx instead of the
                # AltiVec single-element load -- but that is correct for this CPU.
                # ReXGlue v0.10.0 ships element-load builders and deliberately
                # leaves them UNDISPATCHED, with the reason in
                # instruction_dispatch.cpp: "Xenia treats lvebx/lvehx/lvewx as
                # 'Same as lvx' ... The Xbox 360 Xenon CPU does not implement the
                # standard AltiVec element-load semantics for these instructions."
                # Verified by running a patched v0.10.0 codegen over this same
                # title: its lvewx output is byte-identical to 0.8.2's.
            ctx.t_note(gen_units_reused=_gen_restore_unchanged(ctx, snap))
            # PCH wiring must run AFTER codegen: the only earlier call site
            # (setup_extra_modules) fires before <name>_init.h exists, so its
            # exists() guard silently skipped the injection and the v2.4.0
            # ~21%/TU win quietly vanished for every fresh port (fleet audit:
            # 1/18 ports had the PCH block). Idempotent, so calling per-codegen
            # is free once injected.
            _inject_pch_into_cmake(ctx)
            _inject_debug_diet_into_cmake(ctx)
            return out
        targets = _heal.unresolved_calls_from_text(out)
        if not targets and (r.returncode in CXX_EXCEPTION_RC or "bad allocation" in out):
            # Not a codegen error: the process ran out of commit and threw. The
            # inputs are fine, so wait for memory and try again before giving up.
            _oom = getattr(ctx, "_codegen_oom_retries", 0)
            state = _commit_state()
            ctx.log("  codegen died with a C++ exception (rc=0x%08X) -- almost always "
                    "std::bad_alloc: %s" % (r.returncode & 0xFFFFFFFF,
                                            _commit_warning(state, need_gb=1e9) or "commit unknown"))
            if _oom < 2:
                ctx._codegen_oom_retries = _oom + 1
                ctx.log("  retrying codegen in 20s (%d/2)" % (_oom + 1))
                time.sleep(20)
                continue
        if not targets:
            tail = "\n".join(out.splitlines()[-15:])
            raise SystemExit("[rexauto] codegen FAILED (rc=%d) — aborting\n%s" % (r.returncode, tail))
        # Route each target to the module that owns it: with [[modules]] in the
        # manifest one codegen validates every module, and a companion's target
        # registered in the entrypoint's functions.toml is "not in any code
        # region" there while staying unresolved in the companion forever.
        n = 0
        for owner, mine in _split_by_owner(ctx, targets):
            n += _heal.register_functions(mine, owner.functions)
        ctx.log("  codegen: %d unresolved tail-call target(s) -> registered %d; retrying"
                % (len(targets), n))
        if n == 0:
            raise SystemExit("[rexauto] codegen stuck on unresolved calls: %s"
                             % ", ".join("0x%X" % t for t in targets))
    raise SystemExit("[rexauto] codegen unresolved-call heal did not converge")


def do_build(ctx, bat, attempt=None):
    """Stream the build so ninja's [N/M] progress reaches the UI live."""
    logp = os.path.join(ctx.work, "_build.log")
    # Two facts decide whether this build's seconds are comparable to another
    # build's, and neither survives into .ninja_log: whether the build dir still
    # had a CMakeCache (so the bat skipped the whole configure) and what -j the
    # OOM handler had already knocked this port down to. Both are knowable here
    # and nowhere afterwards, so they are captured before the clock starts.
    op = ctx.t_op("build", "build", attempt=attempt,
                  cmakecache=os.path.exists(os.path.join(ctx.builddir, "CMakeCache.txt")),
                  parallel=ctx.load_state().get("build_parallel"))
    with op:
        p = subprocess.Popen(["cmd", "/c", bat], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        last = 0.0
        edges = None  # ninja's [N/M] total, if it printed one (a no-work build prints none)
        with open(logp, "w") as lf:
            for line in p.stdout:
                lf.write(line)
                m = re.search(r"\[(\d+)/(\d+)\]", line)
                if m:
                    n, tot = int(m.group(1)), int(m.group(2))
                    edges = tot
                    now = time.time()
                    if now - last > 0.3 or n == tot:
                        last = now
                        name = line.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:42]
                        ctx.log("@build %d/%d %s" % (n, tot, name))
        rc = p.wait()
        op.set(rc=rc, edges=edges)
    if rc == 0:
        apply_game_icon(ctx)  # every relink rewrites the exe -> re-brand it
    return logp, rc


def apply_game_icon(ctx):
    """Brand ctx.exe with the game's marketplace tile as its Windows icon.
    Best-effort: offline/no-tile/locked-exe just skips (never fails a build)."""
    try:
        if not os.path.exists(ctx.exe):
            return
        xex = ctx.xex or os.path.join(ctx.game or "", "default.xex")
        if not os.path.exists(xex):
            return
        with open(xex, "rb") as f:
            tid = _extract._xex_title_id(f.read(0x10000))
        png = _extract.fetch_title_icon(tid) if tid else None
        if not png:
            return
        import exeicon
        if exeicon.set_exe_icon(ctx.exe, png):
            if not getattr(ctx, "_icon_logged", False):
                ctx._icon_logged = True
                ctx.log("exe branded with the game's tile icon (title_id %s)" % tid)
    except Exception as ex:
        ctx.log("icon branding skipped (%s)" % ex)


def write_game_root(ctx):
    """Drop a 'game_root.txt' sidecar next to the exe naming the game data dir, so
    double-clicking the exe (no --game_data_root) still launches the title -- the
    runtime reads this when the flag is absent."""
    try:
        if ctx.game and os.path.isdir(ctx.game):
            with open(os.path.join(ctx.builddir, "game_root.txt"), "w", encoding="utf-8") as f:
                f.write(os.path.abspath(ctx.game) + "\n")
    except OSError as ex:
        ctx.log("could not write game_root.txt sidecar (%s)" % ex)
    copy_gpu_plugins(ctx)
    write_play_launcher(ctx)
    # Graphical launcher: the exe has no options screen, and resolution / monitor
    # / frame cap are runtime cvars that can only be chosen through REX_* env vars
    # BEFORE the process starts. Without this the only way to pick them is editing
    # a .cmd by hand.
    _gpu = gpu_plugins(ctx)
    _launcher.write(ctx, _gpu[0] if _gpu else None, log=ctx.log)


def gpu_plugins(ctx):
    """GPU plugin names shipped by the SDK in use, e.g. ["xenos"].

    ReXGlue 0.8.2 links the GPU into the runtime and has none. v0.10.0 moved it
    out to rexgpu-<name>.dll, loaded only when the `gpu_plugin` cvar names one --
    without it the runtime logs "no GPU emulation loaded (gpu_plugin not set);
    call ignored" for every Vd* call and the port renders nothing at all, with no
    error. Empty list on an SDK that has no plugins, so 0.8.2 is untouched."""
    sdk = ctx.env.get("sdk") or ""
    out = []
    for p in glob.glob(os.path.join(sdk, "bin", "rexgpu-*.dll")):
        name = os.path.basename(p)[len("rexgpu-"):-len(".dll")]
        if name:
            out.append(name)
    return sorted(out)


def copy_gpu_plugins(ctx):
    """Put the SDK's GPU plugin DLLs beside the exe. The SDK's own CMake copies
    rexruntime.dll but not these, so a v0.10.0 port could not find its GPU even
    once the cvar was set."""
    names = gpu_plugins(ctx)
    if not names:
        return
    sdk = ctx.env.get("sdk") or ""
    for n in names:
        src = os.path.join(sdk, "bin", "rexgpu-%s.dll" % n)
        dst = os.path.join(ctx.builddir, "rexgpu-%s.dll" % n)
        try:
            if os.path.exists(src) and (not os.path.exists(dst)
                                        or os.path.getmtime(src) > os.path.getmtime(dst)):
                shutil.copy2(src, dst)
                ctx.log("  GPU plugin: rexgpu-%s.dll copied beside the exe" % n)
        except OSError as ex:
            ctx.log("  could not copy rexgpu-%s.dll (%s)" % (n, ex))


def write_play_launcher(ctx):
    """Write `play <name>.cmd` next to the exe: the same launch, with the
    dispatcher in TOLERANT mode.

    By default an indirect call to a function the static scan never discovered is
    REX_FATAL -- the process dies, and every such address has to be found by the
    run-heal one launch-and-crash at a time. Many of those targets are
    runtime-computed vtable entries that no static analysis can reach, so dying
    on them buys nothing: hells-gate-recomp's ReXGlue patch replaces that
    REX_FATAL with a log-and-return in three lines, and it is why their Dante's
    Inferno reaches gameplay.

    Our own runtime already has the behaviour behind REX_HEAL_DISCOVER, used only
    while healing. Measured on Dante's Inferno, same exe, same build: strict dies
    at 20 s on 0x82908134; tolerant runs past 120 s with zero fatals.

    The exe keeps its strict default, because the heal needs a launch that stops
    at the first missing function to find it. This launcher is the one to hand a
    player. REXAUTO_NO_PLAY_LAUNCHER=1 skips writing it."""
    if os.environ.get("REXAUTO_NO_PLAY_LAUNCHER"):
        return
    try:
        gpu = gpu_plugins(ctx)
        path = os.path.join(ctx.builddir, "play %s.cmd" % ctx.name)
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(
                "@echo off\r\n"
                "rem Written by rexauto. Runs %s with the dispatcher tolerant of\r\n"
                "rem functions the static scan never discovered: an unresolved indirect\r\n"
                "rem call is logged and returns instead of killing the process.\r\n"
                "rem Run the .exe directly for the strict behaviour the heal needs.\r\n"
                "set REX_HEAL_DISCOVER=1\r\n" % ctx.name)
            if gpu:
                f.write(
                    "rem This SDK ships the GPU as a plugin. Without naming one the\r\n"
                    "rem runtime loads no GPU at all and the port renders nothing,\r\n"
                    "rem reporting only \"gpu_plugin not set; call ignored\".\r\n"
                    "set REX_GPU_PLUGIN=%s\r\n" % gpu[0])
            # Pass the game root explicitly. The game_root.txt sidecar is read
            # only by our fork's patch; stock ReXGlue v0.10.0 answers
            # "--game_data_root was not provided" and exits. Naming it here works
            # on both and costs nothing.
            root = os.path.abspath(ctx.game) if ctx.game else ""
            if root:
                f.write('start "" "%%~dp0%s.exe" --game_data_root="%s" %%*\r\n'
                        % (ctx.name, root))
            else:
                f.write('start "" "%%~dp0%s.exe" %%*\r\n' % ctx.name)
    except OSError as ex:
        ctx.log("could not write the play launcher (%s)" % ex)


def _game_icon_png(ctx):
    """Best-effort PNG bytes to use as the exe icon: the package cover (STFS),
    else the title Thumbnail.png from the extracted game. None if neither."""
    try:
        meta = _extract.read_package_meta(getattr(ctx.args, "container", "") or "")
        if meta.get("cover"):
            return meta["cover"]
    except Exception:
        pass
    if ctx.game:
        thumb = os.path.join(ctx.game, "Thumbnail.png")
        if os.path.exists(thumb):
            try:
                return open(thumb, "rb").read()
            except OSError:
                pass
    return None


def _inject_icon_into_cmake(ctx):
    """Wire src/<name>.rc into the port build (before add_executable), idempotently."""
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    if not os.path.exists(cml):
        return
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    if "rexauto-game-icon" in txt:
        return
    m = re.search(r"add_executable\(\s*%s\s+WIN32\s+\$\{(\w+)\}\s*\)" % re.escape(ctx.name), txt)
    if not m:
        return
    srcvar = m.group(1)
    block = ('    # rexauto-game-icon: use the game icon for the exe if present\n'
             '    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/%s.rc")\n'
             '        enable_language(RC)\n'
             '        list(APPEND %s src/%s.rc)\n'
             '    endif()\n' % (ctx.name, srcvar, ctx.name))
    open(cml, "w", encoding="utf-8").write(txt[:m.start()] + block + txt[m.start():])


def write_game_icon(ctx):
    """Give the recompiled exe the game's icon: build src/<name>.ico from the
    package cover or the title Thumbnail.png, emit a .rc, and wire it into the
    port build. No-op (keeps the default icon) when no game image is available."""
    png = _game_icon_png(ctx)
    if not png:
        return
    try:
        import io
        from PIL import Image
    except Exception:
        return
    try:
        im = Image.open(io.BytesIO(png)).convert("RGBA")
        w, h = im.size
        s = max(w, h, 16)
        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        canvas.paste(im, ((s - w) // 2, (s - h) // 2))
        srcdir = os.path.join(ctx.port, "src")
        os.makedirs(srcdir, exist_ok=True)
        sizes = [(n, n) for n in (16, 32, 48, 64, 128, 256) if n <= s] or [(s, s)]
        canvas.save(os.path.join(srcdir, ctx.name + ".ico"), sizes=sizes)
        with open(os.path.join(srcdir, ctx.name + ".rc"), "w", encoding="utf-8") as f:
            f.write('1 ICON "%s.ico"\n' % ctx.name)
        _inject_icon_into_cmake(ctx)
        ctx.log("game icon embedded in the exe")
    except Exception as ex:
        ctx.log("could not generate game icon (%s)" % ex)


# ----------------------------------------------------- extra (multi-binary) modules
# Some titles ship a 2nd recompilable guest module (e.g. Skate 3's EAWebkit.xex at
# guest 0x88xxxxxx) that the entrypoint calls into. The fork SDK already supports it
# (per-manifest out_directory_path + symbol_prefix, one dispatcher spanning both
# ranges); rexauto just orchestrates a 2nd codegen and wires its sources + host
# registration in. Everything below is a NO-OP for single-module titles
# (extra_modules() is empty), so those builds are byte-identical.
def extra_modules(ctx):
    """[{key, name, xex, symbol_prefix}] of recompilable modules beyond the entrypoint.
    Opt-in by data on disk: an optional port/<name>_modules.toml, else a narrow built-in
    for the known skate3/EAWebkit case (only fires when that exact 2nd module is present)."""
    cfg = os.path.join(ctx.port, "%s_modules.toml" % ctx.name)
    if os.path.exists(cfg):
        mods, txt = [], open(cfg, encoding="utf-8", errors="ignore").read()
        for blk in re.split(r'\[\[\s*modules\s*\]\]', txt)[1:]:
            g = lambda k: (re.search(k + r'\s*=\s*"([^"]*)"', blk) or [None, None])[1]
            key, xex = g("key"), g("xex")
            if key and xex:
                mods.append({"key": key, "name": g("name") or key,
                             "xex": os.path.join(ctx.game, xex.replace("/", os.sep)),
                             "symbol_prefix": g("symbol_prefix") or (key + "_")})
        return mods
    ewk = os.path.join(ctx.game, "data", "webkit", "EAWebkit.xex")
    if os.path.exists(ewk):
        return [{"key": "eawebkit", "name": "eawebkit", "xex": ewk, "symbol_prefix": "eawebkit_"}]
    return []


# ----------------------------------------------------- per-title app-glue (factory)
# Beyond the 2nd-module dispatch above, some titles need a little host glue wired
# into the generated app's OnPostSetup(): a signed-in user identity, content-scheme
# symbolic links (e.g. big:/dlcbig:), and a BIG-directory probe overlay. This is the
# mechanical, per-title-but-data-driven layer: the MECHANISM is generic (SDK
# RegisterSymbolicLink / HostPathDevice / SetIdentity) but the values are per-title,
# so they live in an opt-in port/<name>_appglue.toml consumed here. Everything below
# is a strict NO-OP when that file is absent: glue_records() returns {} and the
# injector is never called, so app.h stays byte-identical for every existing title.
def glue_records(ctx):
    """Parse the optional port/<name>_appglue.toml into a dict, or {} if absent.

    Mirrors extra_modules()' lightweight regex/toml parsing (same style as the
    <name>_modules.toml reader). Recognized sections:
      [identity]            xuid, name
      [[alias]]             scheme, target           (one per entry)
      [overlay]             enabled, scan_root, scan_prefix, device_scheme,
                            overlay_subdir, fixed_dirs[], [[overlay.link]] guest/target
      [dlc]                 auto_install, root
      [title_update]        container, url, [[title_update.payload]] src/dest/sha256/size
    Returns {} when the file does not exist."""
    cfg = os.path.join(ctx.port, "%s_appglue.toml" % ctx.name)
    if not os.path.exists(cfg):
        return {}
    txt = open(cfg, encoding="utf-8", errors="ignore").read()

    def _strip_comments(s):
        # drop full-line and trailing '#' comments (none of our values contain '#')
        out = []
        for ln in s.splitlines():
            h = ln.find("#")
            out.append(ln if h < 0 else ln[:h])
        return "\n".join(out)

    txt = _strip_comments(txt)

    def _sect(name):
        # body of a single [name] table up to the next top-level/array header
        m = re.search(r'(?m)^\s*\[%s\]\s*$' % re.escape(name), txt)
        if not m:
            return None
        rest = txt[m.end():]
        nxt = re.search(r'(?m)^\s*\[', rest)
        return rest[:nxt.start()] if nxt else rest

    def _arrays(name):
        # bodies of every [[name]] array-of-tables entry
        out = []
        for m in re.finditer(r'(?m)^\s*\[\[\s*%s\s*\]\]\s*$' % re.escape(name), txt):
            rest = txt[m.end():]
            nxt = re.search(r'(?m)^\s*\[', rest)
            out.append(rest[:nxt.start()] if nxt else rest)
        return out

    def _unescape(raw):
        # decode the TOML basic-string escapes we care about so the dict holds the
        # true string (e.g. "\\Device" -> "\Device"); _cpp_str re-escapes for C++.
        return re.sub(r'\\(.)', lambda mo: {
            "n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(mo.group(1),
            mo.group(1)), raw)

    def _s(blk, k):
        m = re.search(k + r'\s*=\s*"((?:[^"\\]|\\.)*)"', blk)
        return _unescape(m.group(1)) if m else None

    def _b(blk, k):
        m = re.search(k + r'\s*=\s*(true|false)', blk)
        return (m.group(1) == "true") if m else None

    def _i(blk, k):
        m = re.search(k + r'\s*=\s*(-?\d+)', blk)
        return int(m.group(1)) if m else None

    def _list(blk, k):
        m = re.search(k + r'\s*=\s*\[(.*?)\]', blk, re.S)
        if not m:
            return []
        return [_unescape(v.group(1)) for v in re.finditer(r'"((?:[^"\\]|\\.)*)"', m.group(1))]

    glue = {}

    ident = _sect("identity")
    if ident is not None:
        xuid = _s(ident, "xuid")
        if xuid:
            glue["identity"] = {"xuid": xuid, "name": _s(ident, "name") or "Player"}

    aliases = []
    for blk in _arrays("alias"):
        scheme, target = _s(blk, "scheme"), _s(blk, "target")
        if scheme and target:
            aliases.append({"scheme": scheme, "target": target})
    if aliases:
        glue["aliases"] = aliases

    ov = _sect("overlay")
    if ov is not None and _b(ov, "enabled"):
        links = []
        for blk in _arrays("overlay.link"):
            guest, target = _s(blk, "guest"), _s(blk, "target")
            if guest and target:
                links.append({"guest": guest, "target": target})
        glue["overlay"] = {
            "scan_root": _s(ov, "scan_root"),
            "scan_prefix": _s(ov, "scan_prefix"),
            "device_scheme": _s(ov, "device_scheme"),
            "overlay_subdir": _s(ov, "overlay_subdir"),
            "fixed_dirs": _list(ov, "fixed_dirs"),
            "links": links,
        }

    dlc = _sect("dlc")
    if dlc is not None and _b(dlc, "auto_install"):
        glue["dlc"] = {"auto_install": True, "root": _s(dlc, "root") or "dlc"}

    tu = _sect("title_update")
    if tu is not None:
        payloads = []
        for blk in _arrays("title_update.payload"):
            src, dest = _s(blk, "src"), _s(blk, "dest")
            if src and dest:
                payloads.append({"src": src, "dest": dest,
                                 "sha256": _s(blk, "sha256") or "",
                                 "size": _i(blk, "size") or 0})
        container, url = _s(tu, "container"), _s(tu, "url")
        if payloads or container or url:
            glue["title_update"] = {"container": container or "", "url": url or "",
                                    "payloads": payloads}

    return glue


def _author_module_manifest(ctx, m):
    sdkv = "0.8.0.0"
    try:
        mm = re.search(r'sdk_version\s*=\s*"([^"]*)"', open(ctx.manifest).read())
        sdkv = mm.group(1) if mm else sdkv
    except OSError:
        pass
    rel = os.path.relpath(m["xex"], ctx.port).replace("\\", "/")
    # This manifest exists to dump the module's image for IDA (a project codegen
    # dumps the entrypoint's), so it emits into a scratch directory: the C++ the
    # build compiles comes from the [[modules]] entry in the entrypoint manifest.
    open(os.path.join(ctx.port, "%s_manifest.toml" % m["key"]), "w", encoding="utf-8").write(
        '# %s -- companion module of %s: image-dump / jump-table manifest, authored by rexauto.\n'
        '# The build reads the [[modules]] entry in the entrypoint manifest, not this file.\n'
        '[project]\nname = "%s"\nsdk_version = "%s"\ngame_root = "../game"\n\n'
        '[entrypoint]\nfile_path = "%s"\nout_directory_path = "generated/%s_scan"\n'
        'includes = ["%s_functions.toml"]\n'
        % (m["key"], ctx.name, m["name"], sdkv, rel, m["key"], m["key"]))


def _declare_native_modules(ctx, mods):
    """Mirror every companion into the entrypoint manifest as a [[modules]] entry.

    This is the v0.10.0 contract: one manifest, one codegen, and the companion
    comes out as its own shared library that the runtime binds by guest_path at
    the guest's XexLoadImage. The module's cures stay in its own <key>_*.toml
    files (that is where the heal writes them); this only points the entry at
    whichever of them exist, plus the setjmp pair its scan manifest may carry.
    A block rexauto wrote earlier -- or a hand-written one for the same file --
    is replaced; a block for some other file is left alone. The file is rewritten
    only when the text changes, so an unchanged manifest keeps its mtime and the
    codegen stamp stays valid."""
    txt = _heal._read_text(ctx.manifest).replace("\r\n", "\n")
    ours, entries = set(), []
    for m in mods:
        rel = os.path.relpath(m["xex"], ctx.port).replace(os.sep, "/")
        guest = os.path.relpath(m["xex"], ctx.game).replace(os.sep, "/")
        ours.add(guest.lower())  # the runtime's own identity for a module
        inc = [n % m["key"] for n in ("%s_functions.toml", "%s_switch_tables.toml",
                                     "%s_forced_landings.toml")
               if os.path.exists(os.path.join(ctx.port, n % m["key"]))]
        block = ["# rexauto-module: %s" % m["key"], "[[modules]]",
                 'guest_path = "%s"' % guest, 'file_path = "%s"' % rel,
                 'out_directory_path = "generated/%s"' % m["key"],
                 "includes = [%s]" % ", ".join('"%s"' % n for n in inc)]
        scan = os.path.join(ctx.port, "%s_manifest.toml" % m["key"])
        if os.path.exists(scan):
            stxt = _heal._read_text(scan)
            for key in ("setjmp_address", "longjmp_address"):
                mm = re.search(r"^[ \t]*%s[ \t]*=[ \t]*(0x[0-9A-Fa-f]+)" % key, stxt, re.M)
                if mm:
                    block.append("%s = %s" % (key, mm.group(1)))
        entries.append("\n".join(block))
    # Every existing [[modules]] block, with its marker line when rexauto wrote
    # it. A block's body ends at the next section header OR the next marker:
    # letting the body run on to the next "[" swallowed the marker of the block
    # that followed, so every pass left one more orphaned marker behind and the
    # file was never stable. Stray markers from that era are collapsed first.
    txt = re.sub(r"(?:^# rexauto-module:[^\n]*\n)+(?=^# rexauto-module:)", "", txt, flags=re.M)
    pat = re.compile(r"(?:^# rexauto-module:[^\n]*\n)?^\[\[modules\]\][ \t]*\n"
                     r"(?:(?!\[|# rexauto-module:)[^\n]*\n?)*", re.M)

    def _drop_ours(mm):
        b = mm.group(0)
        fm = re.search(r'^[ \t]*guest_path[ \t]*=[ \t]*"([^"]*)"', b, re.M)
        return "" if fm and fm.group(1).replace("\\", "/").lower() in ours else b

    body = pat.sub(_drop_ours, txt)
    new = body.rstrip("\n") + "\n" + "".join("\n" + e + "\n" for e in entries)
    if new != txt:
        open(ctx.manifest, "w", encoding="utf-8").write(new)
        ctx.log("  %d companion(s) declared as [[modules]] in %s"
                % (len(mods), os.path.basename(ctx.manifest)))


def _migrate_legacy_app_header(ctx):
    """Rename the entrypoint's image-config symbol in a 0.8.2-era src/<name>_app.h.

    ReXGlue 0.8.2 took `symbol_prefix` on the entrypoint and emitted
    `<name>_PPCImageConfig`; v0.10.0 dropped the knob and emits a plain
    `PPCImageConfig`, so every port whose src/ was written by the old `init`
    stopped compiling the moment its generated tree was rebuilt -- one error,
    `use of undeclared identifier 'gears_of_war_judgment_PPCImageConfig'`, in
    the one hand-editable file the pipeline does not regenerate. Every port in
    the fleet except a freshly-initialised one was in that state, which is why
    none of them could be rebuilt against the pinned SDK.

    Renames only that symbol, only when the generated tree does not declare the
    prefixed name, so a tree that really is prefixed is left alone. Any user
    edits elsewhere in the file survive."""
    app = os.path.join(ctx.port, "src", "%s_app.h" % ctx.name)
    old_sym = "%s_PPCImageConfig" % ctx.name
    if not os.path.exists(app):
        return
    txt = _heal._read_text(app)
    if old_sym not in txt:
        return
    import glob as _glob
    for h in _glob.glob(os.path.join(ctx.gen, "*.h")):
        if old_sym in _heal._read_text(h):
            return  # this tree really does emit the prefixed symbol
    open(app, "w", encoding="utf-8").write(txt.replace(old_sym, "PPCImageConfig"))
    ctx.log("  migrated %s_app.h: %s -> PPCImageConfig (this SDK emits no symbol prefix)"
            % (ctx.name, old_sym))


def _strip_legacy_module_glue(ctx):
    """Remove the 0.8.2-era companion glue from a port that still carries it.

    The CMake foreach that linked generated/<mod>/sources.cmake into the exe would
    now duplicate every symbol the module's own shared library defines, and the
    OnPostSetup block calls a 6-argument InitializeFunctionTable that v0.10.0 does
    not have -- a port with either cannot build at all (fifa_street,
    spider_man_dimensions). Both are anchored on rexauto's own marker comments.
    An app.h whose hook also carries appglue is left alone with a warning:
    cutting the module half out of a shared method by regex is not something to
    do blind."""
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    if os.path.exists(cml):
        txt = _heal._read_text(cml).replace("\r\n", "\n")
        new = re.sub(r"\n# rexauto-extra-modules:[^\n]*\nforeach\(_rexauto_mod [^)]*\)\n.*?"
                     r"\nendforeach\(\)\n", "\n", txt, count=1, flags=re.S)
        if new != txt:
            open(cml, "w", encoding="utf-8").write(new)
            ctx.log("  legacy extra-module CMake block removed (this SDK builds companions as "
                    "shared libraries)")
    app = os.path.join(ctx.port, "src", "%s_app.h" % ctx.name)
    if not os.path.exists(app):
        return
    txt = _heal._read_text(app).replace("\r\n", "\n")
    if "rexauto: 2nd-module" not in txt:
        return
    if "rexauto: appglue" in txt:
        ctx.log("  WARNING: %s_app.h carries the 0.8.2 module dispatcher AND appglue in one "
                "OnPostSetup; remove the InitializeFunctionTable loop by hand (it does not "
                "compile against this SDK)" % ctx.name)
        return
    new = txt.replace("\n#include <rex/system/function_dispatcher.h>  // rexauto: 2nd-module\n",
                      "\n", 1)
    new = re.sub(r"\n// rexauto: extra recompiled module\(s\) linked into this exe\.\n"
                 r"(?:extern const rex::PPCImageInfo \w+PPCImageConfig;\n)+", "\n", new, count=1)
    new = re.sub(r"\n  // rexauto: register the extra module function tables once the\n"
                 r"  // entrypoint's exists, so guest calls into them resolve\.\n"
                 r"  void OnPostSetup\(\) override \{\n.*?\n    \}\n  \}\n",
                 "\n", new, count=1, flags=re.S)
    if new != txt:
        open(app, "w", encoding="utf-8").write(new)
        ctx.log("  legacy extra-module dispatcher removed from %s_app.h" % ctx.name)


def _heal_owners(ctx):
    """The entrypoint view followed by one view per declared companion."""
    return [ctx] + [_module_view(ctx, m) for m in extra_modules(ctx)]


def _split_by_owner(ctx, addrs):
    """[(owner_view, [addrs])]: each address goes to the module whose image
    holds it, read off the XEX header so it works before that module has ever
    been emitted; whatever no companion claims stays with the entrypoint."""
    groups, ranges = [(ctx, [])], []
    for m in extra_modules(ctx):
        rng = _xex_image_range(m["xex"])
        if rng:
            groups.append((_module_view(ctx, m), []))
            ranges.append((rng[0], rng[0] + rng[1], len(groups) - 1))
    for a in addrs:
        groups[next((i for lo, hi, i in ranges if lo <= a < hi), 0)][1].append(a)
    return [(o, mine) for o, mine in groups if mine]


def _build_log_by_owner(ctx, logp):
    """[(owner_view, build_log_path)]: the build log split by which generated/
    directory each compiler line names. A companion's TUs are named after the
    project exactly like the entrypoint's, so heal_boundaries' per-basename grid
    would pin a module's dangling label onto whatever entrypoint function sits
    at that line number. Single-module titles get their log back untouched."""
    mods = extra_modules(ctx)
    if not mods:
        return [(ctx, logp)]
    rest = _heal._read_text(logp).splitlines(True)
    out = []
    for m in mods:
        pat = re.compile(r"generated[\\/]%s[\\/]" % re.escape(m["key"]), re.I)
        mine = [l for l in rest if pat.search(l)]
        rest = [l for l in rest if not pat.search(l)]
        p = os.path.join(ctx.work, "_build.%s.log" % m["key"])
        open(p, "w", encoding="utf-8", errors="replace").write("".join(mine))
        out.append((_module_view(ctx, m), p))
    p = os.path.join(ctx.work, "_build.default.log")
    open(p, "w", encoding="utf-8", errors="replace").write("".join(rest))
    return [(ctx, p)] + out


def _seed_module_functions(ctx, m):
    fns = os.path.join(ctx.port, "%s_functions.toml" % m["key"])
    if _heal.load_overrides(fns):
        return
    # Prefer a seed keyed by the module image's sha256 (collision-proof: "gamelogic"
    # is a generic module name across engines, and a wrong seed registers functions
    # at addresses that aren't code in THAT module). Key-named seeds stay as the
    # legacy fallback (eawebkit).
    seed = None
    try:
        h = hashlib.sha256(open(m["xex"], "rb").read()).hexdigest()[:16]
        cand = os.path.join(HERE, "seeds", "%s_functions.toml" % h)
        if os.path.exists(cand):
            seed = cand
    except OSError:
        pass
    if not seed:
        cand = os.path.join(HERE, "seeds", "%s_functions.toml" % m["key"])
        if os.path.exists(cand):
            seed = cand
    if seed:
        shutil.copyfile(seed, fns)
        ctx.log("  module '%s': seeded cures from rexauto/seeds (%s)"
                % (m["key"], os.path.basename(seed)))
    else:
        _heal.write_overrides(fns, {})


def _inject_pch_into_cmake(ctx):
    """Precompile the entrypoint module's <name>_init.h monolith. Every generated
    recomp TU opens with `#include "<name>_init.h"` -- a huge header (tens of
    thousands of DECLARE_REX_FUNC externs + heavy C++ STL) whose front-end parse
    is otherwise a fixed floor paid once per TU. A PCH parses it ONCE (~20% off
    per-TU compile, small TUs several x). Output-neutral: a PCH caches the parsed
    AST, never the emitted code, so the generated C++ and the binary's .text stay
    byte-identical (codegen gate unaffected). Idempotent; extra modules skip it
    (they include their own init header). Set REXAUTO_NO_PCH=1 to opt out."""
    if os.environ.get("REXAUTO_NO_PCH"):
        return
    if os.path.basename(ctx.gen) != "default":  # extra-module view: no own CMake
        return                                  # target + its init.h lives elsewhere
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    if not os.path.exists(cml):
        return
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    if "target_precompile_headers" in txt:   # already present (manual or prior run)
        return
    if not os.path.exists(os.path.join(ctx.gen, "%s_init.h" % ctx.name)):
        return
    open(cml, "a", encoding="utf-8").write(
        "\n# rexauto-pch: parse the %s_init.h monolith once, not once per TU\n"
        "# (build perf; output-neutral -- a PCH caches the AST, not emitted code).\n"
        "target_precompile_headers(%s PRIVATE\n"
        "    \"${CMAKE_CURRENT_SOURCE_DIR}/generated/default/%s_init.h\")\n"
        % (ctx.name, ctx.name, ctx.name))
    ctx.log("  wired PCH for %s_init.h into CMakeLists" % ctx.name)


def _inject_debug_diet_into_cmake(ctx):
    """RelWithDebInfo builds carry FULL codeview debug info (-g -gcodeview via
    CMake's MSVC debug-format abstraction): variable/type info for tens of
    thousands of generated functions = a ~100MB PDB re-linked on EVERY heal
    round / gate rebuild (~70s a cycle measured on gta_san_andreas).
    -gline-tables-only keeps exactly what our tooling uses -- function symbols +
    line tables (cdb guest stacks like sub_82XXXXXX+off still resolve) -- and
    drops the bulk. Output-neutral for the generated C++ AND for .text: debug
    info only. Appended via target_compile_options so it lands AFTER the
    config-level -g and downgrades it. Idempotent; REXAUTO_FULL_DEBUG=1 opts out."""
    if os.environ.get("REXAUTO_FULL_DEBUG"):
        return
    if os.path.basename(ctx.gen) != "default":  # extra-module view: not its own target
        return
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    if not os.path.exists(cml):
        return
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    if "gline-tables-only" in txt:
        return
    open(cml, "a", encoding="utf-8").write(
        "\n# rexauto-debug-diet: keep function symbols + line tables, drop the\n"
        "# variable/type debug info that bloats the PDB and slows every relink\n"
        "# (build perf; debug-info-only -- .text and codegen stay byte-identical).\n"
        "if(CMAKE_CXX_COMPILER_ID MATCHES \"Clang\")\n"
        "    target_compile_options(%s PRIVATE $<$<CONFIG:RelWithDebInfo>:-gline-tables-only>)\n"
        "endif()\n" % ctx.name)
    ctx.log("  wired -gline-tables-only (RelWithDebInfo) into CMakeLists")


def _cpp_str(s):
    """Escape a Python str (already toml-decoded, so it holds literal backslashes)
    for embedding in a C++ double-quoted string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _appglue_body(ctx, glue):
    """Render the per-section OnPostSetup lines for the appglue.toml sections that
    are present. Returns ('', set()) when glue is empty so nothing is appended and
    no includes are added. Each section emits nothing when absent."""
    lines, includes = [], set()

    ident = glue.get("identity")
    if ident:
        includes.add("rex/system/xam/user_profile.h")
        lines += [
            "    // rexauto: appglue identity -- sign in a stub user so per-user vtable",
            "    // slots are constructed (else a worker thread calls through guest 0x0).",
            "    if (auto* _kernel = runtime()->kernel_state())",
            "      if (auto* _profile = _kernel->user_profile())",
            '        _profile->SetIdentity(%sULL, "%s");'
            % (ident["xuid"], _cpp_str(ident["name"])),
        ]

    aliases = glue.get("aliases")
    if aliases:
        includes.add("rex/filesystem/vfs.h")
        lines.append("    // rexauto: appglue aliases -- content-scheme symbolic links.")
        lines.append("    if (auto* _fs = runtime()->file_system()) {")
        for a in aliases:
            lines.append('      _fs->RegisterSymbolicLink("%s", "%s");'
                         % (_cpp_str(a["scheme"]), _cpp_str(a["target"])))
        lines.append("    }")

    ov = glue.get("overlay")
    if ov:
        includes.add("rex/filesystem/vfs.h")
        includes.add("rex/filesystem/host_path_device.h")
        scheme = ov.get("device_scheme") or "overlay:"
        subdir = ov.get("overlay_subdir") or "vfs_overlay"
        dirs = list(ov.get("fixed_dirs") or [])
        lines += [
            "    // rexauto: appglue overlay -- pre-create the BIG-directory probe",
            "    // dirs the title checks for existence, then mount them as a host",
            "    // device and fan out the guest probe paths to it.",
            "    {",
            '      auto _overlay_root = cache_root() / "%s";' % _cpp_str(subdir),
            "      std::error_code _ec;",
        ]
        for d in dirs:
            lines.append('      std::filesystem::create_directories(_overlay_root / "%s", _ec);'
                         % _cpp_str(d))
        lines += [
            "      auto _overlay_dev ="
            ' std::make_unique<rex::filesystem::HostPathDevice>("%s", _overlay_root);'
            % _cpp_str(scheme),
            "      _overlay_dev->Initialize();",
            "      if (auto* _fs = runtime()->file_system()) {",
            "        _fs->RegisterDevice(std::move(_overlay_dev));",
        ]
        for ln in (ov.get("links") or []):
            lines.append('        _fs->RegisterSymbolicLink("%s", "%s");'
                         % (_cpp_str(ln["guest"]), _cpp_str(ln["target"])))
        lines += ["      }", "    }"]
        if dirs or ov.get("links"):
            includes.add("<filesystem>")

    dlc = glue.get("dlc")
    if dlc:
        lines += [
            "    // rexauto: appglue dlc -- marketplace DLC auto-install. TODO: wire to",
            "    // an SDK InstallMarketplaceDlc(root) helper once it exists; root=%r."
            % dlc.get("root"),
        ]

    tu = glue.get("title_update")
    if tu:
        lines += [
            "    // rexauto: appglue title_update -- TODO: stage TU payloads via an SDK",
            "    // StageTitleUpdate(container, url, payloads) helper once it exists",
            "    // (%d payload(s); per-title manifest from %s_appglue.toml)."
            % (len(tu.get("payloads") or []), ctx.name),
        ]

    if not lines:
        return "", set()
    body = ("\n    // ---- rexauto: appglue (per-title host glue from %s_appglue.toml) ----\n"
            % ctx.name) + "\n".join(lines) + "\n"
    return body, includes


def _inject_app_glue(ctx, mods, glue):
    """Patch src/<name>_app.h: keep the existing extra-module extern/dispatcher
    block verbatim, and append the per-title appglue sections into the SAME
    generated OnPostSetup() body. Idempotent and a strict no-op when both mods and
    glue are empty (caller guards that, but this stays safe regardless)."""
    app = os.path.join(ctx.port, "src", "%s_app.h" % ctx.name)
    if not os.path.exists(app):
        return
    txt = open(app, encoding="utf-8", errors="ignore").read()
    has_mods = "rexauto: 2nd-module" in txt
    has_glue = "rexauto: appglue" in txt
    if has_mods and mods:
        # UPDATE the already-injected block when the module set changed (modules
        # can be added after the first injection -- a later-declared companion or
        # run-heal's zero-touch auto-detection). A stale extern/config list means
        # the new modules' function tables never register at runtime.
        externs_new = "\n".join('extern const rex::PPCImageInfo %sPPCImageConfig;' % m["symbol_prefix"]
                                for m in mods)
        cfgs_new = ", ".join('&%sPPCImageConfig' % m["symbol_prefix"] for m in mods)
        upd = re.sub(r"(for \(const rex::PPCImageInfo\* _cfg : \{ )[^}]*( \}\))",
                     lambda mm: mm.group(1) + cfgs_new + mm.group(2), txt, count=1)
        upd = re.sub(r"(// rexauto: extra recompiled module\(s\) linked into this exe\.\n)"
                     r"(?:extern const rex::PPCImageInfo \w+PPCImageConfig;\n?)+",
                     lambda mm: mm.group(1) + externs_new + "\n", upd, count=1)
        if upd != txt:
            open(app, "w", encoding="utf-8").write(upd)
            ctx.log("  extra-module dispatcher list in %s_app.h updated -> %d module(s)"
                    % (ctx.name, len(mods)))
            txt = upd
    if (has_mods or not mods) and (has_glue or not glue):
        return  # nothing new to inject

    glue_body, glue_includes = _appglue_body(ctx, glue) if glue else ("", set())
    inc = "#include <rex/rex_app.h>"

    # ---- includes after the rex_app.h line (only for what's actually emitted) ----
    if not has_mods and mods:
        externs = "\n".join('extern const rex::PPCImageInfo %sPPCImageConfig;' % m["symbol_prefix"]
                            for m in mods)
        txt = txt.replace(inc,
            inc + "\n#include <rex/system/function_dispatcher.h>  // rexauto: 2nd-module\n\n"
            "// rexauto: extra recompiled module(s) linked into this exe.\n" + externs, 1)
    if not has_glue and glue_includes:
        addl = "".join(
            ("\n#include %s  // rexauto: appglue" % h) if h.startswith("<")
            else ("\n#include <%s>  // rexauto: appglue" % h)
            for h in sorted(glue_includes))
        txt = txt.replace(inc, inc + addl, 1)

    # ---- the OnPostSetup() body: dispatcher block (if any) + appglue block ----
    if has_mods:
        # extra-module hook already present; append glue inside the same body, right
        # before its closing brace (the dispatcher loop's "}\n  }\n").
        if glue_body:
            anchor = "    }\n  }\n"      # end of the dispatcher for-loop + method
            pos = txt.rfind(anchor)
            if pos < 0:                  # hand-folded body; fall back to method end
                pos = txt.rfind("  }\n")
                insert_at = pos
            else:
                insert_at = pos + len("    }\n")
            txt = txt[:insert_at] + glue_body + txt[insert_at:]
    else:
        cfgs = ", ".join('&%sPPCImageConfig' % m["symbol_prefix"] for m in mods)
        hook_open = (
            "\n  // rexauto: register the extra module function tables once the\n"
            "  // entrypoint's exists, so guest calls into them resolve.\n"
            "  void OnPostSetup() override {\n")
        hook_dispatch = (
            "    auto* dispatcher = runtime()->function_dispatcher();\n"
            "    if (!dispatcher) return;\n"
            "    for (const rex::PPCImageInfo* _cfg : { %s }) {\n"
            "      if (!_cfg->func_mappings) continue;\n"
            "      if (!dispatcher->InitializeFunctionTable(_cfg->code_base, _cfg->code_size,\n"
            "                                               _cfg->image_base, _cfg->image_size,\n"
            "                                               /*is_entrypoint=*/false,\n"
            "                                               _cfg->function_table_base))\n"
            "        continue;\n"
            "      for (int i = 0; _cfg->func_mappings[i].guest != 0; ++i)\n"
            "        if (_cfg->func_mappings[i].host)\n"
            "          dispatcher->SetFunction(\n"
            "              static_cast<uint32_t>(_cfg->func_mappings[i].guest),\n"
            "              _cfg->func_mappings[i].host);\n"
            "    }\n" % cfgs) if mods else ""
        if not mods:
            # appglue only: open the hook with a distinct marker comment.
            hook_open = (
                "\n  // rexauto: appglue -- per-title host glue (identity / aliases /\n"
                "  // overlay) from %s_appglue.toml, wired into OnPostSetup.\n"
                "  void OnPostSetup() override {\n" % ctx.name)
        hook = hook_open + hook_dispatch + glue_body + "  }\n"
        idx = txt.rstrip().rfind("};")
        txt = txt[:idx] + hook + txt[idx:]

    open(app, "w", encoding="utf-8").write(txt)
    what = []
    if mods:
        what.append("%d extra module(s)" % len(mods))
    if glue:
        what.append("appglue [%s]" % ", ".join(sorted(glue.keys())))
    ctx.log("  wired %s into %s_app.h" % (" + ".join(what), ctx.name))


def _module_view(ctx, m):
    """A shallow ctx clone whose per-title paths point at an extra module's files,
    so the entrypoint's full IDA pipeline (stage_jumptables / stage_deepextract /
    do_codegen) runs verbatim on the module. Env/work/build paths are inherited
    (same port tree); only the name-derived artifacts diverge. A separate statefile
    keeps the module's stage marks from clobbering the entrypoint's resumable
    state; _jt_image is cleared so jumptables never reuses the entrypoint's dump
    (a different image); log lines are prefixed to stay distinguishable."""
    mc = copy.copy(ctx)
    key = m["key"]
    mc.name = key
    mc.manifest = os.path.join(ctx.port, "%s_manifest.toml" % key)
    mc.functions = os.path.join(ctx.port, "%s_functions.toml" % key)
    mc.switches = os.path.join(ctx.port, "%s_switch_tables.toml" % key)
    mc.forced = os.path.join(ctx.port, "%s_forced_landings.toml" % key)
    mc.gen = os.path.join(ctx.port, "generated", key)
    mc.statefile = os.path.join(ctx.work, ".rexauto_state_%s" % key)
    mc._jt_image = None
    mc._native_parent = ctx  # do_codegen(mc) is the PROJECT codegen; see there
    base_log = ctx.log
    mc.log = lambda msg, _b=base_log, _k=key: _b("[mod:%s] %s" % (_k, msg))
    return mc


def _codegen_module(ctx, m):
    """Recover a companion's jump tables through the same IDA pass the entrypoint
    gets. The module's own manifest is only used to dump its raw image (a project
    codegen dumps the entrypoint's); the C++ itself is emitted by the project
    codegen from the [[modules]] entry _declare_native_modules writes, so nothing
    here emits sources or runs deep-extract -- the run-heal covers the dynamic
    residue, exactly as it does for the entrypoint. One-shot: the recovered
    <key>_switch_tables.toml is the product, so its presence is the skip
    condition (REXAUTO_MODULE_JT=force re-runs it)."""
    mc = _module_view(ctx, m)
    if os.path.exists(mc.switches) and os.path.getsize(mc.switches) > 0 \
            and os.environ.get("REXAUTO_MODULE_JT") != "force":
        mc.log("jump tables already recovered -> skip re-analysis "
               "(REXAUTO_MODULE_JT=force to re-run)")
        mc.t_note(module_jumptables="skip-done")
        mc.timing_skip("jumptables", "skip-done", module=mc.name)
        return
    image = os.path.join(mc.work, "%s_image.bin" % mc.name)
    mc.log("recovering companion jump tables through the IDA pipeline")
    # Dump the raw decompressed image for IDA. This lone-module codegen may well
    # fail validation -- its computed branches are what we are here to recover
    # -- and that is fine: the dump lands before analysis.
    r = rexglue(mc, "--log-level", "trace", "codegen", "--ignore-stamp", mc.manifest,
                env={"REX_DUMP_IMAGE": image}, capture=True)
    blob = (r.stdout or "") + (r.stderr or "")
    if os.path.exists(image):
        bm = re.search(r"base=0x([0-9A-Fa-f]+), size=0x([0-9A-Fa-f]+)", blob)
        base = int(bm.group(1), 16) if bm else 0x82000000
        image_end = base + (int(bm.group(2), 16) if bm else 0x900000)
        secs = re.findall(r"section '([^']+)' at 0x([0-9A-Fa-f]+) size 0x([0-9A-Fa-f]+) exec=(\w+)", blob)
        exec_secs = [(int(a, 16), int(a, 16) + int(sz, 16))
                     for _, a, sz, ex in secs if ex.lower() in ("true", "1")]
        if exec_secs:
            mc._jt_image = {"image": image, "base": base, "image_end": image_end,
                            "exec_secs": exec_secs}
            # A companion can ship its OWN setjmp pair (fifadllzf embeds Lua 5.1)
            # and needs the same special codegen the entrypoint gets. Written to
            # the scan manifest; _declare_native_modules mirrors it into the
            # [[modules]] entry the build reads.
            try:
                import detect_setjmp as _dj
                sres = _dj.detect(image, exec_secs, base)
                slj, ssj = sres.get("longjmp_address"), sres.get("setjmp_address")
                if slj and ssj:
                    _dj.write_addresses(mc.manifest, longjmp=slj, setjmp=ssj)
                    mc.log("module setjmp/longjmp detected -> setjmp=0x%X longjmp=0x%X"
                           % (ssj, slj))
            except Exception as ex:
                mc.log("module setjmp detection skipped (%s)" % ex)
    mc.t_note(module_jumptables="ran")
    with mc.timer("jumptables", phase="module", module=mc.name):
        stage_jumptables(mc)
    if not (os.path.exists(mc.switches) and os.path.getsize(mc.switches) > 0):
        mc.log("no jump tables recovered (fine for a module without bctr tables)")


def _warn_colliding_tables(ctx, mods):
    """Multi-XEX: the runtime places each module's function-pointer dispatch table
    at image_base + image_size. When a companion's image loads right after the
    main's (FIFA Street: main [0x82000000,0x821C0000) + companion at 0x82300000),
    the MAIN's table [0x821C0000,~0x82413000) overlaps the companion image ->
    InitializeFunctionTable fails when the guest loads it -> its functions never
    register -> FATAL on the first inter-module call. 0.8.2 let a manifest
    `function_table_base` move the table; v0.10.0 dropped that knob, so all this
    can do is say it loudly, so the "unregistered function" that follows is not
    chased as a missing cure. Silent for titles whose ranges do not collide."""
    RESERVE = 0x10000  # SDK FunctionDispatcher::kThunkReserveSize
    r = _dx.read_ranges(ctx.gen, ctx.name)
    if not r:
        return
    ib, cb, cs, isz = r
    main_tab = (ib + isz, ib + isz + (cs + RESERVE) * 2)
    spans = [(ib, ib + isz)]  # every image+table span the main table must dodge
    collide = False
    for m in mods:
        mr = _dx.read_ranges(os.path.join(ctx.port, "generated", m["key"]), m["key"])
        if not mr:
            continue
        mib, mcb, mcs, misz = mr
        mod_img = (mib, mib + misz)
        mod_tab = (mib + misz, mib + misz + (mcs + RESERVE) * 2)
        spans += [mod_img, mod_tab]
        for lo, hi in (mod_img, mod_tab):
            if main_tab[0] < hi and main_tab[1] > lo:
                collide = True
    if not collide:
        return
    ctx.log("WARNING: the entrypoint's dispatch table [0x%X, 0x%X) overlaps a companion's "
            "image or table; this SDK cannot relocate it, so that companion will fail to "
            "register when the guest loads it" % main_tab)


def setup_extra_modules(ctx):
    """Declare every companion as a native [[modules]] entry of the entrypoint
    manifest, recover its jump tables, and wire per-title app glue.

    ReXGlue v0.10.0 recompiles a companion into its own shared library (built by
    the codegen-emitted dll_targets.cmake, registered through module_registry.cpp,
    bound by the runtime at the guest's XexLoadImage) and dropped symbol_prefix,
    so the 0.8.2 route -- codegen the module alone, link its sources into the same
    exe, register its table by hand in OnPostSetup -- cannot link any more (two
    PPCImageConfig), and the app.h it generated no longer compiles either (a
    6-argument InitializeFunctionTable). Ports still carrying that glue get it
    stripped here. No-op for single-module titles with no appglue.toml."""
    mods = extra_modules(ctx)
    glue = glue_records(ctx)
    if not mods and not glue:
        return
    for m in mods:
        _author_module_manifest(ctx, m)
        _seed_module_functions(ctx, m)
        with ctx.timer("module", module=m["key"], phase="build"):
            _codegen_module(ctx, m)
    if mods:
        _declare_native_modules(ctx, mods)
        _warn_colliding_tables(ctx, mods)
        _strip_legacy_module_glue(ctx)
    _inject_app_glue(ctx, [], glue)
    _inject_pch_into_cmake(ctx)


def stage_deepextract(ctx, gen_current=False):
    """Static function/vtable recovery: a deep IDA pass on the .i64 the jumptables stage
    produced harvests the function/vtable-target set the linear scan misses (~96% of what
    run-heal would otherwise find by launching the game N times), and the pure-addition
    gate folds only the provably-safe ones into functions.toml BEFORE the first build.
    run-heal stays as the backstop for the genuinely-dynamic residue. Fully additive and
    opt-in on IDA: no idat / no .i64 -> skip (byte-identical to before)."""
    if not (ctx.env.get("idat") and ctx.env.get("jt_repo") and ctx.env.get("python")):
        ctx.log("deep-extract: no IDA/repo/python -> skip (run-heal covers it)")
        return ctx.mark("deepextract", {"skipped": "no-ida"})
    i64 = os.path.join(ctx.work, "%s_image.bin.elf.i64" % ctx.name)
    script = os.path.join(ctx.env["jt_repo"], "src", "deep_extract.py")
    ranges = _dx.read_ranges(ctx.gen, ctx.name)
    if not os.path.exists(i64) or not os.path.exists(script) or not ranges:
        ctx.log("deep-extract: no .i64/script/ranges -> skip (run-heal covers it)")
        return ctx.mark("deepextract", {"skipped": "no-i64-or-ranges"})
    ib, cb, cs, isz = ranges
    funclist = os.path.join(ctx.work, "%s_functions_list.txt" % ctx.name)
    # The known-set must reflect the CURRENT generated sources. For a companion
    # module, stage_jumptables ran before any sources existed (step 2 of
    # _codegen_module) and wrote an EMPTY funclist; feeding that to deep_extract
    # made every already-emitted function a "candidate" (fifadllzf: 92188) and
    # the pure-add gate rightly rejected the lot -- accepted=0, deep-extract a
    # no-op for every companion, real cures (FIFA 0x827838A0) discarded with the
    # noise. Refresh via the same extract_funcs the jumptables stage uses
    # whenever the list is missing/empty but sources exist; healthy entrypoints
    # (non-empty list) are untouched.
    if (not os.path.exists(funclist) or os.path.getsize(funclist) == 0) \
            and os.path.exists(os.path.join(ctx.gen, "sources.cmake")):
        rf = run([ctx.env["python"], os.path.join(ctx.env["jt_repo"], "src", "extract_funcs.py"),
                  ctx.gen, "-o", funclist])
        n = sum(1 for l in open(funclist) if l.strip()) if os.path.exists(funclist) else 0
        ctx.log("deep-extract: refreshed empty funclist from generated sources (%d entries)" % n)
    workcopy = os.path.join(ctx.work, "%s_deepx.i64" % ctx.name)  # NEVER open the original
    cfg = os.path.join(ctx.work, "%s_deepx_cfg.json" % ctx.name)
    outjson = os.path.join(ctx.work, "%s_deepx.json" % ctx.name)
    outtoml = os.path.join(ctx.work, "%s_deepx.toml" % ctx.name)
    # The working copy is its own cost line, not a rounding error: the .i64 is
    # tens to hundreds of MB (299MB on grand_theft_auto_v), and a jumptables CACHE
    # HIT still pays this copy in full -- so it belongs in the ledger next to the
    # IDA seconds the cache did save.
    with ctx.t_op("copy", "i64_copy", src=os.path.basename(i64),
                  bytes=(os.path.getsize(i64) if os.path.exists(i64) else None)):
        shutil.copyfile(i64, workcopy)
    p = lambda x: x.replace("\\", "/")
    json.dump({"image_base": ib, "text_start": cb, "text_end": cb + cs, "image_end": ib + isz,
               "known": p(funclist), "out_toml": p(outtoml), "out_json": p(outjson)},
              open(cfg, "w"))
    ctx.log("deep IDA extraction (funcmap + vtable data-xref) on a .i64 copy")
    if os.path.exists(outjson):
        os.remove(outjson)
    # Separated from the pure-add gate's codegen seconds on purpose: this stage
    # pays BOTH an idat launch and (through the gate) up to eight full-title
    # codegen passes, and until now the mark recorded only candidates/accepted
    # with no cost attached to either half -- so "deepextract is expensive" could
    # never be aimed at the half that actually is.
    with ctx.t_op("ida", "ida", tool="idat", script="deep_extract.py") as op:
        r_dx = run([ctx.env["idat"], "-A", "-S%s %s" % (p(script), p(cfg)),
                    "-L" + p(os.path.join(ctx.work, "%s_deepx_ida.log" % ctx.name)), workcopy])
        op.set(rc=r_dx.returncode)
    if not os.path.exists(outjson):
        ctx.log("deep-extract: IDA produced nothing -> skip")
        return ctx.mark("deepextract", {"skipped": "extract-empty"})
    cands = set(int(x["addr"], 16) for x in json.load(open(outjson)).get("emitted", []))
    # Code that follows an unconditional bctr and that nothing emits. IDA's deep pass does
    # not surface it -- there is no reference TO it; it is simply the next routine in a run
    # of dispatch thunks, orphaned when the recompiler ended the previous one at its bctr.
    # It needs the raw image, not the database, so it is scanned here rather than in IDA.
    fall = _dx.bctr_fallthrough_candidates(
        ctx.gen, ctx.name, os.path.join(ctx.work, "%s_image.bin" % ctx.name), ib, cb, cs)
    if fall:
        ctx.log("bctr-fallthrough scan: %d address(es) of unemitted code after an "
                "unconditional bctr" % len(fall))
        cands |= set(fall)
    cands = sorted(cands - set(_heal.load_overrides_full(ctx.functions)))
    if not cands:
        return ctx.mark("deepextract", {"candidates": 0, "accepted": 0})
    # Route by the recompiler's own emitted grid BEFORE gating: an interior address can
    # never become a function head, so sending it to the pure-add gate guarantees a drop
    # and throws away a real in-function landing. (budokai3: 115 of 116 were interior.)
    cands, interior = _dx.split_by_grid(cands, ctx.gen, ctx.name)
    n_land = 0
    if interior:
        ctx.log("deep-extract: %d of %d candidates are INTERIOR to an emitted function "
                "-> in-function landings" % (len(interior), len(interior) + len(cands)))
        n_land = _dx.landing_gate(
            ctx.name, ctx.gen, ctx.forced, ctx.manifest,
            lambda: rexglue(ctx, "--log-level", "error", "codegen", ctx.manifest, capture=True),
            interior, log=ctx.log, switch_path=ctx.switches)
    if not cands:
        ctx.log("deep-extract: no gap candidates left for the pure-addition gate")
        return ctx.mark("deepextract", {"candidates": len(interior), "accepted": 0,
                                        "landings": n_land})
    ctx.log("deep-extract: %d gap candidate(s) -> pure-addition gate" % len(cands))
    # baseline_current decides whether the gate pays its opening baseline probe (a
    # whole extra full-module codegen). It is the single biggest warm/cold switch
    # inside this stage, so it is recorded next to the seconds rather than left to
    # be inferred from the codegen pass count.
    ctx.t_note(candidates=len(cands), gate_baseline_skipped=bool(gen_current))
    accepted = _dx.pure_add_gate(
        ctx.env["rexglue"], ctx.port, ctx.name, ctx.manifest, ctx.gen, ctx.functions, cands,
        codegen_fn=lambda: rexglue(ctx, "--log-level", "error", "codegen", ctx.manifest,
                                   capture=True),
        log=ctx.log, baseline_current=gen_current)
    if accepted:
        _heal.register_functions(accepted, ctx.functions)  # additive {} superset-only
    ctx.log("deep-extract: +%d functions folded (pure additions), +%d landings; %d dropped, "
            "run-heal backstops the rest"
            % (len(accepted), n_land, len(cands) - len(accepted)))
    return ctx.mark("deepextract", {"candidates": len(cands) + len(interior),
                                    "accepted": len(accepted), "landings": n_land})


def _codegen_ranges(ctx):
    """(image_base, code_base, code_size) from the generated tree, or None.

    Do NOT read only <name>_init.h. ReXGlue 0.8.2 puts these defines there and
    v0.10.0 puts them in <name>_pch.h, so the hard-coded filename turned both the
    pointer scan and the gap fill into silent no-ops on the newer SDK -- the
    regex raised, the except swallowed it, and the pass just returned nothing.
    Scan the emitted headers for whichever one carries them."""
    import glob as _glob
    seen = []
    for pat in ("%s_init.h" % ctx.name, "%s_pch.h" % ctx.name, "*.h"):
        for f in sorted(_glob.glob(os.path.join(ctx.gen, pat))):
            if f in seen:
                continue
            seen.append(f)
            try:
                h = _heal._read_text(f)
            except OSError:
                continue
            mi = re.search(r"REX_IMAGE_BASE 0x([0-9A-Fa-f]+)", h)
            mb = re.search(r"REX_CODE_BASE 0x([0-9A-Fa-f]+)", h)
            ms = re.search(r"REX_CODE_SIZE 0x([0-9A-Fa-f]+)", h)
            if mi and mb and ms:
                return (int(mi.group(1), 16), int(mb.group(1), 16), int(ms.group(1), 16))
    return None


# The save/restore helper tables (__savegprlr_14 .. __restvmx_64) are code the
# recompiler never emits as functions: every bl/b into them becomes an
# intrinsic. That makes them the one stretch of the code range that is real code
# AND legitimately uncovered -- exactly what the gap fill looks for. rexauto
# registered all eight heads on Forza Horizon (plus one interior entry of
# __savevmx_64); the SDK lets a CONFIG entry outrank its HELPER detection, so
# 472 + 669 call sites turned into calls of an emitted `sub_82A7DDD0` /
# `sub_82A7DE20`, and the game booted to a null read in a routine whose
# instructions were otherwise byte-identical to the 0.8.2 build that reached
# gameplay. These are the signatures the SDK's detectSaveRestoreHelpers uses;
# the table sizes were checked against the eight addresses it reported.
_HELPER_HEADS = (
    (0xF9C1FF68, None, "__savegprlr_14", 18 * 4 + 8),    # std r14..r31; stw r12; blr
    (0xE9C1FF68, None, "__restgprlr_14", 18 * 4 + 12),   # ld r14..r31; lwz r12; mtlr r12; blr
    (0xD9CCFF70, None, "__savefpr_14", 18 * 4 + 4),      # stfd f14..f31; blr
    (0xC9CCFF70, None, "__restfpr_14", 18 * 4 + 4),      # lfd f14..f31; blr
    (0x3960FEE0, 0x7DCB61CE, "__savevmx_14", 18 * 8 + 4),
    (0x3960FEE0, 0x7DCB60CE, "__restvmx_14", 18 * 8 + 4),
    (0x3960FC00, 0x100B61CB, "__savevmx_64", 64 * 8 + 4),
    (0x3960FC00, 0x100B60CB, "__restvmx_64", 64 * 8 + 4),
)


def _helper_tables(ctx):
    """[(lo, hi, name)] of the save/restore helper tables in this module's image,
    read from the same dump + ranges the pointer scan uses; [] when either is
    missing. Cached on the ctx: the image does not change under a run."""
    cached = getattr(ctx, "_helper_tables_cache", None)
    if cached is not None:
        return cached
    out = []
    rng = _codegen_ranges(ctx)
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    if rng and os.path.exists(image):
        import struct
        ib, cb, cs = rng
        try:
            with open(image, "rb") as fh:
                fh.seek(cb - ib)
                data = fh.read(cs)
            n = len(data) // 4
            words = struct.unpack(">%dI" % n, data[:n * 4])
            found = set()
            for i, w in enumerate(words):
                for head, second, name, size in _HELPER_HEADS:
                    if w != head or name in found:
                        continue
                    if second is not None and (i + 1 >= n or words[i + 1] != second):
                        continue
                    found.add(name)
                    out.append((cb + 4 * i, cb + 4 * i + size, name))
        except (OSError, struct.error):
            out = []
    ctx._helper_tables_cache = out
    return out


def _in_helper(ctx, addr):
    return any(lo <= addr < hi for lo, hi, _ in _helper_tables(ctx))


def _detect_xapi_fibers(ctx):
    """Map the title's XAPI fiber routines to the SDK's host implementations.

    A guest fiber switch cannot survive static recompilation as-is: XAPI's
    SwitchToFiber saves the context into the current fiber and TAIL-CALLS
    KeSetCurrentStackPointers, whose `blr` was meant to land in the other
    fiber. Recompiled, that is a C++ `return` into the OLD fiber's host frames
    carrying the NEW fiber's registers -- Forza Horizon read NULL through r20
    five seconds into boot, in a routine emitted byte-identical to the 0.8.2
    build (whose runtime fork re-entered guest code after the switch instead).
    ReXGlue v0.10.0 ships host fibers behind [rexcrt] hooks (kernel/crt/
    threading.cpp) and replaces the five XAPI routines when the manifest names
    their addresses. Nothing detects them, so this does, from the emitted
    sources, by the shape XAPI gives them:

      ConvertThreadToFiber  reads KTHREAD.fiber_ptr (r13+0x100 -> +0x164) and
                            fails with ERROR_ALREADY_FIBER (li r3,1280)
      ConvertFiberToThread  clears fiber_ptr, fails with ERROR_ALREADY_THREAD
                            (li r3,1281)
      CreateFiber           the one caller of MmCreateKernelStack
      DeleteFiber           the one caller of MmDeleteKernelStack
      SwitchToFiber         opens with lwz rA,256(r13); lwz rB,356(rA) and
                            calls KeSetCurrentStackPointers (FiberStart shares
                            its tail but opens with mr r3,r31; bl FiberEntry)

    All five or nothing: the host hooks only know fibers the host CreateFiber
    registered, so a partial map would make SwitchToFiber a silent no-op.
    Writes port/<name>_rexcrt.toml and includes it; returns True when the
    include is new (the caller re-runs codegen). Idempotent."""
    import glob as _glob
    if os.path.basename(ctx.gen) != "default":
        return False  # XAPI lives in the entrypoint; companions never carry it
    path = os.path.join(ctx.port, "%s_rexcrt.toml" % ctx.name)
    if os.path.exists(path) and os.path.basename(path) in _heal._read_text(ctx.manifest):
        return False  # already mapped: the routines are no longer in the sources to find
    DEF = re.compile(r"^DEFINE_REX_FUNC\((sub_[0-9A-F]{8})\)", re.M)
    bodies = {}
    for fp in _glob.glob(os.path.join(ctx.gen, "*recomp*.cpp")):
        t = _heal._read_text(fp)
        pos = [(m.start(), m.group(1)) for m in DEF.finditer(t)]
        for i, (st, name) in enumerate(pos):
            en = pos[i + 1][0] if i + 1 < len(pos) else len(t)
            bodies[name] = t[st:en]
    if not bodies:
        return False

    def instrs(b):
        return [l.strip()[3:] for l in b.splitlines() if l.lstrip().startswith("// ")]

    def sole_caller(imp):
        c = [n for n, b in bodies.items() if ("__imp__%s(ctx" % imp) in b]
        return c[0] if len(c) == 1 else None

    def thread_fiber(b):
        return re.search(r"// lwz r\d+,256\(r13\)", b) and re.search(r"// lwz r\d+,356\(r\d+\)", b)

    found = {}
    found["CreateFiber"] = sole_caller("MmCreateKernelStack")
    found["DeleteFiber"] = sole_caller("MmDeleteKernelStack")
    c2t = [n for n, b in bodies.items() if thread_fiber(b) and "// li r3,1280" in b and len(instrs(b)) < 80]
    f2t = [n for n, b in bodies.items() if thread_fiber(b) and "// li r3,1281" in b
           and re.search(r"// stw r\d+,356\(r\d+\)", b) and len(instrs(b)) < 80]
    sw = []
    for n, b in bodies.items():
        if "__imp__KeSetCurrentStackPointers(ctx" not in b:
            continue
        ins = instrs(b)
        if len(ins) >= 2 and re.match(r"lwz r\d+,256\(r13\)", ins[0]) and re.match(r"lwz r\d+,356\(r\d+\)", ins[1]):
            sw.append(n)
    found["ConvertThreadToFiber"] = c2t[0] if len(c2t) == 1 else None
    found["ConvertFiberToThread"] = f2t[0] if len(f2t) == 1 else None
    found["SwitchToFiber"] = sw[0] if len(sw) == 1 else None
    missing = [k for k, v in found.items() if not v]
    if missing:
        if len(missing) < 5:
            ctx.log("  XAPI fibers: %s found but not %s -> leaving the guest routines alone "
                    "(the host hooks need all five)" % (", ".join(k for k in found if found[k]),
                                                       ", ".join(missing)))
        return False
    body = ("# XAPI fiber routines of this title, replaced by the ReXGlue host implementations\n"
            "# (src/kernel/crt/threading.cpp). Written by rexauto from the emitted sources:\n"
            "# a recompiled SwitchToFiber returns into the old fiber's host frames with the new\n"
            "# fiber's registers, so the switch has to happen on the host.\n"
            "[rexcrt]\n" + "".join("%s = 0x%s\n" % (k, found[k][4:]) for k in
                                    ("ConvertThreadToFiber", "ConvertFiberToThread", "CreateFiber",
                                     "DeleteFiber", "SwitchToFiber")))
    before = _heal._read_text(path) if os.path.exists(path) else None
    if before != body:
        open(path, "w", encoding="utf-8").write(body)
    man = _heal._read_text(ctx.manifest)
    fresh = os.path.basename(path) not in man
    _heal.ensure_manifest_include(ctx.manifest, os.path.basename(path))
    if fresh or before != body:
        ctx.log("  XAPI fibers: %s -> host fibers via [rexcrt] (%s)"
                % (", ".join("%s=%s" % (k[:6], found[k][4:]) for k in found), os.path.basename(path)))
    return fresh or before != body


def _crash_since(ctx, since_ts):
    """'0xC0000005 in sub_XXXXXXXX' for a crash the runtime's handler wrote
    beside the exe after since_ts, symbolized through the port's PDB when
    llvm-symbolizer sits next to clang; None when there is no newer report.
    An access violation in guest code leaves no invalid-function fatal, so
    without this a production run that dies at 2s reads as "other stop" and
    the loop calls the port converged."""
    path = os.path.join(ctx.builddir, "rexglue-crash.txt")
    if not os.path.exists(path):
        return None
    blocks = re.split(r"(?m)^=== ", _heal._read_text(path))[1:]
    for b in reversed(blocks):
        m = re.match(r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\s+pid (\d+)", b)
        if not m:
            continue
        ts = time.mktime(tuple(int(x) for x in m.groups()[:6]) + (0, 0, -1))
        if ts + 1 < since_ts:
            return None
        code = re.search(r"unhandled exception: code (0x[0-9A-Fa-f]+)", b)
        exe = os.path.basename(ctx.exe)
        frames = re.findall(r"^\s*\d+\s+%s\s+\+0x([0-9A-Fa-f]+)" % re.escape(exe), b, re.M)
        where = ("+0x%s" % frames[0]) if frames else "outside the recompiled code"
        if frames:
            sym = os.path.join(os.path.dirname(ctx.env.get("clang") or ""), "llvm-symbolizer.exe")
            if os.path.exists(sym):
                try:
                    r = subprocess.run([sym, "--obj=" + ctx.exe, "--relative-address",
                                        "--functions=short", "--no-inlines", "0x" + frames[0]],
                                       capture_output=True, text=True, timeout=120)
                    lines = (r.stdout or "").strip().splitlines()
                    if lines and "sub_" in lines[0]:
                        where = lines[0].replace("__imp__", "")
                except (OSError, subprocess.SubprocessError):
                    pass
        return "%s in %s" % (code.group(1) if code else "crash", where)
    return None


def _materialized_pointer_scan(words, code_base, code_size):
    """{target: use} for every code address the code builds in a register pair.

    A callback handed to a registration routine, a handler stored into an object,
    a function whose address is taken at all: the compiler builds it as
    `lis rA,hi` ... `addi rB,rA,lo` (or `ori`), and no dword anywhere in the
    image ever holds the value -- so the data-pointer scan cannot see it, the
    recompiler's flow-following scan never reaches it, and the run-heal finds
    each one by launching the game and dying on it. Forza Horizon's heal cured
    14 such functions one launch at a time (0x8249D410, 0x8249CA20, ...); every
    one of them is materialised this way and passed straight to a `bl`, none
    appears in data, and the same shape names 38 siblings the heal had not got
    to yet. `use` says what the register was used for afterwards, so a data
    address that merely lives inside the code section (an embedded table) can be
    told apart: only a value that is stored, moved to CTR, or handed to a call
    as an argument counts as a function pointer."""
    n = len(words)
    out = {}
    for i, w in enumerate(words):
        if (w & 0xFC1F0000) != 0x3C000000:          # lis rD,hi  (addis rD,0,hi)
            continue
        rd = (w >> 21) & 31
        hi = w & 0xFFFF
        for j in range(i + 1, min(i + 33, n)):
            w2 = words[j]
            o = w2 >> 26
            if o == 14 and ((w2 >> 16) & 31) == rd:  # addi rX,rD,lo (signed)
                lo = w2 & 0xFFFF
                t = ((hi << 16) + (lo - 0x10000 if lo >= 0x8000 else lo)) & 0xFFFFFFFF
                rx = (w2 >> 21) & 31
            elif o == 24 and ((w2 >> 21) & 31) == rd:  # ori rX,rD,lo
                t = ((hi << 16) | (w2 & 0xFFFF)) & 0xFFFFFFFF
                rx = (w2 >> 16) & 31
            elif o in (14, 15, 24) and ((w2 >> 21) & 31) == rd:
                break                                   # rD rewritten first
            else:
                continue
            if not (code_base <= t < code_base + code_size) or (t & 3):
                break
            use = None
            for m in range(j + 1, min(j + 8, n)):
                w3 = words[m]
                o3 = w3 >> 26
                if o3 in (36, 37, 62) and ((w3 >> 21) & 31) == rx:
                    use = "store"                          # stw/stwu/std rX,d(rY)
                elif o3 == 31 and ((w3 >> 1) & 0x3FF) in (151, 183) and ((w3 >> 21) & 31) == rx:
                    use = "store"                          # stwx/stwux rX
                elif (w3 & 0xFC1FFFFF) == (0x7C0903A6 | rx << 21):
                    use = "mtctr"
                elif o3 in (32, 33, 34, 35, 40, 42, 44, 46, 48, 50, 52, 54, 58, 62) \
                        and ((w3 >> 16) & 31) == rx:
                    use = "base"                           # rX used as an address base
                elif o3 in (14, 15, 24, 32, 33, 34, 35, 40, 42, 58) and ((w3 >> 21) & 31) == rx:
                    use = "rewritten"
                elif o3 == 18 and (w3 & 1):
                    use = "arg" if 3 <= rx <= 10 else "call"  # bl with rX still live
                else:
                    continue
                break
            if use in ("store", "mtctr", "arg"):
                out[t] = use
            break
    return out


def _materialized_scan_register(ctx):
    """Register the function pointers the code materialises (see
    _materialized_pointer_scan). Same inputs and the same three guards as the
    data-pointer scan: never an address that is already a function, never an
    interior of an emitted routine (that would split it), never a save/restore
    helper. Returns the addresses newly written to functions.toml; the caller
    re-runs codegen and drops whatever it declined to define."""
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    rng = _codegen_ranges(ctx)
    if not (os.path.exists(image) and rng):
        return []
    import struct
    ib, cb, cs = rng
    try:
        with open(image, "rb") as fh:
            fh.seek(cb - ib)
            data = fh.read(cs)
        n = len(data) // 4
        words = struct.unpack(">%dI" % n, data[:n * 4])
    except (OSError, struct.error):
        return []
    found = _materialized_pointer_scan(words, cb, n * 4)
    if not found:
        return []
    defined, labels = _heal._emitted_symbols(ctx.gen)
    known = set(_heal.load_overrides_full(ctx.functions))
    new = sorted(a for a in found
                 if a not in defined and a not in labels and a not in known and not _in_helper(ctx, a))
    try:
        _, _starts = _heal.func_grid(ctx.gen)
        _ranges = _closure.covered_ranges(ctx.gen)
    except Exception:
        _starts, _ranges = [], []
    if _starts and _ranges:
        import bisect as _bis
        _lo = [r[0] for r in _ranges]
        keep = []
        for a in new:
            k = _bis.bisect_right(_lo, a) - 1
            if 0 <= k and _ranges[k][0] <= a < _ranges[k][1]:
                j = _bis.bisect_right(_starts, a) - 1
                if j >= 0 and _starts[j] < a:
                    continue      # interior of an emitted routine: a landing, not a head
            keep.append(a)
        new = keep
    if new:
        _heal.register_functions(new, ctx.functions)
    return new


def _gap_fill_register(ctx):
    """Register the start of every stretch of the code range that carries
    instructions and has no emitted C++ behind it.

    The coverage measurement already knows exactly which bytes came out of
    codegen; this closes the loop by feeding the gaps back as function starts.
    Two kinds of gap are skipped because there is nothing there to recompile:

      - alignment padding between functions (only 0x00000000 / `nop`), which is
        the overwhelming majority -- on Gears of War Judgment 112,832 bytes of
        it, against 11,064 bytes of real code;
      - the import thunk table, whose entries are two data words followed by
        `mtctr`/`bctr` and are resolved by the runtime, not recompiled.

    Judgment went 11,064 -> 5,672 -> 5,864 bytes of uncovered code over three
    rounds and 59,761 -> 60,137 functions. Returns the addresses registered."""
    import array
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    rng = _codegen_ranges(ctx)
    if not (os.path.exists(image) and rng):
        return []
    ib, cb, cs = rng
    try:
        with open(image, "rb") as f:
            raw = f.read()
        w = array.array("I")
        w.frombytes(raw[:len(raw) - (len(raw) % 4)])
        if sys.byteorder == "little":
            w.byteswap()
    except Exception:
        return []

    def word(a):
        o = (a - ib) // 4
        return w[o] if 0 <= o < len(w) else None

    merged = _closure.covered_ranges(ctx.gen)
    gaps, pos = [], cb
    for st, en in merged:
        if st > pos:
            gaps.append((pos, st))
        pos = max(pos, en)
    if pos < cb + cs:
        gaps.append((pos, cb + cs))
    known = set(_heal.load_overrides_full(ctx.functions))
    out = []
    for st, en in gaps:
        ws = [word(a) for a in range(st, en, 4)]
        if all(x in (0, 0x60000000, None) for x in ws):
            continue                                  # alignment padding
        if any(x == 0x7D6903A6 for x in ws) and any(x == 0x4E800420 for x in ws):
            continue                                  # import thunk table
        if any(lo < en and st < hi for lo, hi, _ in _helper_tables(ctx)):
            continue                                  # save/restore helper table
        if st not in known:
            out.append(st)
    if out:
        _heal.register_functions(out, ctx.functions)
    return out


def _pointer_scan_register(ctx):
    """Register the function pointers sitting in the image's data sections.

    Reads the image + ranges the pipeline already has (<work>/<name>_image.bin,
    written by the setjmp/jumptables stages, and REX_IMAGE_BASE / REX_CODE_BASE /
    REX_CODE_SIZE from the generated header). No-op and silent when either is
    absent, so a port that never dumped an image is unaffected.

    Only addresses that are neither an emitted function nor an emitted `loc_`
    label are added, so this can never split a routine -- registering an interior
    landing as a function is what made Judgment die 0.7s into every launch.
    Returns the addresses newly written to functions.toml."""
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    rng = _codegen_ranges(ctx)
    if not (os.path.exists(image) and rng):
        return []
    ib, cb, cs = rng
    new, lab = _heal.data_pointer_scan(image, ctx.gen, ib, cb, cs)
    full = _heal.load_overrides_full(ctx.functions)
    known = set(full)
    new = [a for a in new if a not in known and not _in_helper(ctx, a)]

    # A pointer target that lands INSIDE a routine's body is a sub-entry, never a
    # new function -- registering it as one splits the routine, and the boundary
    # heal then has to extend some other function across it, which is an overlap
    # rexglue refuses ("Overlapping boundaries"). That is what took Forza Horizon
    # from building at v2.27.0 to a dead codegen once this scan started running:
    # 0x82AA6414, 0x641C, 0x6420 and 0x6444 all sit inside sub_82AA62FC.
    #
    # The docstring above already promised this ("can never split a routine") and
    # only delivered it for addresses that are emitted `loc_` labels. An interior
    # address that no label names slipped through. Chunk it instead: the entry
    # still exists, so the pointer still resolves, and the routine stays whole.
    interior = {}
    try:
        _, _starts = _heal.func_grid(ctx.gen)
        _ranges = _closure.covered_ranges(ctx.gen)
    except Exception:
        _starts, _ranges = [], []
    if _starts and _ranges:
        import bisect as _bis
        # covered_ranges hands back lists, so index the starts column rather than
        # bisecting the pairs -- comparing a tuple against a list raises.
        _lo = [r[0] for r in _ranges]
        for a in new:
            k = _bis.bisect_right(_lo, a) - 1
            if k < 0 or not (_ranges[k][0] <= a < _ranges[k][1]):
                continue          # in a gap: a genuinely new function, keep it
            j = _bis.bisect_right(_starts, a) - 1
            if j >= 0 and _starts[j] < a:
                interior[a] = _starts[j]
    if interior:
        # Do not register them at all -- not as functions (that splits the
        # routine and the boundary heal then overlaps it) and not as chunks
        # either (a chunk inside a routine that also `goto`s to the address
        # leaves the label undefined under v0.10.0). If the game ever takes one
        # of these addresses at runtime, the run-heal registers it from the
        # fatal, which is exactly how the 0.8.2 port of Forza Horizon converged
        # with zero cures in this region.
        new = [a for a in new if a not in interior]
        ctx.log("  pointer scan: %d target(s) sit inside an existing routine -> "
                "left to the run-heal, not registered" % len(interior))
    # A pointer whose target is only a `loc_` inside another function is still an
    # entry the game takes the address of. Registering it as a function would
    # split the owner, so it is cured as a CHUNK -- which is how ReXGlue v0.10.0
    # ends up with 51 functions on Dante's Inferno that we did not have, 40 of
    # them sitting in the data as plain pointers we were throwing away.
    chunked = 0
    for a, owner in sorted(lab.items()):
        if a in known:
            continue
        full[a] = {"end": None, "parent": owner, "size": None, "name": None}
        chunked += 1
    if chunked:
        _heal.write_overrides_full(ctx.functions, full)
    if new:
        _heal.register_functions(new, ctx.functions)
    return new + sorted(a for a in lab if a not in known)


def apply_game_patches(ctx):
    """Grava no port os patches da comunidade escolhidos, antes do codegen.

    Tem de rodar ANTES do codegen porque quase todo patch do catalogo escreve em
    .text: num recompilador estatico a instrucao e traduzida uma vez e vira
    codigo nativo, entao um patch que chegue depois nao muda mais nada. E o
    motivo de nao existir um liga/desliga instantaneo para eles.

    Sem --patch nem --no-patches nao mexe em nada: a selecao ja gravada no port
    (pela GUI ou a mao) e a verdade, e um build comum nao pode apaga-la.
    """
    wanted = getattr(ctx.args, "patch", None)
    if not wanted and not getattr(ctx.args, "no_patches", False):
        applied = _gamepatches.applied_names(ctx.port)
        if applied:
            ctx.log("game patches: %d ja aplicado(s) no port (%s)"
                    % (len(applied), ", ".join(sorted(applied))))
        return

    # A classificacao e o "expect" leem as faixas dos headers gerados; num
    # projeto novo eles ainda nao existem. Um codegen de partida resolve, e o
    # loop de build regenera em seguida de qualquer jeito.
    if not glob.glob(os.path.join(ctx.gen, "*.h")):
        ctx.log("game patches: gerando uma vez para descobrir as faixas do modulo")
        do_codegen(ctx)

    try:
        r = _gamepatches.apply(ctx.port, wanted or [])
    except (ValueError, SystemExit) as e:
        raise SystemExit("game patches: %s" % e)
    except Exception as e:
        raise SystemExit("game patches: %s: %s" % (type(e).__name__, e))

    if not r["patches"]:
        ctx.log("game patches: nenhum (port limpo)")
    else:
        ctx.log("game patches: %d aplicado(s), %d escrita(s) -> %s"
                % (len(r["patches"]), r["writes"], ", ".join(r["files"])))
        for n in r["patches"]:
            ctx.log("  + %s" % n)
    ctx._game_patches = r["patches"]


def verify_switch_tables(ctx, switches=None, work=None):
    """Repair a [[switch_tables]] block whose `labels` array no longer matches the
    jump table IDA recovered.

    The array is POSITIONAL. rexglue v0.10.0 lowers a bctr as
    `switch (index) { case i: goto labels[i]; }`, so ONE extra or missing label
    silently re-points every case after it: the port still builds, still runs,
    and dispatches to the wrong block forever. v0.8.2 keyed the same switch on
    the computed CTR value (`case 0xTARGET:`), where a damaged array was
    harmless -- which is why ports carried the damage through a fleet of green
    runs and only started rendering garbage on the new SDK.

    Captain America: its Bink block decoder (bctr 0x82871704) had 11 labels for
    a 10-entry table, the extra one being the table's own address at index 0.
    Every block type decoded with the handler of the type below it -- video
    garbage, audio untouched. 43 of the port's 242 tables were damaged the same
    way, by the landing-merge this file's heal once did with `sorted(set(...))`
    (disabled since; see heal.merge_jt_landings). The damage outlives the fix
    because the jump-table stage is skipped whenever the .toml already exists,
    so it is repaired here, before every codegen.

    jumptables.json (the IDA pass's own output) is the source of truth, and is
    itself cross-checked against the image bytes at each table's address when
    both are available. Refuses to touch anything if the json describes a
    different module. Returns the number of tables repaired (0 = no-op, and the
    file is left byte-identical)."""
    switches = switches or ctx.switches
    work = work or ctx.work
    js = os.path.join(work, "jumptables.json")
    if not (os.path.exists(switches) and os.path.exists(js)):
        return 0
    rows = {}
    try:
        for line in open(js):
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if "bctr" in o and "targets" in o:
                rows[o["bctr"]] = o
    except (OSError, ValueError):
        return 0
    if not rows:
        return 0
    try:
        txt = open(switches).read()
    except OSError:
        return 0
    blocks = list(re.finditer(
        r"(\[\[switch_tables\]\]\s*address\s*=\s*0x([0-9A-Fa-f]+)\s*register\s*=\s*\d+\s*labels\s*=\s*)"
        r"\[([^\]]*)\]", txt))
    if not blocks:
        return 0
    known = sum(1 for m in blocks if int(m.group(2), 16) in rows)
    if known * 2 < len(blocks):
        # jumptables.json belongs to another module (module views share ctx.work).
        return 0
    img = os.path.join(work, "%s_image.bin" % ctx.name)
    blob = None
    if os.path.exists(img):
        try:
            blob = open(img, "rb").read()
        except OSError:
            blob = None
    base = 0x82000000
    try:
        m = re.search(r"base_address\s*=\s*(0x[0-9A-Fa-f]+|\d+)", open(ctx.manifest).read())
        if m:
            base = int(m.group(1), 0)
    except OSError:
        pass

    def image_says(row):
        """The table as the image reads it, or None when it cannot be read there."""
        tbl = row.get("table")
        if blob is None or not tbl:
            return None
        off = tbl - base
        n = len(row["targets"])
        if off < 0 or off + 4 * n > len(blob):
            return None
        return list(struct.unpack(">%dI" % n, blob[off:off + 4 * n]))

    # Is the image usable as a second opinion at all? A dumped image is not always
    # the flat base-relative mapping this assumes (FIFA Street's 1.8 MB dump does
    # not cover tables that sit 0x27C0 past the base), and there a mismatch means
    # "read the wrong bytes", not "the recovery is stale". Calibrate on the blocks
    # that are already intact: if the image confirms those, a disagreement
    # elsewhere is real and vetoes the repair; if it confirms almost none, the
    # image is not a mapping we understand and jumptables.json stands alone.
    agree = seen = 0
    for m in blocks:
        row = rows.get(int(m.group(2), 16))
        if not row:
            continue
        cur = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", m.group(3))]
        if cur != row["targets"]:
            continue
        got = image_says(row)
        if got is None:
            continue
        seen += 1
        agree += (got == row["targets"])
    image_usable = seen >= 4 and agree * 2 >= seen

    def json_matches_image(row):
        if not image_usable:
            return True
        got = image_says(row)
        return got is None or got == row["targets"]

    fixed = []
    for m in reversed(blocks):                  # reversed: earlier spans stay valid
        row = rows.get(int(m.group(2), 16))
        if not row:
            continue
        cur = [int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", m.group(3))]
        want = row["targets"]
        if cur == want or not want or not json_matches_image(row):
            continue
        body = ", ".join("0x%08X" % a for a in want)
        txt = txt[:m.start()] + m.group(1) + "[" + body + "]" + txt[m.end():]
        fixed.append((int(m.group(2), 16), len(cur), len(want)))
    if not fixed:
        return 0
    open(switches, "w").write(txt)
    fixed.reverse()
    ctx.log("switch tables repaired: %d of %d had labels that no longer match the "
            "recovered table (a positional array: one wrong label re-points every "
            "case after it)" % (len(fixed), len(blocks)))
    for addr, had, now in fixed[:8]:
        ctx.log("  bctr 0x%08X: %d labels -> %d" % (addr, had, now))
    if len(fixed) > 8:
        ctx.log("  ... e mais %d" % (len(fixed) - 8))
    ctx.t_note(switch_tables_repaired=len(fixed))
    return len(fixed)


def stage_build(ctx):
    miss = [k for k in ("vcvars", "clang", "clangxx", "sdk") if not ctx.env[k]]
    if miss:
        raise SystemExit("missing build tools: %s (set via env vars or install)" % ", ".join(miss))
    write_game_icon(ctx)
    bat = write_build_bat(ctx)
    if not _heal.load_overrides(ctx.functions):  # fresh project -> seed from the shared gabarito
        fetch_gabarito(ctx)
    _migrate_legacy_app_header(ctx)  # 0.8.2-era src/<name>_app.h; no-op once migrated
    verify_switch_tables(ctx)  # positional labels vs the recovered table; no-op when intact
    setup_extra_modules(ctx)   # codegen + wire any extra recompiled modules (no-op if none)
    apply_game_patches(ctx)    # community patches land in the image before codegen
    last_ends = None
    oom_parallel = None
    skip_codegen = False
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        ctx.log("codegen + build (attempt %d/%d)" % (attempt, MAX_BUILD_ATTEMPTS))
        if skip_codegen:
            skip_codegen = False  # OOM retry: generated/ is already current
        else:
            do_codegen(ctx)
            # Fibers cannot be recompiled (see _detect_xapi_fibers); hand the
            # XAPI routines to the host before anything else looks at the tree.
            if _detect_xapi_fibers(ctx):
                do_codegen(ctx)
            # Data-section function-pointer scan. vtable entries, callback arrays
            # and handler tables are dwords in DATA holding code addresses; the
            # recompiler's scan follows control flow and never sees them, so the
            # run-heal finds them one launch-and-crash at a time. Read them out of
            # the image instead -- on Judgment 35 of its 46 cures are in there.
            # Runs BEFORE the trap loop below, because registering a function can
            # expose new unresolved branches and the trap loop is what cures those;
            # with the order reversed Judgment came out of the scan with 17 fresh
            # holes and static closure fell off 100%.
            # REXAUTO_NO_PTRSCAN=1 disables it (the regression gate wants codegen
            # held byte-identical to a baseline that predates this).
            if not os.environ.get("REXAUTO_NO_PTRSCAN"):
                _added = _pointer_scan_register(ctx)
                if _added:
                    ctx.log("  pointer scan: +%d function(s) found in the image's "
                            "data sections; re-running codegen" % len(_added))
                    ctx._cure_origin = getattr(ctx, "_cure_origin", {})
                    ctx._cure_origin["pointer_scan"] =                         ctx._cure_origin.get("pointer_scan", 0) + len(_added)
                    do_codegen(ctx)
                    # Keep only what codegen actually DEFINED. A pointer can land
                    # in a region codegen declines to translate -- the import
                    # thunk table is full of 16-byte stubs that look exactly like
                    # code pointers -- and those become `undefined symbol:
                    # sub_XXXXXXXX` at link. ReXGlue's own version of this scan
                    # skips the thunk range via importThunkTableStart(); we have
                    # no such accessor from out here, so verify against the
                    # emitted output instead, which covers that case and every
                    # other one it cannot name.
                    _defined, _ = _heal._emitted_symbols(ctx.gen)
                    _bad = [a for a in _added if a not in _defined]
                    if _bad:
                        _ov = _heal.load_overrides_full(ctx.functions)
                        for a in _bad:
                            _ov.pop(a, None)
                        _heal.write_overrides_full(ctx.functions, _ov)
                        ctx.log("  pointer scan: %d of them produced no definition "
                                "(thunks / untranslated regions) -> dropped, "
                                "re-running codegen" % len(_bad))
                        do_codegen(ctx)
                # Function pointers the code BUILDS instead of storing: the
                # other half of what the data scan finds, and on Forza Horizon
                # the whole of what its run-heal was curing one launch at a time.
                _madded = _materialized_scan_register(ctx)
                if _madded:
                    ctx.log("  materialised-pointer scan: +%d function(s) whose address "
                            "the code builds with lis/addi and hands on as a pointer; "
                            "re-running codegen" % len(_madded))
                    ctx._cure_origin = getattr(ctx, "_cure_origin", {})
                    ctx._cure_origin["materialized"] = \
                        ctx._cure_origin.get("materialized", 0) + len(_madded)
                    do_codegen(ctx)
                    _mdef, _ = _heal._emitted_symbols(ctx.gen)
                    _mbad = [a for a in _madded if a not in _mdef]
                    if _mbad:
                        _mov = _heal.load_overrides_full(ctx.functions)
                        for a in _mbad:
                            _mov.pop(a, None)
                        _heal.write_overrides_full(ctx.functions, _mov)
                        ctx.log("  materialised-pointer scan: %d of them produced no "
                                "definition -> dropped, re-running codegen" % len(_mbad))
                        do_codegen(ctx)
                # Gap fill: whatever is left of the code range that carries
                # instructions and still has no C++ behind it. Runs after the
                # pointer scan so it only ever sees what that could not reach,
                # and loops because closing one gap exposes the next.
                for _round in range(0 if os.environ.get("REXAUTO_NO_GAPFILL") else 6):
                    _gaps = _gap_fill_register(ctx)
                    if not _gaps:
                        break
                    ctx.log("  gap fill: %d uncovered code range(s) registered; "
                            "re-running codegen" % len(_gaps))
                    ctx._cure_origin = getattr(ctx, "_cure_origin", {})
                    ctx._cure_origin["gap_fill"] =                         ctx._cure_origin.get("gap_fill", 0) + len(_gaps)
                    do_codegen(ctx)
                    _def2, _ = _heal._emitted_symbols(ctx.gen)
                    _drop = [a for a in _gaps if a not in _def2]
                    if _drop:
                        _ov2 = _heal.load_overrides_full(ctx.functions)
                        for a in _drop:
                            _ov2.pop(a, None)
                        _heal.write_overrides_full(ctx.functions, _ov2)
                        ctx.log("  gap fill: %d produced no definition -> dropped"
                                % len(_drop))
                        do_codegen(ctx)
                        break
            # Static pre-heal: every unresolved call/branch trap is a literal
            # REX_FATAL(...) codegen just wrote into generated/. Cure the whole set
            # here -- before the first build -- instead of paying one
            # build+launch+crash per trap in the run-heal, which also only ever
            # sees the first one the guest reaches. Re-codegen once and re-check;
            # curing a target can expose another, so loop until it stops shrinking.
            for _ in range(MAX_BUILD_ATTEMPTS):
                ub = _heal.unresolved_branches_from_generated(ctx.gen)
                if not ub:
                    break
                # called=True: cure an in-span target as a CHUNK, not a forced
                # landing. A landing is only enough when the branch comes from
                # inside the same routine -- it emits a `loc_` label, and a branch
                # arriving from a DIFFERENT function cannot goto into another
                # function's body, so the trap survives. Dante's Inferno
                # 0x829085A4 sat in forced_landings, with the file written and the
                # manifest including it, and codegen kept emitting
                # REX_FATAL("Unresolved branch from 0x829082B0 ...") anyway. A
                # chunk gives the target a real entry without splitting its owner.
                nr, ns = _heal.register_or_seed(ub, ctx.functions, ctx.forced,
                                                ctx.switches, called=True)
                if not (nr or ns):
                    ctx.log("  %d unresolved-branch trap(s) in generated/ that this heal "
                            "cannot cure (first: 0x%X)" % (len(ub), ub[0]))
                    break
                _heal.ensure_manifest_include(ctx.manifest, os.path.basename(ctx.forced))
                ctx.log("  static heal: %d unresolved-branch trap(s) in generated/ "
                        "-> +%d function(s), +%d landing(s); re-running codegen"
                        % (len(ub), nr, ns))
                ctx._cure_origin = getattr(ctx, "_cure_origin", {})
                ctx._cure_origin["static_trap"] =                     ctx._cure_origin.get("static_trap", 0) + nr + ns
                do_codegen(ctx)
        logp, rc = do_build(ctx, bat, attempt=attempt)
        txt = _heal._read_text(logp)
        if rc == 0 and os.path.exists(ctx.exe):
            write_game_root(ctx)
            ctx.log("build OK -> %s" % ctx.exe)
            cl = _closure.measure(ctx.gen)
            if cl:
                ctx.log("  " + _closure.summary_line(
                    cl, cures=len(_heal.load_overrides(ctx.functions))))
            # Where each cure came from. The gabarito always ships in production
            # -- it carries the ones no static pass can reach (Dante's Inferno:
            # 27 of 33 are addresses already covered by another function, only a
            # runtime call reveals them as separate entries) and it saves the
            # launches. But every change to the static passes is measured with
            # REXAUTO_NO_GABARITO=1, and `runtime` below is the number that has
            # to fall: it is exactly what the tool still cannot find by reading
            # the binary. Recorded per build so progress is comparable run over
            # run instead of remembered.
            _origin = dict(getattr(ctx, "_cure_origin", {}))
            _origin["total"] = len(_heal.load_overrides(ctx.functions))
            _origin["gabarito"] = bool(getattr(ctx, "_seeded_from_gabarito", False))
            return ctx.mark("build", {"exe": ctx.exe, "closure": cl,
                                      "cures": _origin})
        if "LLVM ERROR: out of memory" in txt:
            # Giant-module TUs (~2MB generated C++ each + a multi-MB PCH) at the
            # default parallelism (cores+2 = 18 concurrent clangs) can exceed
            # physical RAM -- fifadllzf hit this twice on 31GB (build died at
            # 214/215). The objs already built persist, so retrying the
            # INCREMENTAL build at reduced -j only recompiles the OOM'd tail.
            # Halve until 4; the bat keeps the reduced value for the rest of
            # this pipeline (heal-round rebuilds inherit the safe -j).
            oom_parallel = max(4, (oom_parallel or ctx.load_state().get("build_parallel") or 18) // 2)
            ctx.mark("build_parallel", oom_parallel)  # persistent lesson (write_build_bat reads it)
            bat = write_build_bat(ctx, parallel=oom_parallel)
            skip_codegen = True  # generated/ didn't change; only the build OOM'd
            ctx.log("  clang OUT OF MEMORY (too many concurrent frontends) -> "
                    "retrying incrementally with --parallel %d" % oom_parallel)
            continue
        if "has been modified since the precompiled header" in txt:
            # Installing a new SDK gives every header a new mtime. Ninja then
            # rebuilds the .pch, but the object files that consume it are not
            # ordered after it, so a whole build can compile against the stale
            # one and clang refuses every TU. Deleting the .pch files makes the
            # next round regenerate them first. Costs one full rebuild after an
            # SDK bump, and nothing at all otherwise.
            gone = 0
            for root, _dirs, files in os.walk(ctx.builddir):
                for f in files:
                    if f.endswith(".pch"):
                        try:
                            os.remove(os.path.join(root, f))
                            gone += 1
                        except OSError:
                            pass
            if gone:
                skip_codegen = True  # generated/ is current; only the PCH was stale
                ctx.log("  precompiled header older than the installed SDK headers -> "
                        "removed %d .pch and rebuilding" % gone)
                continue
        if "use of undeclared label" in txt:
            # Two undeclared-label classes: (a) a jump-table landing the SDK's heuristic
            # under-recovered -> force the SDK to recover it as an in-function block
            # (keeps the routine whole, e.g. Gears' decompressor loop); (b) a genuine
            # mid-flow function split -> extend the owning function's end. Apply both;
            # they partition the case space, so this converges either kind.
            nf = nb = 0
            for owner, olog in _build_log_by_owner(ctx, logp):
                landings = _heal.forced_landings_from_log(olog)
                # A landing we already forced that is STILL dangling did not take.
                # Retire it (and remember that) so the target goes back to being a
                # function and the goto becomes a call -- otherwise the loop
                # re-reads the same error every round and reports "not converging".
                _nf_state = owner.load_state().get("no_force") or []
                retired, _nf_new = _heal.retire_failed_landings(
                    owner.forced, landings, _nf_state)
                if retired:
                    owner.mark("no_force", sorted(_nf_new))
                    owner.log("  retired %d forced landing(s) that never took: %s"
                              % (len(retired), ", ".join("0x%08X" % a for a in retired)))
                landings = [a for a in landings if a not in _nf_new]
                onf = _heal.write_forced(owner.forced, landings) + len(retired)
                if onf:
                    _heal.ensure_manifest_include(owner.manifest, os.path.basename(owner.forced))
                nf += onf
                nb += _heal.heal_boundaries(olog, owner.gen, owner.functions, owner.forced)
            state = tuple(
                (tuple(sorted(_heal.load_forced(o.forced))),
                 tuple(sorted((a, e) for a, e in _heal.load_overrides(o.functions).items() if e)))
                for o in _heal_owners(ctx))
            if (nf + nb) == 0 or state == last_ends:
                # Tier 3: neither a forced landing nor a boundary extension took
                # (Scott Pilgrim's 0x82635368: the target sits past another
                # registered entry, so heal_boundaries refuses to reach, and the
                # forced landing was retired because the SDK emitted no label).
                # What is left is the classic mid-flow split: make the target a
                # function ENTRY. classifyTarget then sees a real function and
                # lowers the dangling `goto loc_T` to `sub_T(ctx, base); return;`
                # -- a tail call, which is exactly what a branch into another
                # routine's body is. Registered once per address; a target that
                # produces no definition is dropped by the next codegen's
                # emitted-symbols check like every other speculative cure.
                nreg = 0
                for owner, olog in _build_log_by_owner(ctx, logp):
                    dangling = _heal.forced_landings_from_log(olog)
                    have = _heal.load_overrides_full(owner.functions)
                    tried = set(owner.load_state().get("split_entries") or [])
                    new = [a for a in dangling if a not in have and a not in tried]
                    if new:
                        nreg += _heal.register_functions(new, owner.functions)
                        owner.mark("split_entries", sorted(tried | set(new)))
                        owner.log("  registered %d dangling branch target(s) as function "
                                  "entries (split -> tail call): %s"
                                  % (len(new), ", ".join("0x%08X" % a for a in new)))
                if nreg:
                    last_ends = None
                    continue
                ctx.log("  undeclared-label heal not converging (no new fix) -> see %s" % logp)
                break
            last_ends = state
            ctx.log("  jump-table landing heal -> +%d forced landing(s), +%d boundary fix(es); rebuilding"
                    % (nf, nb))
            continue
        imports = sorted(set(re.findall(r"undefined symbol:[^\n]*?_([A-Za-z]\w+)", txt)))
        if imports:
            ctx.log("  LINK ERROR: unresolved kernel import(s): %s" % ", ".join(imports[:12]))
            ctx.log("  these need runtime support — implement/enable them in the ReXGlue SDK "
                    "(e.g. uncomment the relevant src/kernel/*.cpp and rebuild the SDK).")
        else:
            ctx.log("  build failed (rc=%d) with no auto-fixable cause -> see %s" % (rc, logp))
        break
    raise SystemExit("build did not converge; see %s" % os.path.join(ctx.work, "_build.log"))


def _autoplay_thread(proc, stop_evt):
    """Press menu-advance keys (Enter=START, Space=A -- the MnK driver defaults)
    every few seconds so title/menu screens advance unattended, and heal windows
    exercise menu->deeper code instead of idling on PRESS START.
    IMPLEMENTATION MATTERS: the runtime window is SDL3, which maps keys by
    HARDWARE SCANCODE -- keybd_event(vk, scan=0) arrives as scancode 0 and SDL
    sees nothing (the first version of this was invisible to every game). Use
    SendInput with KEYEVENTF_SCANCODE (Enter=0x1C, Space=0x39) and force the
    game window to the foreground first (found by pid; SDL only receives key
    events with focus). Opt out with REXAUTO_NO_AUTOPLAY=1."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class INPUT(ctypes.Structure):
        class U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 40)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", U)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_SCANCODE = 0x0008
    KEYEVENTF_KEYUP = 0x0002

    def press_scan(scan):
        down = INPUT(type=INPUT_KEYBOARD)
        down.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE, 0, 0)
        up = INPUT(type=INPUT_KEYBOARD)
        up.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0)
        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.08)
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))

    def find_game_hwnd():
        target = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, lparam):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == proc.pid and user32.IsWindowVisible(hwnd):
                target.append(hwnd)
                return False
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return target[0] if target else None

    SC_ENTER, SC_SPACE = 0x1C, 0x39
    t0 = time.time()
    while not stop_evt.is_set() and proc.poll() is None:
        if time.time() - t0 > 15:  # boot/intro grace
            hwnd = find_game_hwnd()
            if hwnd:
                fg = user32.GetForegroundWindow()
                if fg != hwnd:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.3)
                if user32.GetForegroundWindow() == hwnd:
                    for scan in (SC_ENTER, SC_SPACE, SC_ENTER):
                        if stop_evt.is_set() or proc.poll() is not None:
                            break
                        press_scan(scan)
                        time.sleep(0.9)
        stop_evt.wait(2.5)


def run_once(ctx, seconds, discover=False):
    """Launch the game, let it run, kill it; return (newest-this-launch log text, alive).
    discover=True sets REX_HEAL_DISCOVER so the runtime logs+continues on each
    unregistered indirect target (surfacing many in one run) instead of aborting."""
    logdir = os.path.join(ctx.builddir, "logs")
    before = set(glob.glob(os.path.join(logdir, "*.log")))
    t0 = time.time()
    # window_s is what the launch was ASKED to survive; wall_s is what it actually
    # cost. They are not the same number and the state file only ever recorded the
    # first: run_once breaks out the moment the process exits, so a 360s window
    # spent on a title that dies at 12s has been billed at 360s in every estimate
    # ever made of this stage.
    op = ctx.t_op("launch", "launch", window_s=seconds, discover=bool(discover),
                  autoplay=not bool(os.environ.get("REXAUTO_NO_AUTOPLAY")))
    env = dict(os.environ)
    if discover:
        env["REX_HEAL_DISCOVER"] = "1"
    # The same GPU the player gets. v0.10.0 ships the GPU as a plugin and loads
    # none unless the cvar names one, so a launch without it renders nothing --
    # every Vd* call is "gpu_plugin not set; call ignored" -- and, worse for this
    # stage, runs a different program: no swap, no vblank, no render-thread work.
    # The play .cmd and launcher.ps1 both set it; the heal was the one launch
    # path that did not, so its verdicts were about a game nobody plays (and
    # Forza Horizon's heal windows were a black screen for 360s at a time).
    _gpu = gpu_plugins(ctx)
    if _gpu and not env.get("REX_GPU_PLUGIN"):
        env["REX_GPU_PLUGIN"] = _gpu[0]
    env.setdefault("REX_VSYNC", "true")
    env.setdefault("REX_D3D12_ALLOW_VARIABLE_REFRESH_RATE_AND_TEARING", "false")
    # Runtime-side autoplay: the MnK driver synthesizes periodic START/A presses
    # (REX_AUTOPLAY, SDK mnk_input_driver.cpp) so unattended windows advance
    # title/menu screens. Works without window focus -- OS-level key injection
    # (the first two attempts) was unreliable: SDL maps by scancode, background
    # processes can't steal foreground, and GetState zeroes input when unfocused.
    if not os.environ.get("REXAUTO_NO_AUTOPLAY"):
        env["REX_AUTOPLAY"] = "1"
    with op:
        try:
            p = subprocess.Popen([ctx.exe, "--game_data_root=%s" % ctx.game], cwd=ctx.builddir,
                                 env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True,
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError as ex:
            ctx.log("  could not launch the game: %s" % ex)
            op.set(launched=False, alive=False, produced_log=False)
            return "", False
        while time.time() - t0 < seconds:
            if p.poll() is not None:
                break
            time.sleep(0.5)
        alive = p.poll() is None
        if alive:
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
                try:
                    p.wait(timeout=5)
                except Exception:
                    pass
        new = [q for q in glob.glob(os.path.join(logdir, "*.log"))
               if q not in before or os.path.getmtime(q) >= t0]
        op.set(launched=True, alive=alive, produced_log=bool(new))
        if not new:
            ctx.log("  (this launch produced no log of its own)")
            return "", alive
        # The runtime rotates its log (NNN.2.log -> NNN.1.log -> NNN.log) once a
        # launch grows past the size cap, and a GPU-heavy title fills a part in
        # a minute or two: Forza Horizon's 360s confirm window left three parts
        # totalling 80,000 lines. Reading only the newest part meant the heal
        # judged a run by its last two minutes. Take every part this launch
        # wrote, oldest first.
        _txt = "".join(_heal._read_text(q) for q in sorted(new, key=os.path.getmtime))
        return _txt, alive


def _code_range(ctx):
    """([lo, hi), exact) of the entrypoint module's generated code, for filtering
    discovered targets down to plausible function addresses. exact=False means the
    wide fallback window -- fine for heal filtering, NOT precise enough to persist a
    verified-forever receipt on (in-image DATA addresses would pass as "in code").
    NOTE: ctx.gen already ends in generated/default -- an extra "default" segment
    here used to make the open() always fail, silently pinning every game to the
    fallback window (adversarial review catch)."""
    # v0.10.0 keeps these defines in <name>_pch.h, and a companion built as a
    # native [[modules]] entry names its files after the PROJECT, not the module
    # key -- so a fixed "<name>_init.h" pinned every module view to the fallback
    # window and the heal called every companion fatal uncurable. Take whichever
    # emitted header carries the ranges.
    r = _codegen_ranges(ctx)
    if r:
        _ib, b, sz = r
        return b, b + sz, True
    return 0x82000000, 0x84000000, False


def _prev_list_function(ctx, addr):
    """Largest functions-list start strictly below addr (None if unavailable or
    addr itself is a list entry). Used to find the neighbour whose emitted body
    absorbed a gap containing addr."""
    import bisect
    path = os.path.join(ctx.work, "%s_functions_list.txt" % ctx.name)
    if not os.path.exists(path):
        return None
    try:
        starts = sorted({int(l, 16) for l in open(path) if l.strip()})
    except ValueError:
        return None
    i = bisect.bisect_left(starts, addr)
    if i < len(starts) and starts[i] == addr:
        return None  # addr is a known start; overlap is not the story here
    return starts[i - 1] if i > 0 else None


def _runheal_fingerprint(ctx):
    """What a convergence verdict is actually a property of: the exact game exe, the
    runtime DLL it loads, AND the guest image it executes (xex + staged title-update
    + which game root) -- the runtime re-reads those at every launch, so behavior can
    change while exe+dll stay identical (adversarial review catch). A cure-toml/SDK/
    codegen change flows into the exe hash; a re-rip/TU/game-swap flows into these."""
    dll = os.path.join(ctx.builddir, "rexruntime.dll")
    try:
        return {"exe": _sha256(ctx.exe),
                "runtime": _sha256(dll) if os.path.exists(dll) else "",
                "image": _sha256(ctx.xex) if ctx.xex and os.path.exists(ctx.xex) else "",
                "tu": _sha256(ctx.tu_xexp) if ctx.tu_xexp and os.path.exists(ctx.tu_xexp) else "",
                "game": ctx.game or ""}
    except Exception:
        return None


def _xex_image_range(path):
    """(load_address, image_size) from a XEX2 container on disk, or None.
    The security-info offset sits at +0x10 of the header; that block carries the
    image size at +0x04 and the load address at +0x110 -- the same two numbers
    the 0.8.2 runtime printed as "XEX image loaded at LO-HI"."""
    import struct
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x18)
            if len(head) < 0x18 or head[:4] != b"XEX2":
                return None
            sec = struct.unpack(">I", head[0x10:0x14])[0]
            fh.seek(sec)
            si = fh.read(0x114)
        if len(si) < 0x114:
            return None
        size = struct.unpack(">I", si[4:8])[0]
        load = struct.unpack(">I", si[0x110:0x114])[0]
    except (OSError, struct.error):
        return None
    return (load, size) if load and size else None


def _guest_images(game, entry_xex=None):
    """[(rel, lo, hi)] for every XEX2 container under the game root except the
    entrypoint -- what a companion looks like on disk. rel uses the guest's
    backslashes, like the log-derived entries it sits beside."""
    out = []
    entry = os.path.normcase(os.path.abspath(entry_xex)) if entry_xex else None
    for root, _dirs, files in os.walk(game or ""):
        for fn in files:
            if not fn.lower().endswith((".xex", ".dll", ".exe")):
                continue
            p = os.path.join(root, fn)
            if entry and os.path.normcase(os.path.abspath(p)) == entry:
                continue
            rng = _xex_image_range(p)
            if rng:
                out.append((os.path.relpath(p, game).replace("/", "\\"), rng[0], rng[0] + rng[1]))
    return out


def _autodetect_companions(ctx, log_text, targets):
    """Zero-touch multi-XEX: when a production run fatals on addresses OUTSIDE
    every recompiled module, find which guest-loaded companion XEX contains them.
    The runtime log records, at load time, a '<file>.dllp / .xexp' patch probe
    immediately before each 'XEX image loaded at LO-HI' line -- pairing the two
    yields (module path, image range). A fatal target inside a companion's range
    + the file on disk being XEX2 => author it into port/<name>_modules.toml,
    where stage_build's setup_extra_modules recompiles it through the full IDA
    pipeline (v2.18). Returns the newly-authored module dicts; [] when nothing
    new (already-declared companions are never re-authored -> the caller's
    anti-loop: a companion that still fatals AFTER recompilation falls through
    to the honest production_fatal verdict)."""
    loads, last_probe = [], None
    for ln in log_text.splitlines():
        m = re.search(r"entry not found for '([^']+\.(?:xex|dll|exe))p'", ln, re.I)
        if m:
            last_probe = m.group(1)
            continue
        m = re.search(r"XEX image loaded at ([0-9A-Fa-f]{8})-([0-9A-Fa-f]{8})", ln)
        if m:
            if last_probe:
                rel = re.sub(r"^\\Device\\[^\\]+\\[^\\]+\\", "", last_probe)
                loads.append((rel, int(m.group(1), 16), int(m.group(2), 16)))
            last_probe = None
    # The v0.10.0 runtime logs neither line above, so read the same two numbers
    # off every XEX2 container under the game root instead: a companion's image
    # range is in its own header, and the disk is there on every SDK.
    seen_rel = {r.lower() for r, _, _ in loads}
    for rel, mlo, mhi in _guest_images(ctx.game, ctx.xex):
        if rel.lower() not in seen_rel:
            loads.append((rel, mlo, mhi))
    existing_mods = extra_modules(ctx)
    existing_paths = {os.path.normcase(m["xex"]) for m in existing_mods}
    existing_keys = {m["key"] for m in existing_mods} | {ctx.name}
    newmods, seen = [], set()
    for a in targets:
        for rel, mlo, mhi in loads:
            if not (mlo <= a < mhi) or rel.lower() == "default.xex" or rel in seen:
                continue
            path = os.path.join(ctx.game, rel.replace("\\", os.sep))
            if os.path.normcase(path) in existing_paths:
                continue
            try:
                if open(path, "rb").read(4) != b"XEX2":
                    continue
            except OSError:
                continue
            key = re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(rel))[0].lower()) or "mod"
            if key[0].isdigit():
                key = "m" + key
            while key in existing_keys:
                key += "x"
            existing_keys.add(key)
            seen.add(rel)
            newmods.append({"key": key, "rel": rel.replace("\\", "/"),
                            "lo": mlo, "hi": mhi})
    if not newmods:
        return []
    cfgp = os.path.join(ctx.port, "%s_modules.toml" % ctx.name)
    body = open(cfgp, encoding="utf-8", errors="ignore").read() if os.path.exists(cfgp) else (
        "# Extra recompilable guest modules beyond the entrypoint -- AUTO-DETECTED by\n"
        "# rexauto run-heal: a production run fataled on calls landing inside these\n"
        "# guest-loaded companion XEX images (probe + 'XEX image loaded' log pairs).\n")
    for m in newmods:
        body += ('\n[[modules]]\nkey = "%s"\nname = "%s"\nxex = "%s"\n'
                 'symbol_prefix = "%s_"\n' % (m["key"], m["key"], m["rel"], m["key"]))
        ctx.log("  companion XEX auto-detected: %s @ 0x%X-0x%X (fatal target inside) "
                "-> authored into %s" % (m["rel"], m["lo"], m["hi"], os.path.basename(cfgp)))
    open(cfgp, "w", encoding="utf-8").write(body)
    return newmods


def stage_runheal(ctx):
    bat = write_build_bat(ctx)
    lo, hi, range_exact = _code_range(ctx)
    # Multi-XEX: know each extra module's code range so an invalid-function target
    # inside a companion (e.g. Spider-Man's GameLogic.dll at 0x88080000) is healed
    # in THAT module's functions.toml + a module re-codegen, instead of being
    # written off as "uncurable/out-of-image". Empty for single-module titles ->
    # behavior byte-identical to before.
    mod_heal = []
    for m in extra_modules(ctx):
        mc = _module_view(ctx, m)
        mlo, mhi, mexact = _code_range(mc)
        if mexact:
            mod_heal.append((mc, mlo, mhi))

    def _partition(logged):
        """(main_addrs, [(module_view, addrs)], uncurable) by owning code range."""
        main = [a for a in logged if lo <= a < hi and (a & 3) == 0]
        seen = set(main)
        hits = []
        for mc, mlo, mhi in mod_heal:
            ma = [a for a in logged if a not in seen and mlo <= a < mhi and (a & 3) == 0]
            if ma:
                seen.update(ma)
                hits.append((mc, ma))
        return main, hits, [a for a in logged if a not in seen]
    if not os.path.exists(ctx.exe):
        # A failure must FAIL (SystemExit, like stage_build) -- a truthy mark would
        # make the next plain pipeline run print "skip runheal (done)" and never
        # re-attempt verification (adversarial review catch).
        raise SystemExit("[rexauto] runheal: no exe at %s -- run the build stage first" % ctx.exe)
    rcpt_path = os.path.join(ctx.port, "%s_runheal_receipt.json" % ctx.name)
    # Confirm/discover window floors at 360s. The short heal ROUNDS stay fast
    # (ctx.args.run_seconds, ~22s), but the initial discover pass and the final
    # convergence check run this long so late-loading indirect targets are caught
    # up front instead of surfacing as a crash mid-gameplay. Gears of War Judgment
    # loads sub_824CA490 only ~71s in (past the old 47s window) -> it converged
    # "clean" then FATAL'd in play; a wide window heals it in the same pass. Some
    # titles (565507E4 Crash of the Titans) have a long green-thread-paced loading
    # phase (~2min) before the first gameplay indirect calls surface, so the floor
    # is 360s to reach past loading into actual play.
    confirm_seconds = max(ctx.args.run_seconds * 2, ctx.args.run_seconds + 25, 360)
    # --- Tier 0: convergence receipt = ZERO launches --------------------------
    # A "converged" verdict is a property of the binaries + guest image that ran.
    # Persist it keyed by their hashes: when the same set comes around again (a
    # pipeline re-run, --from build with no change, a GUI reopen) there is nothing
    # new to learn from launching the game, so don't. Honored only if it was
    # verified with a window at least as long as the one requested now. Delete the
    # receipt (or set REXAUTO_FORCE_RUNHEAL=1) to force a live check.
    fp = _runheal_fingerprint(ctx)
    try:
        rcpt = json.load(open(rcpt_path)) if os.path.exists(rcpt_path) else None
    except Exception:
        rcpt = None
    if os.environ.get("REXAUTO_FORCE_RUNHEAL"):
        rcpt = None
    if fp and rcpt and rcpt.get("fingerprint") == fp \
            and rcpt.get("seconds", 0) >= confirm_seconds:
        ctx.log("runheal: receipt matches the current exe+runtime+image -> already "
                "verified (%s); not launching the game (delete %s to re-verify)"
                % (rcpt.get("verdict", "converged"), os.path.basename(rcpt_path)))
        if getattr(ctx.args, "publish_gabarito", False):
            publish_gabarito(ctx)  # cures are on disk; publishing needs no launch
        # The receipt path is THE warm case of this stage: it is the difference
        # between zero launches and two 360s ones. A runheal duration that does not
        # say which of those it was is not a measurement of anything.
        ctx.t_note(receipt="hit", launches=0)
        return ctx.mark("runheal", {"receipt": True, "verdict": rcpt.get("verdict")})
    # --- Tier 1: minimal launches decide ---------------------------------------
    # Discover mode (REX_HEAL_DISCOVER): the runtime logs+continues on each
    # unregistered indirect target instead of aborting, so one run surfaces MANY
    # missing functions at once. The key property: if a discover run logs ZERO
    # targets AT ALL, no call was ever no-op'd, so the execution was identical to
    # a clean run -- that run doubles as the convergence confirmation (zero
    # *logged*, not zero *in-range*: an out-of-image/misaligned no-op'd call means
    # a production run FATALs there, so it must never mint a "survived" verdict --
    # adversarial review catch). This collapses the old discover(22s)xN ->
    # fatal(22s) -> confirm(47s) dance: a cured re-port launches ONCE, and the
    # receipt makes the next pipeline run launch ZERO times. Guards that stay:
    #  * long window on the deciding run (rayman crashed at 0x82162208 ~1s past a
    #    22s window after "converging" on the short one); heal rounds in between
    #    keep the short window for fast bulk iteration.
    #  * the deciding run must not be the port's first-ever launch: first boot
    #    creates saves/caches, and load-existing-state code paths (the v2.6.0
    #    xam_content crash class) only execute on the SECOND boot. A clean
    #    first-ever launch primes state; the next clean run decides.
    #  * a launch that produced no log is no evidence -- never converge on it.
    # "primed" = the CURRENT guest state (image+TU+game root) has been booted at
    # least once, so saves/caches exist and second-boot code paths are reachable.
    # Keyed to the guest fingerprint, not bare log existence: stale logs from a
    # previous game root must not skip the priming run (adversarial review catch).
    primed_path = os.path.join(ctx.port, "%s_runheal_primed.json" % ctx.name)
    guest_fp = {k: fp[k] for k in ("image", "tu", "game")} if fp else None
    try:
        primed = guest_fp is not None and json.load(open(primed_path)) == guest_fp
    except Exception:
        primed = False
    window = confirm_seconds
    # Recorded before the first launch, because both facts change during the loop:
    # `primed` decides whether the deciding run costs one extra launch, and the
    # receipt miss is what this stage's whole cost hangs off.
    ctx.t_note(receipt="miss", primed_at_entry=bool(primed), confirm_seconds=confirm_seconds)
    resynced = set()  # addresses we've already forced a clean relink for (anti-loop)
    shrunk = set()    # containing functions we've already end-shrunk (anti-loop)
    # Discover mode is what makes this loop cheap -- one launch surfaces many
    # targets instead of aborting at the first -- but it does not run the same
    # program. A no-op'd call returns without doing its work, so every branch
    # after it can differ. Forza Horizon reaches 0x8249C648 only when the calls
    # before it are real: four clean discover launches declared "converged" for
    # an exe that died 2s into every production run. So the last launch before a
    # convergence verdict is always a production one, and any cure drops back to
    # discover mode (which then has to earn the production confirm again).
    next_is_production = False
    for it in range(1, ctx.args.heal_iters + 1):
        primed_at_launch = primed
        was_production = next_is_production
        next_is_production = False
        t_launch = time.time()
        txt, alive = run_once(ctx, window, discover=not was_production)
        primed = True
        if guest_fp and not primed_at_launch:
            try:
                json.dump(guest_fp, open(primed_path, "w"), indent=1)
            except Exception:
                pass
        if not txt:
            # No log = no evidence either way. FAIL (not mark): a truthy mark would
            # make the next plain run skip the stage as "done" forever.
            raise SystemExit("[rexauto] runheal: launch produced no log -- fix the "
                             "launch environment and re-run")
        # Range-filter: a logged target can be OUTSIDE this module's recompiled
        # code range (e.g. a call into a companion XEX a multi-XEX title loads at
        # 0x88000000+). Registering such an out-of-image address as a {} function
        # corrupts the port -- it killed sonic_adventure's boot (a stray
        # "0x88610000" = {}). Only heal targets that live in this image; report
        # (never "cure") the rest.
        logged = _heal.invalid_functions_ordered(txt)
        log_text = txt  # freshest runtime log (for companion auto-detection)
        # Codegen-baked "Unresolved call from X to Y" fatals: the branch target is
        # neither a discovered function nor a recovered landing, so the generated
        # code traps unconditionally -- launching again can never cure it. Force
        # the target as an in-function landing in the OWNING module (never a {}
        # split: the forced-landings lesson) and rebuild. crash_mind_over_mutant
        # sat through 4 identical runs on this class; Forza Horizon hit it at
        # 0x830ED910 mid-boot.
        ub = _heal.unresolved_branches_from_runtime(txt)
        if ub:
            forced_new = 0
            for owner, olo, ohi in [(ctx, lo, hi)] + [(mc, mlo, mhi) for mc, mlo, mhi in mod_heal]:
                mine = [a for a in ub if olo <= a < ohi]
                if not mine:
                    continue
                # register_or_seed routes each target correctly: a landing INSIDE
                # an existing function -> forced_landings (keeps the routine
                # whole); a target in an override GAP -> a {} FunctionNode so
                # graph().getFunction() is non-null and build_b lowers the branch
                # to a real tail call. A forced-landing alone never creates the
                # node, so gap targets (crash_mind_over_mutant 0x82476040) stayed
                # unresolved and re-fataled every run.
                nr, ns = _heal.register_or_seed(mine, owner.functions, owner.forced, owner.switches)
                if ns:
                    _heal.ensure_manifest_include(owner.manifest, os.path.basename(owner.forced))
                if nr + ns:
                    owner.log("  %d unresolved-branch target(s) cured (%d fn, %d landing): %s; rebuilding"
                              % (nr + ns, nr, ns, ", ".join("0x%X" % a for a in mine)))
                    do_codegen(owner)
                    forced_new += nr + ns
            if forced_new:
                do_codegen(ctx)  # no-op for main-only fixes; restores rexglue.cmake after module codegen
                logp, rc = do_build(ctx, bat)
                if rc != 0 or not os.path.exists(ctx.exe):
                    raise SystemExit("[rexauto] runheal: rebuild failed after forcing %d "
                                     "unresolved-branch landing(s) -> see %s" % (forced_new, logp))
                window = ctx.args.run_seconds
                continue
        addrs, mod_hits, uncurable = _partition(logged)
        if (addrs or mod_hits) and uncurable:
            # Corrupted-continuation guard: after the first no-op'd uncurable call
            # the run executes with corrupt state, so in-range targets logged in
            # the SAME run may be garbage that register_or_seed would enshrine.
            # One fatal-mode run gives ground truth (it aborts at the first
            # invalid target, so everything it logs precedes any corruption).
            # SAME window as the discover run (a shorter one would miss targets
            # first reached late and mint a false "uncurable" verdict); BOTH lists
            # are recomputed from the ground-truth log; a clean fatal run where
            # discover saw targets is timing nondeterminism -> inconclusive,
            # re-observe instead of deciding (adversarial review catches).
            ctx.log("  %d uncurable no-op'd target(s) alongside %d curable -> "
                    "re-reading ground truth with one fatal-mode run"
                    % (len(uncurable), len(addrs)))
            txt2, _ = run_once(ctx, window)
            if not txt2:
                raise SystemExit("[rexauto] runheal: ground-truth launch produced no "
                                 "log -- fix the launch environment and re-run")
            logged2 = _heal.invalid_functions_ordered(txt2)
            if not logged2:
                ctx.log("  fatal-mode run logged nothing (timing nondeterminism); re-observing")
                continue
            log_text = txt2
            addrs, mod_hits, uncurable = _partition(logged2)
        if not addrs and not mod_hits:
            if uncurable:
                # Zero-touch multi-XEX: the fatal may be a call into a companion
                # XEX the guest loaded but we never recompiled. Detect it from
                # this run's own log (probe + "XEX image loaded" pairs), author
                # it into <name>_modules.toml, rebuild (stage_build runs the new
                # module through the full IDA pipeline), and keep healing. Only
                # modules NOT already declared are authored, so a companion that
                # STILL fatals after recompilation falls through to the honest
                # verdict below instead of looping.
                newmods = _autodetect_companions(ctx, log_text, uncurable)
                if newmods:
                    ctx.log("  %d companion XEX(s) auto-detected -> rebuilding with "
                            "them recompiled" % len(newmods))
                    # phase="runheal": this is stage_build re-entered from inside
                    # the heal loop. It gets its own frame in the sidecar and is
                    # deliberately kept OUT of the .rexauto_state roll-up, because
                    # publishing a heal-round relink under the key "build" would be
                    # a number that contradicts its own name.
                    with ctx.timer("build", phase="runheal"):
                        stage_build(ctx)
                    known = {mc.name for mc, _, _ in mod_heal}
                    for m in extra_modules(ctx):
                        mc = _module_view(ctx, m)
                        mlo, mhi, mexact = _code_range(mc)
                        if mexact and mc.name not in known:
                            mod_heal.append((mc, mlo, mhi))
                    continue
                # Honest non-convergence: discover mode no-op'd calls that a
                # production run FATALs on; nothing in THIS module cures them.
                verdict = ("recompilation of this module found no curable targets, but "
                           "%d uncurable target(s) were no-op'd (out-of-image/misaligned,"
                           " e.g. 0x%X) -- a production run FATALs there (companion XEX?)"
                           % (len(uncurable), uncurable[0]))
                ctx.log("run-heal: %s" % verdict)
                if getattr(ctx.args, "publish_gabarito", False):
                    publish_gabarito(ctx)
                # "alive" records what was OBSERVED (discover mode no-ops the calls,
                # so the game may well be alive); the prediction lives in its own key.
                return ctx.mark("runheal", {"iters": it, "alive": alive,
                                            "production_fatal": True,
                                            "uncurable": ["0x%X" % a for a in uncurable[:8]]})
            if window != confirm_seconds:
                ctx.log("  clean at %ds; stretching to the %ds confirm window"
                        % (window, confirm_seconds))
                window = confirm_seconds
                continue
            if not primed_at_launch:
                ctx.log("  clean first-ever launch primed saves/caches; re-running once "
                        "against existing state (second-boot code paths)")
                continue
            if not was_production:
                next_is_production = True
                ctx.log("  clean in discover mode; confirming once in production mode "
                        "(a no-op'd call changes which code runs after it)")
                continue
            crash = None if alive else _crash_since(ctx, t_launch)
            if crash:
                # Nothing here to cure: a guest access violation is a runtime or
                # codegen fault, not a missing function. Say so instead of
                # "converged (other stop)", and mint no receipt.
                ctx.log("run-heal: the production run CRASHED after %d launch(es): %s -- not "
                        "converged; see rexglue-crash.txt beside the exe" % (it, crash))
                return ctx.mark("runheal", {"iters": it, "alive": False, "crash": crash})
            verdict = ("survived %ds with no invalid-function fatal" % confirm_seconds) if alive \
                else "exited without an invalid-function fatal (other stop - likely GPU/runtime)"
            ctx.log("run-heal converged in %d launch(es): %s" % (it, verdict))
            if getattr(ctx.args, "publish_gabarito", False):
                publish_gabarito(ctx)
            # The receipt is only minted on POSITIVE evidence: the game was still
            # alive at window end (an early "other stop" exit may be transient --
            # driver, GPU wall -- and is cheap to re-verify precisely because it
            # exits early) and the real code range was known (the fallback window
            # would let in-image DATA addresses masquerade as verified code).
            if alive and range_exact:
                fp = _runheal_fingerprint(ctx)  # recompute: heal rounds relinked the exe
                if fp:
                    json.dump({"fingerprint": fp, "verdict": verdict,
                               "seconds": confirm_seconds, "launches": it},
                              open(rcpt_path, "w"), indent=1)
            return ctx.mark("runheal", {"iters": it, "alive": alive,
                                        "confirmed_seconds": confirm_seconds})
        n = 0
        if addrs:
            n_reg, n_seed = _heal.register_or_seed(addrs, ctx.functions, ctx.forced, ctx.switches, called=True)
            if n_seed:
                _heal.ensure_manifest_include(ctx.manifest, os.path.basename(ctx.forced))
            n = n_reg + n_seed
            # the number the static passes have to drive down: cures that only a
            # launch could reveal (see the `cures` block in stage_build)
            ctx._cure_origin = getattr(ctx, "_cure_origin", {})
            ctx._cure_origin["runtime"] = ctx._cure_origin.get("runtime", 0) + n
            ctx.log("heal round %d: target(s) @ %s -> +%d (%d fn, %d landing); rebuilding"
                    % (it, ",".join("0x%X" % a for a in addrs), n, n_reg, n_seed))
        # Targets owned by an extra module: cure in ITS functions.toml and re-codegen
        # that module (its objects relink into the same exe in the shared rebuild below).
        for mc, ma in mod_hits:
            mr, ms = _heal.register_or_seed(ma, mc.functions, mc.forced, mc.switches, called=True)
            if ms:
                _heal.ensure_manifest_include(mc.manifest, os.path.basename(mc.forced))
            n += mr + ms
            mc.log("heal round %d: target(s) @ %s -> +%d (%d fn, %d landing); re-codegen module"
                   % (it, ",".join("0x%X" % a for a in ma), mr + ms, mr, ms))
            do_codegen(mc)
        window = ctx.args.run_seconds  # short fast rounds while targets keep coming;
        # the final clean round stretches back to confirm_seconds before converging.
        if n == 0:
            # register_or_seed added nothing -> addrs[0] is ALREADY registered in the
            # current sources. But the *running exe* can lag the codegen: an earlier
            # codegen (deep-extract gate churn, or a prior no-op heal) leaves
            # register.cpp newer than the linked exe, so the built exe's dispatch tables
            # never got SetFunction(addr) -> a SPURIOUS "unregistered" fatal on a
            # function that source-registers fine. This exact case made dbz look like an
            # unfixable runtime wall at 0x82415F90 when a plain relink converged it.
            # Force one codegen+relink to resync the exe, then re-run. Only if the same
            # address STILL fatals after a clean relink is it a genuine wall.
            a0 = (addrs + [a for _, ma in mod_hits for a in ma])[0]
            if a0 not in resynced:
                resynced.add(a0)
                ctx.log("  0x%X already registered but still flagged -> resync exe "
                        "(codegen may be newer than the linked exe) and retry" % a0)
                do_codegen(ctx)
                logp, rc = do_build(ctx, bat)
                if rc == 0 and os.path.exists(ctx.exe):
                    continue  # next iteration re-runs against the resynced exe
                ctx.log("  resync rebuild failed -> %s" % logp)
            # Boundary overlap: the address IS registered but codegen ignores the
            # override because a NEIGHBOUR's emitted body extends across it (the
            # scanner absorbed a functions-list gap). Seen as vtable-thunk tables:
            # Captain America 0x822A2040 is a 16-byte virtual-call thunk absorbed
            # into 0x822A2010's body. The runtime just indirect-called the address,
            # so it IS a true entry point -> shrink the containing function with an
            # end-override at the target and re-codegen. Fires only on this exact
            # class (registered + survives resync + a prior list entry spans it).
            # Owner-aware: a module-range a0 shrinks in THAT module's functions.toml
            # (Halo 3 waveslib 0x8A061018 was this class); its funclist is refreshed
            # first -- module funclists are written PRE-emit (0 functions), so the
            # neighbour bisect needs a post-emit regeneration.
            owner = ctx if lo <= a0 < hi else None
            if owner is None:
                for omc, omlo, omhi in mod_heal:
                    if omlo <= a0 < omhi:
                        owner = omc
                        if ctx.env.get("python") and ctx.env.get("jt_repo"):
                            run([ctx.env["python"],
                                 os.path.join(ctx.env["jt_repo"], "src", "extract_funcs.py"),
                                 omc.gen, "-o",
                                 os.path.join(omc.work, "%s_functions_list.txt" % omc.name)])
                        break
            prev = _prev_list_function(owner, a0) if owner is not None else None
            if prev is not None and prev not in shrunk:
                shrunk.add(prev)
                ov = _heal.load_overrides_full(owner.functions)
                cur = ov.get(prev) or {}
                if cur.get("end") is None or cur["end"] > a0:
                    cur["end"] = a0
                    ov[prev] = cur
                    _heal.write_overrides_full(owner.functions, ov)
                    owner.log("  0x%X lies inside 0x%X's emitted body -> shrink it with "
                              "end=0x%X and retry (absorbed-gap/vtable-thunk class)" % (a0, prev, a0))
                    do_codegen(owner)
                    if owner is not ctx:
                        do_codegen(ctx)  # restore generated/rexglue.cmake to the entrypoint
                    logp, rc = do_build(ctx, bat)
                    if rc == 0 and os.path.exists(ctx.exe):
                        continue
                    ctx.log("  shrink rebuild failed -> %s" % logp)
            ctx.log("  stuck on 0x%X (already registered, survives resync) — needs a closer look" % a0)
            return ctx.mark("runheal", {"stuck": "0x%X" % a0})
        do_codegen(ctx)
        logp, rc = do_build(ctx, bat)
        # Label-heal to convergence (rc re-checked each pass -- a 2-deep cascade used
        # to dead-end silently) + ONE plain retry for transient failures (e.g. the
        # relink racing the just-killed game process still holding the exe); a second
        # consecutive non-label failure is a real break -- stop burning rebuilds.
        plain_fails = 0
        for _pass in range(4):
            if rc == 0 and os.path.exists(ctx.exe):
                break
            _txt = _heal._read_text(logp)
            if "LLVM ERROR: out of memory" in _txt:
                # Same auto-fix as stage_build: halve -j, persist the lesson,
                # retry incrementally (objs persist; generated/ unchanged).
                plain_fails = 0
                _oomj = max(4, (ctx.load_state().get("build_parallel") or 18) // 2)
                ctx.mark("build_parallel", _oomj)
                bat = write_build_bat(ctx, parallel=_oomj)
                ctx.log("  clang OUT OF MEMORY in heal rebuild -> retrying with --parallel %d" % _oomj)
            elif "use of undeclared label" in _txt:
                plain_fails = 0
                for owner, olog in _build_log_by_owner(ctx, logp):
                    dangling = _heal.forced_landings_from_log(olog)
                    fixed = 0
                    if _heal.write_forced(owner.forced, dangling):
                        _heal.ensure_manifest_include(owner.manifest,
                                                      os.path.basename(owner.forced))
                        fixed += 1
                    fixed += _heal.heal_boundaries(olog, owner.gen, owner.functions, owner.forced)
                    if not fixed:
                        # same tier-3 fallback as stage_build: split -> tail call
                        have = _heal.load_overrides_full(owner.functions)
                        new = [a for a in dangling if a not in have]
                        if new and _heal.register_functions(new, owner.functions):
                            owner.log("  registered %d dangling branch target(s) as "
                                      "function entries (split -> tail call)" % len(new))
                do_codegen(ctx)
            else:
                plain_fails += 1
                if plain_fails >= 2:
                    break
            logp, rc = do_build(ctx, bat)
        if rc != 0 or not os.path.exists(ctx.exe):
            raise SystemExit("[rexauto] runheal: rebuild failed after registering %d "
                             "target(s) -> see %s" % (len(addrs), logp))
    ctx.log("run-heal hit max iterations (%d)" % ctx.args.heal_iters)
    if getattr(ctx.args, "publish_gabarito", False):
        publish_gabarito(ctx)
    ctx.mark("runheal", {"iters": ctx.args.heal_iters})


def stage_run(ctx):
    ctx.log("launching %s" % ctx.exe)
    # Detach the game from the pipeline's stdio. If it inherits the GUI Hub's stdout
    # pipe, the Hub's reader blocks until the GAME exits -> the 'done' event never
    # fires -> the GUI stays 'Recompiling' and you cannot start another game.
    subprocess.Popen([ctx.exe, "--game_data_root=%s" % ctx.game], cwd=ctx.builddir,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    ctx.log("running. a game window should open. (GPU/playability is per-title and not "
            "auto-solved by rexauto.)")


# --------------------------------------------------------------------------- main
# --- SDK version floor -------------------------------------------------------
# Separate from SDK_PIN. The pin says "this is the exact SDK this rexauto was
# tested with". The floor says something stronger: rexauto now *requires*
# v0.10.0 and cannot produce a correct port below it --
#
#   * [[image_patch]] lives in the manifest; an older rexglue ignores the block,
#     so every community game patch silently vanishes from the build,
#   * the GPU moved out into rexgpu-*.dll, and the generated launcher names a
#     plugin that older runtimes know nothing about,
#   * the codegen ranges moved from <name>_init.h to <name>_pch.h.
#
# None of those fail loudly on an old SDK -- they produce a port that builds and
# is quietly wrong, which is the worst outcome and exactly what a skippable check
# would let through.
SDK_MIN_VERSION = (0, 10, 0)
_sdk_floor_checked = False


def _rexglue_version(path, tries=4):
    """(major, minor, patch) reported by `rexglue --version`, or None.

    Retries, because the binary is not reliable here: the 0.8.2 build returns an
    EMPTY stdout with exit code 0 roughly one run in three. Asking once made this
    check pass an old SDK at random, which is worse than not having it.

    Reads "0.10.0" on the current SDK and "0.8.2.171-dev.g79f589e" on the old one,
    so only the first three numbers matter.
    """
    for _ in range(tries):
        try:
            r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", (r.stdout or "") + (r.stderr or ""))
        if m:
            return tuple(int(g) for g in m.groups())
    return None


def verify_sdk_floor(env):
    """Refuse an SDK older than SDK_MIN_VERSION.

    Fails CLOSED when the version cannot be read. A check that shrugs when it
    cannot tell is not a check -- and this binary really does answer with nothing
    sometimes. REXAUTO_ALLOW_UNVERIFIED_SDK=1 is the deliberate way past that one
    case; it does not let an SDK through that reported a version below the floor.
    """
    global _sdk_floor_checked
    if _sdk_floor_checked:
        return
    _sdk_floor_checked = True
    path = env.get("rexglue")
    if not path or not os.path.exists(path):
        return
    want = SDK_MIN_VERSION
    want_s = ".".join(map(str, want))
    got = _rexglue_version(path)

    if got is None:
        if os.environ.get("REXAUTO_ALLOW_UNVERIFIED_SDK"):
            print("[rexauto] WARNING: no version came back from %s after several tries; "
                  "continuing because REXAUTO_ALLOW_UNVERIFIED_SDK is set." % path)
            return
        raise SystemExit(
            "[rexauto] SDK VERSION UNREADABLE - refusing to run.\n"
            "  %s answered nothing to --version after several tries.\n"
            "  rexauto requires rexglue %s or newer and will not guess.\n"
            "  If you are sure this SDK is new enough, set REXAUTO_ALLOW_UNVERIFIED_SDK=1.\n"
            % (path, want_s))

    if got < want:
        raise SystemExit(
            "[rexauto] SDK TOO OLD - refusing to run.\n"
            "  found   rexglue %s\n"
            "  require rexglue %s or newer\n"
            "    at %s\n"
            "  rexauto needs v%s: [[image_patch]] (community game patches), the GPU\n"
            "  plugin split, and the codegen ranges that moved to <name>_pch.h. An older\n"
            "  SDK does not fail on these -- it builds a port that is quietly wrong.\n"
            "  Install the rexglue-sdk bundled with this rexauto release (Setup in the\n"
            "  GUI, or extract it next to rexauto).\n"
            % (".".join(map(str, got)), want_s, path, want_s))


# --- SDK compatibility pin --------------------------------------------------
# rexauto generates code with a specific rexglue codegen tool and links it
# against a specific runtime. Mixing a DIFFERENT SDK build can silently produce
# broken or crashing exes — the v1.3 fork migration changed the scaffolding and
# the runtime ABI, exactly the kind of mismatch this guards against. rexauto
# refuses to run against an SDK whose binaries don't match the ones it was built
# and tested with. No override: every port rexauto has ever shown correct was
# built on these exact binaries. Bump these when the bundled SDK is updated.
#
# Where the binaries come from: branch `rexauto` of github.com/xdzleo/rexglue-sdk
# -- upstream main plus every fix below as its own branch (each one an open
# upstream PR), merged. Its tree is byte-identical to the source these were
# built from, so anyone can rebuild the bundled SDK from that branch.
# The release this source belongs to. The GUI's Setup fetches the SDK of THIS
# tag, never "latest": a newer release's SDK would fail this build's pin.
REXAUTO_VERSION = "2.36.6"

SDK_PIN = {
    # v2.35: ReXGlue v0.10.0 becomes the default SDK, built from source with four
    # fixes on top -- all four are open upstream, so this pin is a fork only until
    # they land:
    #
    #   * xam_content: XamContentCreate captured a std::string_view over GUEST
    #     memory into the lambda it hands to CompleteOverlappedDeferredEx. By the
    #     time the dispatch thread ran it the title had reused the buffer, and the
    #     stale bytes reached utf8 iteration -> utf8::invalid_utf8 -> FATAL ->
    #     0xC0000409. Gears of War Judgment died at 13s, three runs of three; 108s
    #     clean with the fix. We already fixed this once, in 2.6.0, in a fork we
    #     never upstreamed -- which is exactly how v0.10.0 shipped it back to us.
    #   * codegen: resolve a call/branch target from the graph when the per-site
    #     CallTarget table has no entry, instead of emitting REX_FATAL. 39 sites on
    #     Judgment, 28 of them conditional back-edges into registered functions.
    #     39 holes -> 0.
    #   * codegen: an out-of-range jump-table index dispatches instead of
    #     __builtin_trap(). The trap lowers to ud2, so it killed the process with
    #     STATUS_ILLEGAL_INSTRUCTION and nothing in the log -- 5s into Judgment, at
    #     one of 121 such defaults. The hot path is unchanged: the same switch at
    #     -O2 gives a byte-identical dispatch prologue either way.
    #   * codegen: [[image_patch]], byte patches applied to the decoded image
    #     before analysis. This is what makes the community patch catalogues usable
    #     in a static recompiler at all -- see gamepatches.py.
    #
    # Verified no recompilation regression against the 0.8.2 baseline on Judgment:
    # 99.2073% of code bytes vs 99.1979%, 60,146 functions vs 60,137, 0 holes in
    # both, and identical numbers before and after the clang-format pass that the
    # upstream CI requires.
    #
    # rexgpu-xenos.dll is pinned as well now: v0.10.0 moved the GPU out into a
    # plugin, so an SDK whose runtime matches but whose plugin does not renders
    # nothing while reporting only "gpu_plugin not set; call ignored".
    # v2.35.2: rebuilt with two more runtime fixes -- the InputSystem device-table
    # race that killed Gears of War Judgment between 48s and 160s of gameplay
    # (rexglue/rexglue-sdk#432), and last-chance crash diagnostics so the next one
    # leaves a symbolisable backtrace instead of an empty log.
    # v2.36: rebuilt with four more fixes, every one proven on Forza Horizon
    # (dead at 3s in v2.35.2 -> gameplay):
    #   * codegen: a bdz/bdnz whose target is a discovered function is a tail
    #     call, not a `goto` into a label that never exists (the build could not
    #     converge on sub_82AA6270 whatever the config said).
    #   * codegen: a jump-table case whose landing the graph's containing node
    #     cannot see is still an internal label of the function being emitted
    #     (embedded table data split the node; five cases lowered to REX_FATAL).
    #   * runtime: a companion loaded by bare name reaches the recompiled-module
    #     registry as \Device\Harddisk0\Partition1\X.xex, never matching the
    #     manifest's root-relative guest_path -- XMediaFacade_default.xex was
    #     recompiled and never wired.
    #   * codegen: WARN when a config override lands on a save/restore helper;
    #     the gap fill produced eight of those and 1,141 call sites lost their
    #     intrinsic (the null read in sub_8310C340).
    #   * codegen: an absorbed function is removed from the graph while call
    #     sites still hold a CallTarget pointing at its FunctionNode. Captain
    #     America read the freed node and emitted `(ctx, base);` plus
    #     `DECLARE_REX_FUNC();` -- C++ that does not compile.
    #   * kernel: answer NtQueryInformationFile(XFileXctdCompressionInformation)
    #     with "not compressed" instead of INVALID_PARAMETER. We never serve
    #     XCTD-compressed bytes (the pipeline pre-decompresses), and the error
    #     made Captain America render none of its UI.
    #   * gpu: gpu_allow_invalid_fetch_constants defaults ON. A texture fetch
    #     constant with type 0 is still bound by the real GPU; Captain America
    #     builds every Bink plane texture that way (intro and title were flat
    #     magenta) and Forza Horizon logs two thousand per run. The fork this
    #     runtime replaced shipped the same default and ran the fleet on it.
    "rexglue.exe": "f243c4d4625d416ca473dca57ed25d11675eb07375136ceb2e9b8cbc1e5ede09",
    "rexruntime.dll": "51d4346634b669af87e3c357178c84db44f2567192e51c79851db273b1fe9337",
    "rexgpu-xenos.dll": "d6aecb9eaea56510e2936388e0ec7af3a14a57a91c078973e61d11121d2a0179",
}


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_sdk_pin_checked = False


def sdk_pin_mismatch(env):
    """The first (name, expected, found) whose sha256 differs from SDK_PIN, or
    None when every pinned binary next to env["rexglue"] matches. No side
    effects and no exit: the GUI asks this to decide whether the ReXGlue row
    counts as installed at all."""
    rexglue = env.get("rexglue")
    if not rexglue or not os.path.exists(rexglue):
        return None
    _bin = os.path.dirname(rexglue)
    for name, want in SDK_PIN.items():
        path = rexglue if name == "rexglue.exe" else os.path.join(_bin, name)
        if not os.path.exists(path):
            continue
        got = _sha256(path)
        if got != want:
            return name, want, got
    return None


class SdkMismatch(SystemExit):
    """The SDK on disk is not the one this rexauto was tested with.

    Its own type so the stages that tolerate a failed codegen (image dump for
    setjmp / jump tables: `except SystemExit` -> "skipping") cannot mistake it
    for one. v2.36.0 swallowed the refusal there, and because the check was
    latched as "done" before raising, the main codegen then ran on the wrong
    SDK without a word.
    """


def verify_sdk_pin(env):
    """Refuse a mismatched SDK so an incompatible rexglue/runtime can't be used.
    Called right before any rexglue.exe use (codegen/init) -- a pure game run (the
    GUI Launch of an already-built title) never reaches it, so launching is never
    blocked by a pin mismatch; only building/codegen is gated. A PASS is
    remembered; a refusal is raised again on every call. There is no override:
    the SDK we ship is the only one these ports are known to be correct on."""
    global _sdk_pin_checked
    if _sdk_pin_checked:
        return
    rexglue = env.get("rexglue")
    if not rexglue:
        return
    _bin = os.path.dirname(rexglue)
    targets = [("rexglue.exe", rexglue),
               ("rexruntime.dll", os.path.join(_bin, "rexruntime.dll")),
               ("rexgpu-xenos.dll", os.path.join(_bin, "rexgpu-xenos.dll"))]
    for name, path in targets:
        want = SDK_PIN.get(name)
        if not want or not path or not os.path.exists(path):
            continue
        got = _sha256(path)
        if got != want:
            raise SdkMismatch(
                "[rexauto] SDK MISMATCH — refusing to run.\n"
                "  %s does not match the SDK this rexauto was built and tested with.\n"
                "    expected sha256 %s\n    found    sha256 %s\n    at %s\n"
                "  GUI: Setup -> ReXGlue SDK installs the one for this release (a rexglue/\n"
                "  left by an older rexauto is not replaced on its own).\n"
                "  CLI: extract this release's rexglue-sdk-win64.zip next to rexauto, or\n"
                "  point REXSDK_DIR / REXGLUE at it.\n"
                % (name, want, got, path))
    _sdk_pin_checked = True


# --- Shared "gabarito" database: per-binary pre-discovered cures --------------
# Once a title's heal has found its missing functions (the functions.toml
# overrides), that set is identical for everyone running the SAME binary. Publish
# it keyed by the default.xex hash so the next person seeds it and skips the slow
# auto-cure cycle. Fetch is public / no-auth; a miss just falls back to healing.
GABARITO_RAW = "https://raw.githubusercontent.com/xdzleo/rexauto/main/gabaritos"


def gabarito_key(ctx):
    """Exact per-binary key: sha256 of the entrypoint default.xex (cures are
    address-specific, so they must match the exact code image). When a title update
    is applied, codegen + runtime recompile/run the PATCHED image, so fold the
    .xexp delta into the key -- the TU build's cures are for the patched image and
    must not collide with (or be seeded from) the base build's."""
    import hashlib
    try:
        if not ctx.xex or not os.path.exists(ctx.xex):
            return None
        key = _sha256(ctx.xex)
        tu = getattr(ctx, "tu_xexp", None)
        if tu and os.path.exists(tu):
            key = hashlib.sha256((key + _sha256(tu)).encode()).hexdigest()
        return key
    except Exception:
        return None


def fetch_gabarito(ctx):
    """Seed functions.toml from the shared gabarito for this exact binary, if one
    exists, so the heal starts (mostly) solved. Returns the number of cures seeded."""
    if os.environ.get("REXAUTO_NO_GABARITO"):
        return 0
    key = gabarito_key(ctx)
    if not key:
        return 0
    try:
        import urllib.request
        with urllib.request.urlopen("%s/%s.toml" % (GABARITO_RAW, key), timeout=15) as r:
            body = r.read().decode("utf-8", "ignore")
    except Exception:
        return 0  # no gabarito for this binary -> heal from scratch
    n = len(re.findall(r'"0x[0-9A-Fa-f]+"\s*=', body))
    if n == 0:
        return 0
    with open(ctx.functions, "w", encoding="utf-8") as f:
        f.write(body)
    ctx.log("gabarito: seeded %d known cures from the shared database (xex %s…) -> "
            "auto-heal short or skipped" % (n, key[:12]))
    ctx._seeded_from_gabarito = True
    return n


def publish_gabarito(ctx):
    """Write this title's discovered cures as a gabarito file (keyed by xex hash) in
    the repo's gabaritos/ folder, so it can be committed and shared."""
    key = gabarito_key(ctx)
    if not key or not os.path.exists(ctx.functions):
        ctx.log("gabarito: nothing to publish")
        return
    src = open(ctx.functions, encoding="utf-8", errors="ignore").read()
    m = re.search(r'\[functions\].*', src, re.S)
    n = len(re.findall(r'"0x[0-9A-Fa-f]+"\s*=', src))
    out_dir = os.path.join(HERE, "gabaritos")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, key + ".toml")
    # Carry the closure measurement into the gabarito: a consumer can then see
    # how much of the title was actually recompiled without rebuilding it, and a
    # stale cure set is visible as a hole count that no longer matches.
    cl = _closure.measure(ctx.gen) or {}
    meta = ""
    if cl:
        meta = ("byte_coverage_pct = %s\ncovered_bytes = %s\ncode_bytes = %s\n"
                "static_closed_pct = %s\nstatic_targets = %d\nholes = %d\n"
                "functions = %d\nindirect_sites = %d\n"
                "switch_tables = %d\nswitch_cases = %d\n"
                % (cl["byte_coverage_pct"], cl["covered_bytes"], cl["code_bytes"],
                   cl["static_closed_pct"], cl["static_targets"], cl["holes"],
                   cl["functions"], cl["indirect_sites"],
                   cl["switch_tables"], cl["switch_cases"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write('# rexauto gabarito — pre-discovered cures for "%s" (%d)\n'
                '[meta]\nname = "%s"\nxex_sha256 = "%s"\ncures = %d\n%s\n%s'
                % (ctx.name, n, ctx.name, key, n, meta,
                   m.group(0) if m else "[functions]\n"))
    ctx.log("gabarito: wrote gabaritos/%s.toml (%d cures) — commit it to share" % (key[:12], n))


def preflight(env, args=None):
    """Refuse to start a run that cannot finish, and say what is missing.

    Before this, a machine without a toolchain got through extract, xctd, init
    and setjmp -- minutes of work, and on a title with transparent compression a
    rewritten game folder -- only to die at the build. And a machine without a
    usable Python got a whole port built with NO static jump-table recovery,
    announced by one skipped line nobody reads.

    Two tiers, because they fail differently:
      BLOCKING  nothing can be produced without these -> stop now.
      DEGRADING the port still builds, but measurably worse -> say so, loudly,
                once, at the point where it can still be fixed.
    """
    blocking = [("rexglue.exe", env.get("rexglue"),
                 "Setup -> ReXGlue SDK (one click), or set REXGLUE"),
                ("ReXGlue SDK (headers/libs)", env.get("sdk"),
                 "Setup -> ReXGlue SDK (one click), or set REXSDK_DIR"),
                ("clang", env.get("clang"), "Setup -> LLVM / clang (winget)"),
                ("clang++", env.get("clangxx"), "Setup -> LLVM / clang (winget)"),
                ("MSVC linker + Windows SDK", env.get("vcvars"),
                 "Setup -> VS Build Tools (winget)")]
    missing = [(n, h) for n, v, h in blocking if not v]
    if missing:
        lines = ["rexauto cannot build this title -- %d required tool(s) missing:"
                 % len(missing)]
        lines += ["  - %-26s %s" % (n, h) for n, h in missing]
        lines.append("Open Setup (top-right) and install them, then run again. "
                     "Nothing has been written yet.")
        raise SystemExit("\n".join(lines))
    # degrading: memory. Not a tool, but it kills codegen just as surely, and it
    # is the one condition a user can fix in ten seconds by closing a game.
    _mem = _commit_warning(_commit_state())
    if _mem:
        print("[rexauto] WARNING: " + _mem)
    # degrading: the build works, the recompilation is worse
    if not (args and getattr(args, "no_jumptables", False)):
        why = []
        if not env.get("python"):
            why.append("no usable Python (a Windows Store 'python' alias does not "
                       "count) -- Setup -> Python")
        if not env.get("idat"):
            why.append("no IDA (commercial; install it and re-run to recover more)")
        if not env.get("jt_repo"):
            why.append("no xenon-jumptables (it ships inside rexauto; a broken "
                       "install would explain this)")
        if why:
            print("[rexauto] WARNING: jump-table recovery will be SKIPPED -- this "
                  "title loses static bctr recovery entirely")
            for w in why:
                print("[rexauto]          %s" % w)
            print("[rexauto]          (on Gears of War Judgment that pass found 109 "
                  "tables / 3,142 targets)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("container", help="STFS package, GoD header file or folder, Xbox 360 ISO, or a folder containing default.xex")
    ap.add_argument("--name", required=True, help="project name (a-z0-9_)")
    ap.add_argument("--work", default=os.environ.get("REXAUTO_WORK", r"C:\Skate3\autoports"),
                    help="output root (or env REXAUTO_WORK)")
    ap.add_argument("--run", action="store_true", help="launch the game at the end")
    ap.add_argument("--from", dest="from_stage", choices=STAGES, help="restart from this stage")
    ap.add_argument("--only", choices=STAGES, help="run just this stage")
    ap.add_argument("--until", dest="until_stage", choices=STAGES,
                    help="stop after this stage (e.g. --until build skips runheal)")
    ap.add_argument("--no-jumptables", action="store_true")
    ap.add_argument("--no-title-update", action="store_true",
                    help="do not auto-detect/apply an Xbox 360 title update (.xexp); "
                         "build the base game version")
    ap.add_argument("--heal-iters", type=int, default=20)
    ap.add_argument("--run-seconds", type=int, default=22)
    ap.add_argument("--patch", action="append", metavar="NAME",
                    help="community patch to compile in (repeatable); see --list-patches")
    ap.add_argument("--no-patches", action="store_true",
                    help="remove every community patch from the port and rebuild clean")
    ap.add_argument("--list-patches", action="store_true",
                    help="list the community patches available for this title and exit")
    ap.add_argument("--publish-gabarito", action="store_true",
                    help="write the discovered cures to gabaritos/ (keyed by xex hash) to share")
    args = ap.parse_args()

    env = detect_env()
    ctx = Ctx(args, env)
    if args.list_patches:
        # Antes do preflight: so lista, nao constroi nada, entao exigir clang e
        # vcvars aqui seria barrar quem so quer ver o que existe para o titulo.
        c = _gamepatches.catalog(ctx.port)
        print("port=%s  title_id=%s" % (c["name"] or args.name, c["title_id"] or "?"))
        if c["error"]:
            print("aviso: %s" % c["error"])
        if not c["patches"]:
            print("nenhum patch da comunidade para este titulo")
            raise SystemExit(0)
        print("fonte: github.com/xenia-canary/game-patches -- %s" % ", ".join(c["files"]))
        print()
        for p in c["patches"]:
            mark = "x" if p["applied"] else " "
            print("[%s] [%-10s] %s" % (mark, p["seal"], p["name"]))
            if p["desc"]:
                print("        %s" % p["desc"][:100])
            if p["author"]:
                print("        autor: %s | %d escrita(s) | %s"
                      % (p["author"], p["writes"], p["why"][:70]))
        print()
        print("RECOMPILAR escreve em .text: entra no codigo nativo e exige rebuild do port.")
        print("RUNTIME so toca dados.  [x] = ja aplicado.")
        print('aplicar:  --patch "Unlock FPS" --patch "Disable Film Grain"')
        print("limpar:   --no-patches")
        raise SystemExit(0)

    ctx.log("tools: rexglue=%s sdk=%s clang=%s ida=%s vcvars=%s"
            % (bool(env["rexglue"]), bool(env["sdk"]), bool(env["clang"]),
               bool(env["idat"]), bool(env["vcvars"])))
    preflight(env, args)

    order = STAGES[:]
    if args.no_jumptables:
        order.remove("jumptables")
    want_run = args.run or args.from_stage == "run" or args.only == "run"
    if not want_run:
        order.remove("run")
    if args.from_stage and args.from_stage not in order:
        raise SystemExit("--from %s: that stage is disabled by the current flags" % args.from_stage)
    if args.only and args.only not in STAGES:
        raise SystemExit("--only %s: unknown stage" % args.only)

    state = ctx.load_state()
    fns = {"extract": stage_extract, "xctd": stage_xctd, "init": stage_init, "setjmp": stage_setjmp,
           "jumptables": stage_jumptables, "deepextract": stage_deepextract,
           "build": stage_build, "runheal": stage_runheal, "run": stage_run}
    start = order.index(args.from_stage) if args.from_stage else 0
    selected = [args.only] if args.only else order[start:]
    if args.until_stage and not args.only:
        if args.until_stage not in selected:
            raise SystemExit("--until %s: that stage is not in the selected range" % args.until_stage)
        selected = selected[:selected.index(args.until_stage) + 1]

    # The two log lines below are a public interface -- the GUI's stage tracker
    # string-matches "=== stage: " and "skip ... (done)" off this stdout (gui/
    # server.py) -- so the timing instrument wraps them and never reformats them.
    try:
        for stage in selected:
            if not args.only and not args.from_stage and state.get(stage):
                ctx.log("skip %s (done)" % stage)
                ctx.timing_skip(stage)  # recorded as skipped, NOT as 0 seconds
                continue
            ctx.log("=== stage: %s ===" % stage)
            with ctx.timer(stage):
                fns[stage](ctx)
    finally:
        # In a finally so a stage that raises still leaves a run record -- marked
        # "partial", because a total that silently omits the stage that blew up is
        # a lie about what the run cost.
        ctx.timing_run_end(selected)
    ctx.log("done. project: %s" % ctx.port)
    if not want_run and os.path.exists(ctx.exe):
        launcher = os.path.join(ctx.builddir, "play %s.cmd" % ctx.name)
        if os.path.exists(launcher):
            # the tolerant launch is the one to hand a player; the bare exe keeps
            # the strict dispatcher the heal needs to find its next target
            ctx.log('to play:  "%s"' % launcher)
            ctx.log('  strict: "%s" --game_data_root="%s"' % (ctx.exe, ctx.game))
        else:
            ctx.log('to play:  "%s" --game_data_root="%s"' % (ctx.exe, ctx.game))


if __name__ == "__main__":
    main()
