# rexauto

**Turn an Xbox 360 game into a native PC executable — automatically.**

[![latest release](https://img.shields.io/github/v/release/xdzleo/rexauto?label=download)](https://github.com/xdzleo/rexauto/releases/latest)
[![license](https://img.shields.io/github/license/xdzleo/rexauto)](LICENSE)
![platform](https://img.shields.io/badge/platform-Windows-blue)
![python](https://img.shields.io/badge/python-3.10%2B-blue)

rexauto is a desktop front-end and orchestrator for the
[ReXGlue](https://github.com/xdzleo/rexglue-skate3) static recompiler. Point it at a game
container — ISO, GoD, STFS or an extracted folder — and it runs the entire pipeline that is
otherwise a day of by-hand work: extract, scaffold, recover jump tables, build, and the two
self-heal loops a fresh title needs. Then it launches the result.

<p align="center"><img src="gui/rexauto_icon.png" width="96"></p>

## This is not emulation

An emulator reads guest PowerPC instructions and interprets or JITs them **every time you
run the game**. Static recompilation translates the Xbox 360 binary into C++ **once**, then
compiles it into a real x86-64 executable. Your CPU runs the game's machine code directly —
no interpreter, no per-instruction overhead, no emulator in the loop.

The output is a `.exe`. It shows up in Task Manager as your game.

## Titles taken through the pipeline

Status is per title and honest — recompilation getting a build to boot is a different problem
from the runtime implementing every GPU and kernel path a given game uses.

| Title | Status |
|---|---|
| Skate 3 | playable |
| GTA: San Andreas HD | gameplay |
| GTA V | reaches gameplay |
| Dragon Ball Z: Budokai 3 HD | battle running |
| Captain America: Super Soldier | gameplay |
| WWE SmackDown vs. Raw 2007 | playable to menu |
| Gears of War: Judgment | boots, converges |
| Crash of the Titans | boots |

A wider fleet (~30 titles) is used as a regression gate: every SDK change has to keep the
whole set byte-identical or better before it ships.

## Download & run

1. Grab `rexauto.exe` from the [latest release](https://github.com/xdzleo/rexauto/releases/latest).
2. Run it. A native window opens (Edge WebView2): a 3D scene, the game's cover art and title
   read straight from the package, a live six-stage tracker, and a streaming log.
3. First run, the app installs the **ReXGlue SDK** by itself — the exact build this release
   was tested with, laid out next to the app (a `rexglue/` left by an older release is replaced
   the same way). Open **Setup** (top-right) for the rest:
   - **LLVM/clang** + **VS Build Tools** — via `winget`.
   - **IDA Pro** — optional, commercial; only the jump-table stage uses it.
4. Point it at a container, hit **Recompile**, watch it go.

> rexauto *drives* a C++ compiler — it isn't one. A real recompiler has to build the C++ it
> generates (tens of thousands of functions), so a clang + Windows SDK toolchain is required;
> there's no 15 MB "zero-dependency" recompiler. What rexauto does is make installing all of
> it one button instead of a scavenger hunt.

## The pipeline

1. **extract** — container → `default.xex` + assets: **STFS** (`CON`/`LIVE`/`PIRS` — XBLA/DLC),
   **ISO** (GDFX/XDVDFS disc), **GoD** (SVOD single-file), or an already-extracted folder.
2. **init** — `rexglue init` scaffolds the project.
3. **setjmp** — finds the statically-linked CRT `setjmp`/`longjmp` and records them in the
   manifest. A guest `longjmp` restores registers + stack from a `jmp_buf` and `blr`s;
   recompiled naïvely that `blr` becomes a plain `return`, corrupting a non-volatile register
   and crashing exception-using titles at startup. Titles without exceptions have no signature
   and are left untouched.
4. **jumptables** — with IDA present, recovers `bctr` jump tables into `switch_tables.toml`
   ([xenon-jumptables](https://github.com/xdzleo/xenon-jumptables)). Skipped cleanly otherwise
   — the recompiler's built-in switch handling still applies.
5. **build** — codegen + clang/CMake. When the recompiler splits a function mid-flow (a branch
   into the next one → a `goto` to an undeclared label), rexauto **auto-extends** the boundary
   and rebuilds — the fix porting teams otherwise make by hand — until it's clean.
6. **runheal** — runs the game; every `invalid or unregistered function at 0xADDR` the
   dispatcher hits gets registered, rebuilt, and re-run, until none are left.
7. **run** — launches it.

## Shared cures (the gabarito database)

The slow part of a fresh port is the heal loops re-discovering the functions the static pass
missed — and that set is **identical for everyone running the same binary**. So rexauto
publishes it: once a title converges, its cures (a `functions.toml` keyed by the `default.xex`
SHA-256) go into a shared database, and the next person to recompile that exact binary seeds
them up front and skips most of the heal. Fetch is public and keyless; a miss just heals from
scratch.

The bundled SDK is **pinned by hash** — rexauto refuses to run against an SDK build it wasn't
tested with, so a mismatched runtime can't silently produce a broken exe. There is no override.
The SDK itself is reproducible: branch
[`rexauto`](https://github.com/xdzleo/rexglue-sdk/tree/rexauto) of our fork is upstream `main`
plus every fix we carry, each one also an open upstream PR; its tree is byte-identical to the
source the bundled binaries were built from.

## CLI

Same engine, no window:

```sh
python rexauto.py "<container-or-folder>" --name mygame --run
```

Stages are checkpointed (re-running skips finished ones). Flags: `--from <stage>`,
`--only <stage>`, `--no-jumptables`. Tool paths come from the usual install locations, `PATH`,
or env vars (`REXGLUE`, `REXSDK_DIR`, `IDAT`, `CLANG`, `VCVARS`, `PYTHON`, `JT_REPO`).

## One click from a phone (GitHub Actions)

No PC handy? The repo ships a workflow that does the whole pipeline on a hosted Windows runner:

1. Upload `default.xex` (or the ISO / GoD / STFS / a `.zip` with one) anywhere that gives a
   link — Google Drive, Dropbox, or any direct URL.
2. **Actions → Recompile game → Run workflow**, paste the link, pick a project name, press
   the green button.
3. When it finishes, the `port-<name>` artifact holds `<name>.exe`, the runtime DLLs and
   `play <name>.cmd`; `logs-<name>` has every log; `gabarito-<name>` has the cures the heal
   loop found (drop them into `gabaritos/` to skip the heal next time).

The runner installs the pinned ReXGlue SDK by itself (cached across runs) and uses the
clang / VS Build Tools already on `windows-latest`. IDA isn't available there, so the
jumptables stage is skipped; the run-heal loop is on by default (`runheal` input) and can
be turned off for a quick build-only pass. Unzip the artifact next to the game folder on a
PC (or phone via Winlator), copy the game files into its `assets/` folder and run
`play <name>.cmd` or the exe. The port is **portable**: the game is looked up in
`assets/`, `game/`, `data/` next to the exe and saves go to `userdata/` next to it —
no absolute paths from the build machine are baked in.

## What it does NOT do

rexauto gets you to a **booting, guest-code-executing build, automatically**. It does **not**
close per-title GPU/emulation gaps: a game using vertex formats or kernel calls the ReXGlue
runtime doesn't implement yet will boot, open a window, and reach the render loop but may not
draw correctly or stay up. That's runtime-emulation work — separate from recompilation, and
inherently per title. rexauto removes the mechanical pipeline; the runtime backend is still
where a given title lives or dies.

You supply the game. rexauto does not download, decrypt or distribute copyrighted content.

## Build from source

```sh
pip install pywebview pyinstaller pillow
python gui/make_icon.py
pyinstaller --noconfirm --onefile --windowed --name rexauto \
  --icon gui/rexauto.ico --add-data "gui/index.html;gui" \
  --add-data "vendor/xenon-jumptables;xenon-jumptables" \
  --add-data "thirdparty/libmspack;thirdparty/libmspack" --add-data "tools;tools" \
  --paths gui \
  --hidden-import extract --hidden-import heal --hidden-import closure \
  --hidden-import codegen_patches --hidden-import jt_landings \
  --hidden-import rexauto --hidden-import xctd \
  --hidden-import detect_setjmp --hidden-import server --hidden-import setup \
  --collect-all webview app.py
```

One binary, two modes: no args → the GUI; `--__pipeline …` → the recompiler (the GUI
re-invokes itself to stream the pipeline). Or run it as a plain web app: `python gui/server.py`.

## Related projects

- **[xenon-jumptables](https://github.com/xdzleo/xenon-jumptables)** — recovers PowerPC
  `bctr` jump tables that static recompilers miss. Used by the jumptables stage.
- **[rexglue-sdk](https://github.com/xdzleo/rexglue-sdk)** — the Xbox 360 recompilation
  runtime and toolkit.

## Credits

- **ReXGlue** — the static recompiler + runtime rexauto drives (© Tom Clay, BSD-3; derived
  from [Xenia](https://github.com/xenia-project/xenia)). Releases bundle a prebuilt copy of
  the [skate3 fork](https://github.com/xdzleo/rexglue-skate3) for one-click setup — see
  [NOTICE](NOTICE).
- **[xenon-jumptables](https://github.com/xdzleo/xenon-jumptables)** — the jump-table /
  boundary recovery behind the jumptables stage.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party components keep their own licenses
([NOTICE](NOTICE)).

---

<sub>Keywords: Xbox 360 to PC · static recompilation · PowerPC / Xenon decompilation · XEX ·
native port · game porting · reverse engineering · XenonRecomp</sub>
