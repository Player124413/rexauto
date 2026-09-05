# Changelog

## 2.36.5 — "pick your build" (2026-09-05)

**The patch list shows up again.** Forza Horizon's patch panel was empty, and so
was the panel for every title the catalogue does not cover — for two different
reasons, neither of them visible.

*A title with several patch files showed none of them.* The community catalogue
publishes one file per build: `4D5309C9 - Forza Horizon.patch.toml`, a `(TU4)`
one, and `4D5309C9 - Forza Horizon (E3 Demo).patch.toml`. Their addresses are
not interchangeable, so rexauto picks one — by whether the port has a title
update staged. That signal separates TU from base and says nothing about a demo,
so Forza tied and the tie was a hard error: the panel rendered the message
"ambiguo: 2 variantes casam" and no patches at all. It is now a choice. The
plainest name wins by default (a variant qualifies itself in parentheses; the
retail build does not), a dropdown offers the others, and the pick is remembered
in `patch_variant.txt` next to the port so the next build applies the same file.
Forza Horizon offers its 4 patches, and switching to the demo build shows that
file's single patch instead.

*A title with no patches said nothing.* Captain America, Spider-Man and DBZ
Budokai have no entry in the catalogue at all — 482 files, none for those IDs.
The panel simply came up blank, which reads like a failure. It now says so, with
the title ID it looked for.

## 2.36.4 — "double-click" (2026-09-05)

**A built port starts by double-clicking it.** Two things stood between a
finished port and a picture, and both are now handled by the runtime the setup
installs.

*The game data.* Opening the exe with no arguments popped
`--game_data_root was not provided.` and quit: the flag was the only way in, and
an empty value was a hard error before the runtime was even constructed — which
is why every port needed its `play <name>.cmd` wrapper. The runtime now takes
the first location that actually holds a `default.xex`: the `game_root.txt`
sidecar the pipeline already wrote next to the exe, a `game` folder beside it,
then the exe's own folder. An explicit `--game_data_root` still wins. v0.8.0
had this and it was lost in the v0.10.0 migration.

*The GPU.* With the GPU split out into `rexgpu-*.dll`, a port started without
`REX_GPU_PLUGIN` loaded no graphics at all: every `Vd*` kernel call logged
"gpu_plugin not set; call ignored" and the window stayed black. The plugin sits
right next to the exe, staged by the build. When nothing names one, the runtime
now adopts the Xenos plugin if it is there, or the only `rexgpu-*.dll` present,
and logs the choice. Two or more with no Xenos stays ambiguous and is left to
the cvar.

Both live in `share/rexglue/rex_app.cpp`, the source each port compiles, so the
pinned SDK binaries are unchanged; both are commits on the SDK fork
(`app-game-data-root-fallback`, `app-gpu-plugin-autoselect`).

**The jump-table repair now reaches FIFA Street.** 2.36.3 only rewrote a drifted
`labels` array when the recovered targets also read back out of the dumped image
at the table's address. That second opinion assumes the dump is a flat
base-relative mapping, and FIFA Street's is not: its 1.8 MB image does not hold
the tables that sit `0x27C0` past the base, so the check read unrelated bytes,
called the recovery wrong and left both damaged tables in place. The image is
now calibrated before it may veto anything — the blocks that already agree with
`jumptables.json` are read back first, and only if the image confirms those does
a disagreement elsewhere block a repair. FIFA Street repairs its 2 tables of 24;
every other port comes out byte-identical, and Captain America's 43-table repair
still reproduces.

**A stale precompiled header no longer fails a build.** Installing a new SDK
gives every header a new mtime; ninja rebuilds the `.pch`, but the objects that
consume it are not ordered after it, so a build can compile against the stale
one and clang refuses every translation unit. That is now a cure like any other:
the `.pch` files are removed and the build retried.

**Gate.** Gears of War Judgment (99.1966% / 60,140 / 0), Dante's Inferno
(98.9891% / 36,546 / 0) and Captain America (99.0722% / 32,605 / 0) built and
stayed alive for the full 60-second run, every number unchanged. Forza
Horizon builds and keeps its numbers, and dies on start roughly two runs in
three with `0xC0000005` in `sub_82D3CD48` or `sub_82D3DB00`; rebuilding it
with today's app source reverted reproduces exactly that, so it is the
title's own intermittent startup crash and not a regression. Spider-Man and
FIFA Street still fail to build on their existing backlog causes.

## 2.36.3 — "one label too many" (2026-09-05)

**Captain America's video plays.** The Marvel intro decodes cleanly, in colour,
at the video's own 24 fps. The cause was a single wrong line in the port's own
`ca_switch_tables.toml`: the jump table of Bink's block decoder (`bctr` at
`0x82871704`) carried **11 labels for a 10-entry table**, and the extra one —
the address of the table itself — sat at index 0.

That array is positional. ReXGlue v0.10.0 lowers a `bctr` as
`switch (index) { case i: goto labels[i]; }`, so one extra label re-points
every case after it: block type 0 decoded with the handler for type 1, type 1
with type 2's, and so on down the table. Everything still ran — the file was
read correctly, the ring buffer held the right bytes, the audio track was
untouched — and every frame came out as noise. v0.8.2 lowered the same table as
`switch (ctx.ctr.u32) { case 0xTARGET: ... }`, keyed on the computed address,
where a damaged array is harmless. That is the whole of "it works on the old
SDK and not on the new one", and why the emitted C++, the machine code, the
input bytes and the kernel-call sequence all compared identical.

**The damage was ours, and it outlived its fix.** An earlier heal merged
runtime-discovered landings into these arrays with `sorted(set(...))`, which
deduplicates and re-sorts a list whose order *is* the dispatch. That merge was
disabled in 2.36.0 after it rewrote 10 of Forza Horizon's 325 tables, but a
port keeps the file it was given: the jump-table stage is skipped whenever
`<name>_switch_tables.toml` already exists, so no later run ever repaired it.
Captain America was carrying 43 damaged tables out of 242, FIFA Street 2 of 24.

**`verify_switch_tables` now runs before every codegen.** It compares each
block's labels against `jumptables.json` — the IDA pass's own output — and
rewrites any that drifted, positionally. A table is only repaired when the
recovered targets still read back out of the image at the table's address, and
the whole pass refuses to touch a file when the json describes a different
module. A port whose tables are intact comes out byte-identical, so titles that
never suffered the merge see nothing.

**Gate.** Build plus one 60-second production run per title, no heal cycles.
Gears of War Judgment (99.1966% / 60,140 / 0), Dante's Inferno (98.9891% /
36,546 / 0), Forza Horizon (98.9167% / 79,089 / 0) and Captain America all
built and stayed alive for the full run; the first three keep every number
they had. Captain America moves 99.0718% -> 99.0722% by byte coverage and
32,643 -> 32,605 functions, which is the repair itself: 43 tables' worth of
case targets are now classified against the right addresses.

Spider-Man: Shattered Dimensions and FIFA Street still fail to build, on the
two causes already in the backlog (an oscillating undeclared label, and a
legacy `_app.h` plus the missing `XUsbcamGetState` import). Neither port's
switch tables were touched by this release -- their files are untouched since
4 September -- so this is the state they were already in.

## 2.36.2 — "the type field nobody reads" (2026-09-05)

**Captain America renders.** Its logo screen, title art and "press start" were
already correct; everything drawn from a texture the game had built at runtime
was flat magenta. The Xenos plugin refused every texture fetch constant whose
2-bit type field said "invalid" (0) — and that is how Captain America builds
every one of its Bink video planes, 19,000 refusals per launch. The real GPU
only uses that field to tell texture fetches from vertex fetches; the fork this
runtime replaced shipped `gpu_allow_invalid_fetch_constants=true` and ran the
whole fleet on it, which is why "it used to work". The bundled SDK now defaults
it on. Forza Horizon, which logged two thousand of the same refusals per run,
keeps every number it had. The launcher checkbox and the per-title "quirk"
detection that 2.36.0 added for this are gone: there is nothing left to toggle.

**Still open on Captain America:** the Bink video itself. With the planes now
bound, the decoder writes garbage into them — wrong from the first
macroblock, "DC right, AC wrong". This is not codegen: the Bink library's
recompiled code, its callees two levels down, its jump tables, the loaded
image sections, the compiled machine code of the IDCT, the bytes it reads from
the file, its decode rate and the sequence of kernel calls it makes are all
identical between this build and a v2.27.0/0.8.2 build that decodes the intro
correctly (and then goes black before the logos). Fences, flush-to-zero,
`-fasync-exceptions`, TLS, thread priorities, heap scribbling and allocation
zeroing were each tried or ruled out. The 151 Bink functions are instrumented
in both trees for a call-stream diff; that is the next step.

**Gate = one run.** The fleet gate is a build plus a single 60-second
production run per title. No heal cycles, no 360-second confirms.

**A bad rebuild caught by the gate.** Undoing an experiment with
`git checkout -- helpers.h` also discarded the uncommitted `bdz` tail-call fix
that lives in the same file; the rebuilt `rexglue` regressed Forza to
`undeclared label loc_82AA6444`. The gate refused it, the file was restored
from the fork's branch, and the tree was re-proven byte-identical to the
fork's `rexauto` branch before installing. Every patch we ship has its commit
in the fork; the working tree in `sdk2` is never the reference.

**Upstream.** Every pull request we opened against `rexglue/rexglue-sdk`
(#300–#440) was closed unmerged and the account is blocked there. The SDK
rexauto ships is built from the fork's `rexauto` branch, which carries all
fifteen fixes as their own branches.

## 2.36.1 — "the wrong SDK ran anyway" (2026-09-05)

Two bugs reported against 2.36.0 within the hour, both real, both in how the
release protects itself.

### The SDK gate could be talked past

A `rexglue/` laid down by an older release sat next to the new `rexauto.exe`.
The pin caught it -- `SDK MISMATCH — refusing to run` -- and then the pipeline
**kept going**: the image-dump codegen tolerates a failed dump (`except
SystemExit` -> "skipping setjmp detection"), the refusal was a `SystemExit`,
and because the check latched itself as "done" *before* raising, the main
codegen that followed ran on the unpatched SDK without a word. Skate 3 then
spent its build stage in a loop -- `codegen stuck on unresolved calls:
0x881715C8` -- a target the unpatched recompiler cannot resolve (it is exactly
the shape upstream PRs #435/#437 fix). With the pinned SDK that module emits
zero unresolved calls.

Now: the refusal is its own exception the tolerant stages re-raise; a pass is
remembered, a refusal is raised again on every call; and there is **no
override** any more -- `REXAUTO_SKIP_SDK_CHECK` is gone. Every port rexauto has
ever shown correct was built on these exact binaries, and nothing else is
allowed to build one.

### Setup runs itself, and fetches the right release

The GUI now treats a right-version-wrong-build SDK as *not installed*, says
why (`v0.10.0, but not the build this release was tested with (rexglue.exe is
194b8d37c4a0..., need 96baf3fbe884...)`), and installs the correct one at
startup, unasked. The download is by **this release's tag**
(`releases/download/v2.36.1/...`), not `latest` -- `latest` would hand an
older rexauto a newer SDK its pin then refuses, the same trap from the other
side. After extraction the pin is verified again before the install counts.

### Multi-XEX titles lost their entrypoint jump tables

With `[[modules]]` in the manifest the codegen trace lists every module's
sections, and the jump-table stage took min/max of all of them: Skate 3's
eawebkit sections at `0x88...` pushed the range to `0x8849CAD0`, the sanity
check refused it (`parsed section range looks wrong -> skipping`), and the
entrypoint silently lost static bctr recovery. Sections outside the entrypoint
image are a companion's, not a parse error, and are set aside.
**Skate 3: 0 -> 360 jump tables, 8,716 targets.**

### The SDK is reproducible from the fork

Branch [`rexauto`](https://github.com/xdzleo/rexglue-sdk/tree/rexauto) of our
fork is upstream `main` plus all fourteen fixes we carry, each also its own
branch and open upstream PR (#427–#440; #440, the crash report beside the
executable, is new). Its tree is byte-identical to the source the bundled
binaries were built from. Two PR branches were tidied in the process (#431
carried a duplicated statement, #435 literal tabs inside a string); the
merged result did not change.


## 2.36.0 — "the fleet was never rebuilt" (2026-09-04)

**Forza Horizon reaches gameplay.** It was dead at 3 s; the three things it
needed turned out to matter for the rest of the fleet too, and chasing them
surfaced a fourth: **not one port in the local fleet could be rebuilt against
the SDK we ship**.

### The fleet was stale, and nothing said so

`src/<name>_app.h` is the one file the pipeline never regenerates — it is the
file a user may edit. ReXGlue 0.8.2's `init` emitted `<name>_PPCImageConfig`;
v0.10.0 emits `PPCImageConfig`. So every port created before v0.10.0 failed with
a single error the moment its generated tree was rebuilt. All five local titles
(`gears_of_war_judgment`, `dante_s_inferno`, `ca`, `spider_man_dimensions`,
`fifa_street`) died on it; only a freshly-initialised Forza passed. The
`converged` / coverage numbers those ports carried in `.rexauto_state` were from
the 0.8.2 era and proved nothing about the pinned SDK. `_migrate_legacy_app_header`
renames that one symbol, only when the generated tree does not declare the
prefixed name, so user edits survive.

### The run-heal was judging a game nobody plays

Every heal launch ran in **discover mode** (a no-op'd call returns instead of
doing its work, so every branch after it can differ) and **without the GPU
plugin** (no swap, no vblank, no render thread). Forza collected four clean 360 s
verdicts for an exe that died 2 s into every production run. Now: the last launch
before a convergence verdict is a production one, with the same GPU plugin and
frame cap the player gets; a crash is reported as a crash (symbolised through the
port's PDB) instead of "other stop"; and `run_once` reads **every rotated part**
of a launch's log — Forza's 360 s window produced three parts and 80,000 lines,
and only the newest was being read.

### Companion XEX support, rebuilt on the SDK's own mechanism

The 0.8.2 route — recompile the module alone, link its sources into the same exe,
register its function table by hand in `OnPostSetup` — cannot compile against
v0.10.0: the generated `app.h` calls a 6-argument `InitializeFunctionTable` and
reads `PPCImageInfo::function_table_base`, neither of which exists, and
`symbol_prefix` is gone from the manifest. `setup_extra_modules` now declares each
companion as a `[[modules]]` entry of the entrypoint manifest, so one codegen
emits entrypoint + companions and the runtime binds the shared library at the
guest's `XexLoadImage`. Ports carrying the old glue get it stripped. Detection no
longer needs the runtime log (v0.10.0 never prints `XEX image loaded at LO-HI`) —
the load address and image size come out of the XEX header itself.

### Cures the pipeline was missing, and cures it should never have made

- **Function pointers the code BUILDS.** A callback materialised as
  `lis rA,hi; addi rB,rA,lo` and handed to a call appears in no data word, so the
  data-pointer scan cannot see it and the run-heal found them one
  launch-and-crash at a time. Forza's heal was curing exactly this family, 15
  launches for 15 functions; the static scan finds 50 before the first launch.
- **Nothing may sit on a save/restore helper table.** Gap fill produced overrides
  on all eight of Forza's helper heads, and a config entry outranks the SDK's
  helper detection: 472 + 669 call sites lost their intrinsic and the game read
  NULL through `r20`. The tables are detected from the image and excluded from
  every scan; stray entries are dropped on load.
- **`extend_switch_table` is disabled.** v0.10.0 lowers a recovered table as
  `switch (index) { case i: goto labels[i]; }` — the array is **positional**,
  duplicates included — where 0.8.2 keyed cases on the computed CTR value.
  Merging a landing in as a sorted, deduped set silently re-pointed every case
  after the first change: on Forza it rewrote 10 of 325 tables and dropped 65
  duplicate slots.

### XAPI fibers

A recompiled `SwitchToFiber` tail-calls `KeSetCurrentStackPointers`, whose `blr`
was meant to land in the *other* fiber; statically that returns into the old
fiber's host frames carrying the new fiber's registers. v0.10.0 ships host fibers
behind `[rexcrt]` hooks but nothing detects the guest routines, so
`_detect_xapi_fibers` finds all five by signature (KTHREAD `fiber_ptr` access,
`ERROR_ALREADY_FIBER`/`ERROR_ALREADY_THREAD`, sole callers of
`MmCreateKernelStack`/`MmDeleteKernelStack`) and writes `<name>_rexcrt.toml`.

### Launcher: image quality

`resolution_scale` (1×–4× internal), `swap_post_effect` (FXAA / FXAA extreme) and
`anisotropic_override`, all defined by the xenos plugin we already ship, with the
SDK's real ranges. `present_effect` (CAS/FSR) is deliberately absent: it only
exists under `REXGLUE_ENABLE_FIDELITYFX`, which our SDK build does not set —
exposing it would be a dead control.

A title whose fetch constants the plugin calls "invalid" gets that recorded and
offered in the launcher, **never enabled for you**: binding the descriptor anyway
trades a magenta area for whatever that memory holds.

### SDK re-pinned — six fixes, each proven on a title

All six are open upstream. `bdz`/`bdnz` to a function entry is a tail call
(#434); a jump-table landing inside the function being emitted is a `goto`
(#435); a companion loaded by bare name matches its root-relative `guest_path`
(#436); an absorbed function no longer leaves dangling `CallTarget`s — Captain
America emitted `DECLARE_REX_FUNC();` and could not compile (#437);
`XFileXctdCompressionInformation` answers "not compressed" instead of
`INVALID_PARAMETER` (#438); a config override on a save/restore helper warns
(#439).

### Fleet, same SDK, before → after

| title | coverage / functions / holes | verdict |
|---|---|---|
| `forza_horizon` | 98.9167% / 79,089 / 0 (unchanged) | crash `0xC0000005` → **converged, 360 s production** |
| `gears_of_war_judgment` | 99.1966% / 60,140 / 0 (unchanged) | converged → **converged, 360 s production** |
| `dante_s_inferno` | 98.9891% / 36,546 / 0 (unchanged) | crash `0xC0000005` → **converged, 360 s production** |

Coverage on Gears moved by −188 bytes because the six save/restore helper tables
stopped being emitted as functions and 21,656 call sites got their intrinsic
back. Not a regression: fewer bytes of C++, more correct code.

### Known, named, not fixed

- **`ca`** builds and its logo screen renders perfectly, but the title screen
  background is magenta: the game presents that texture's fetch constant with
  `type=0`, which the plugin drops. Not the cure set (the proven 218-cure
  gabarito shows the same), not XCTD, not the three known VMX builder bugs.
- **`spider_man_dimensions`** does not converge: 9 undeclared labels the landing
  heal forces and retires in a cycle.
- **`fifa_street`** needs `XUsbcamGetState` / `XUsbcamSetConfig` in the SDK.
- **`dante_s_inferno`** converges but the rip carries only `bigfile0/1.viv`; the
  game asks for `BIGFILE2..12`.


## 2.35.2 — "name the corpse" (2026-09-04)

**The port stops dying in the middle of gameplay.** Ships a rebuilt ReXGlue SDK
with two more runtime fixes, both open upstream.

### The crash was a data race in the input system

Gears of War Judgment had been dying between roughly 48 s and 160 s with
`STATUS_HEAP_CORRUPTION` (0xC0000374) or an access violation inside the
allocator — and **nothing in the log**, because both are fail-fast paths that no
handler sees. It was listed as an honest open limit in 2.35.0. It is fixed.

`InputSystem` had no synchronisation at all, and `RefreshDevices()` clears and
rebuilds `devices_` / `device_owners_` on **every** call. It is called from all
four guest-facing entry points, which titles reach through `XamInput*` from more
than one guest thread at once. Two concurrent input polls were freeing and
reallocating the same `std::vector`:

```
guest sub_82DCA948 -> sub_8232D958 -> sub_8299CAE0
  XamInputGetState_entry            xam_input.cpp:115
  InputSystem::RefreshDevices()     input_system.cpp:157
  std::vector<DeviceInfo>::clear() -> _Destroy_range -> deallocate   <-- faults
```

The lock covers the whole body of each entry point, not just `RefreshDevices()`:
`assignment_` and `active_devices_` are the same shared state reached from the
same threads, so guarding the vectors alone would have moved the race rather than
closed it.

| | result |
|---|---|
| before | died at 13 s / 54 s / 74 s / 121 s / 160 s |
| after | three runs of 200 s, zero fatals, no crash report |

Recompilation is untouched: 99.2073% of code bytes, 60,146 functions, 0 holes —
the same numbers as 2.35.1.

### A crash now leaves something behind

Finding the above took building diagnostics that did not exist, so they ship. The
runtime installs a `std::terminate` handler and an unhandled-exception filter that
append to `rexglue-crash.txt` beside the executable: the exception type and
message (or the fault code and address) plus a return-address backtrace as
`module+RVA`, which `llvm-symbolizer` turns into real frames against the port's
PDB.

Its honest limit, learned the hard way: a heap-corruption fail-fast bypasses both
handlers and still writes nothing. It is the access-violation variant that gets
caught — which was enough here, and is enough for the ordinary case of an
exception escaping a `noexcept` boundary.

### Measured in passing

Our locally built `rexglue` does codegen about 21% slower than the upstream
prebuilt (13.4 s vs 11.1 s on Dante's Inferno, 36,479 functions). That is the
build environment, not our patches: the same tree with all four codegen patches
reverted takes 13.5 s. Noted, not fixed.

Upstream now: [#427](https://github.com/rexglue/rexglue-sdk/pull/427),
[#428](https://github.com/rexglue/rexglue-sdk/pull/428),
[#429](https://github.com/rexglue/rexglue-sdk/pull/429),
[#430](https://github.com/rexglue/rexglue-sdk/pull/430),
[#431](https://github.com/rexglue/rexglue-sdk/pull/431),
[#432](https://github.com/rexglue/rexglue-sdk/pull/432).

## 2.35.1 — "a check that shrugs is not a check" (2026-09-04)

### An SDK older than v0.10.0 is now refused outright

`SDK_PIN` already rejected anything but the exact tested binaries, but
`REXAUTO_SKIP_SDK_CHECK=1` gets past it — and that flag exists for a good reason,
so it gets used. Underneath it, an old SDK does not fail loudly. It builds a port
that is **quietly wrong**:

- `[[image_patch]]` lives in the manifest. An older `rexglue` ignores the block,
  so every community game patch silently vanishes from the build.
- The GPU moved out into `rexgpu-*.dll`; the generated launcher names a plugin
  older runtimes know nothing about.
- The codegen ranges moved from `<name>_init.h` to `<name>_pch.h`.

So the floor is a separate check from the pin, and it is **not** bypassable by
`REXAUTO_SKIP_SDK_CHECK`. It reads `rexglue --version`, compares against
`SDK_MIN_VERSION = (0, 10, 0)`, and stops the run in 0.4 s — before the pipeline
touches anything.

**It fails closed.** The first version of this check warned and continued when it
could not read a version, which turned out to matter: the 0.8.2 binary answers
`--version` with an **empty stdout and exit code 0 roughly one run in three**, so
the check passed an old SDK at random. It now retries, and refuses if it still
cannot tell. `REXAUTO_ALLOW_UNVERIFIED_SDK=1` is the deliberate way past that one
case; it does not let through an SDK that reported a version below the floor.

Setup shows the version too — `rexglue.exe (v0.10.0)` — and an SDK below the
floor is listed as **not found**, so the GUI blocks Recompile the same way it does
for a missing toolchain. Finding out there beats finding out after the pipeline
has already rewritten a game folder.

### Fixed: the v2.35.0 executable rejected its own SDK

`rexauto.exe` was built seven minutes before `SDK_PIN` was updated and shipped
with the old hashes, so downloading the release, installing the bundled SDK and
building gave `SDK MISMATCH — refusing to run`. The asset was rebuilt and
replaced. Verified the way it should have been the first time: both assets
downloaded from the release into a clean directory, `rexglue --version` → 0.10.0,
full build OK, 99.2073% of code bytes and 60,146 functions — the same numbers as
the local tree.

## 2.35.0 — "a patch nobody can reach is not a patch" (2026-09-04)

**The community has published byte patches for these games for years, and a
static recompiler could not use a single one of them.** This release makes that
catalogue reachable, and gives every generated port a launcher. Ships a new
**ReXGlue SDK** (v0.10.0 + four fixes, all sent upstream).

### Community game patches, compiled in

`xenia-canary/game-patches` holds thousands of entries keyed by title ID —
framerate unlocks, aspect-ratio fixes, engine bug fixes — as lists of byte
writes at guest addresses. An emulator pokes those into guest memory at load.
**We cannot.** rexauto is a static recompiler: a guest instruction is translated
once, ahead of time, and the native code never reads guest memory for it again.
A code patch only means anything if the image carries it *before* codegen.

So it lands there. The SDK gained an `[[image_patch]]` block applied to the
decoded image right after load, and rexauto converts the community entries into
it. Gears of War Judgment's "Unlock FPS" comes out as a `nop` where the game had
`lfs f1,1228(r3)` — its 30 fps cap is gone, and the recompilation is unchanged
(99.2073% of code bytes, 60,146 functions, 0 holes, byte-for-byte the same
numbers as the unpatched build).

Each patch carries a **seal**, because the distinction is not cosmetic:

- `RECOMPILAR` — at least one write lands in `.text`. It becomes native code and
  needs a rebuild. There is no instant toggle for it, and promising one would be
  a lie.
- `RUNTIME` — every write lands outside `.text`.

Choose them in the GUI (a **PATCHES** panel with the seal on each row), or with
`--list-patches`, `--patch "Unlock FPS"`, `--no-patches`.

### The guard, because the wrong dump is a silent miscompile

Two builds of the same title put the same patch at different addresses — on
Judgment "Unlock FPS" is `0x8255DE08` on the base game and `0x8255E220` on TU4.
Applying the wrong one does not fail; it overwrites an unrelated instruction and
the symptom shows up somewhere else entirely.

Every generated block records `expect`: the bytes the image held when the patch
was converted. A mismatch is refused with a diagnostic instead of applied.
Verified by corrupting an `expect` on purpose — the patch is skipped, the
original instruction survives in the generated code, and the log says why.
`do_codegen` now surfaces that refusal, which it previously captured and dropped
on success.

### Three bugs in the module that was supposed to do this

`gamepatches.py` existed and could never have run:

- It imported `tomllib` (Python 3.11+). rexauto is frozen from **3.10**.
- It read the code ranges from `<name>_init.h` only. v0.10.0 puts them in
  `<name>_pch.h`, so it gave up with "nenhum modulo utilizavel" on every port
  built by the current SDK — the same one-filename assumption that 2.34.0 fixed
  everywhere else.
- It silently dropped `f32` writes. The Judgment file has four. A patch mixing
  `be32` and `f32` would have been applied **half way** — worse than not at all.
  Unsupported write kinds now refuse the whole patch.

The module is also no longer a standalone script: `catalog()` / `apply()` /
`clear()` back the CLI, the GUI and the build stage from one code path.

### A launcher for every port

The port executable has no options screen, and resolution, monitor, fullscreen
and frame cap are runtime cvars that can only be chosen through `REX_*`
environment variables *before* the process starts. Until now the only way to
pick them was editing a `.cmd` by hand.

Every successful build now writes `Launcher <name>.cmd` + `launcher.ps1` beside
the exe — WinForms through PowerShell, so it needs nothing installed. It lists
each monitor's native resolution first, then 720p → 4K, and hides anything
outside the guest video mode's real limit (640–4095 per axis, so a 5120-wide
ultrawide is not offered rather than rejected at startup). Choices persist in
`launcher.json`.

**Frame limiting is on by default**, and it drops two cvars together:
`REX_VSYNC=true` *and*
`REX_D3D12_ALLOW_VARIABLE_REFRESH_RATE_AND_TEARING=false`. Vsync alone caps
nothing while tearing is allowed — that is why an uncapped Judgment renders 421
fps on a title screen.

### SDK: v0.10.0 plus four fixes, all upstreamed

- **`xam_content`: dangling guest `string_view` across the deferred completion.**
  `XamContentCreate` captured a view over guest memory into the lambda handed to
  `CompleteOverlappedDeferredEx`; by the time the dispatch thread ran it, the
  title had reused the buffer. The stale bytes reached `string_key_case::hash()`
  → utf8 iteration → `utf8::invalid_utf8` → `[FATAL] Dispatch thread: deferred
  completion threw 'Invalid UTF-8'` and a `0xC0000409` fastfail. Judgment
  reproduced it in 13 s, three runs out of three; with the fix it ran 108 s
  clean. **We fixed this once already, in 2.6.0, in a fork — and never
  upstreamed it, so v0.10.0 shipped it back to us.** That is the whole argument
  for sending patches up.
- **Branch/call targets resolved from the graph** when the per-site `CallTarget`
  table has no entry: 39 sites on Judgment became `REX_FATAL` at runtime despite
  the graph knowing the function. 39 holes → 0.
- **Out-of-range jump-table index dispatches instead of trapping.**
  `__builtin_trap()` lowers to `ud2`; Judgment died in 5 s at one of 121 such
  defaults with `STATUS_ILLEGAL_INSTRUCTION` and nothing in the log. Compiling a
  representative switch both ways at `-O2` gives a byte-identical dispatch
  prologue — same bounds check, same table, same `jmp rax`; the difference is one
  instruction in the cold block.
- **`[[image_patch]]`**, above.

Upstream: [#427](https://github.com/rexglue/rexglue-sdk/pull/427) and the four
branches on `xdzleo/rexglue-sdk`.

### Honest limits

The v0.10.0 Judgment port still dies non-deterministically between roughly 50 s
and 160 s, with a raw fastfail and no `REX_FATAL` — memory corruption without a
diagnostic. It happens with and without the community patches, so it is not
theirs. Unfixed, and named here rather than left for you to find.

`closure.py` still reports "0 targets resolved statically" on v0.10.0: it reads
0.8.2's address-form switch cases (`case 0x822C6964:`) and v0.10.0 emits the
index form (`case 0:`). The coverage number is unaffected; the jump-table
sub-count is wrong.

The SDK's own `resolution` preset cvar (`720p`/`1080p`/`4k`) is **defined and
never read** — a documented option that does nothing. The launcher writes
`video_mode_width`/`height` directly instead.

## 2.34.0 — "a silent pass is worse than a broken one" (2026-09-04)

**Every discovery pass we shipped was dead on ReXGlue v0.10.0, and said nothing.**
`rexauto`-only; **SDK unchanged** (see the note at the end on why the v0.10.0
switch is not in this release).

### The passes were reading one filename

`_gap_fill_register` and `_pointer_scan_register` read `REX_CODE_BASE` /
`REX_CODE_SIZE` from `<name>_init.h`. ReXGlue 0.8.2 puts those defines there;
**v0.10.0 puts them in `<name>_pch.h`**. The regex raised, a bare `except`
swallowed it, and both passes returned nothing — with no message.

The cost was not the passes: it was three rounds of investigation blaming the
newer SDK for functions it was not missing. `_codegen_ranges()` now scans the
emitted headers for whichever file carries them.

### Cure provenance

The gabarito always ships. It carries what no static pass can reach — on Dante's
Inferno **27 of 33** missing functions are addresses *already covered* by another
function, and only a runtime call reveals them as separate entries — and it saves
the launches.

But a change to the static passes is only an improvement if a run **without** it
needs fewer runtime cures. `stage_build` records that split:

```json
"cures": {"pointer_scan": 15, "gap_fill": 1, "total": 636, "gabarito": false}
```

`runtime` is the number to drive down: exactly what the tool still cannot find by
reading the binary.

### Log scroll

The pipeline log forced `scrollTop` to the bottom on every line, and the pipeline
emits several a second — scrolling up to read anything was undone immediately. It
now follows the tail only while the reader is at the tail. Same for the Setup log.

### Gabaritos

Gears of War Judgment re-published at **728 cures**; Dante's Inferno added at
**639** — 100% of recompilable code, 0 holes, 36,504 functions. Both are fetched
automatically by key on a fresh run.

### Why v0.10.0 is not shipped here

It was built (upstream v0.10.0 + our `dump-image` port + a fix for a regression
in its conditional-branch lowering) and it does match: **60,146 functions vs our
60,137 on Judgment, 0 holes both**. Two things block shipping it:

- **v0.10.0 moved the GPU into a plugin.** Without `rexgpu-xenos.dll` beside the
  exe *and* `--gpu_plugin=xenos`, the runtime logs `no GPU emulation loaded
  (gpu_plugin not set); call ignored` and the port renders nothing. rexauto does
  neither yet.
- Switching re-pins `SDK_PIN` and invalidates every title's byte-identical
  baseline at once.

## 2.33.0 — "don't start what can't finish" (2026-09-03)

**A run with missing tools now refuses to start, and a run that will silently
produce a worse port says so up front.** `rexauto`-only; **SDK unchanged**.

### Preflight

Before this, a machine with no toolchain got through extract, xctd, init and
setjmp — minutes of work, and on a title with transparent compression a
**rewritten game folder** — only to die at the build. Two tiers now run before
anything is written, in the CLI and behind the GUI's Start button:

- **Blocking** — `rexglue.exe`, the SDK headers/libs, clang, clang++, the MSVC
  linker. Nothing can be produced without them, so the run stops and names each
  one with where to get it. Nothing is written.
- **Degrading** — Python and IDA. The port still builds, but jump-table recovery
  is skipped and the title loses static `bctr` recovery entirely. That used to be
  one skipped line in a log; it is now a warning at the point where it can still
  be fixed.

`Setup` installs the blocking three (SDK one-click, LLVM and Build Tools via
winget) and Python; **Install all** does the lot. IDA is commercial and stays
status-only.

### What the silence was costing

Auditing the fleet after 2.32.3 fixed Python detection:

| port | jump tables |
|---|---|
| gears_of_war_judgment | 109 |
| dante_s_inferno | **0** |
| bully | **0** |
| spider_man_dimensions | **0** |

Three of four had been built with **no** static jump-table recovery, each
recorded only as `{"skipped": "extract-funcs-fail"}`. Running the pass on Dante's
Inferno for the first time: **220 tables, 5,999 targets**.

That gain does not move byte coverage and is not claimed to — it moves indirect
targets from "resolved when the guest happens to get there" to "resolved at
codegen". It is the difference the run-heal cannot make up, and it is why
Judgment, which had its tables from the start, survived a 150 s launch while
Dante did not.

### Also

`Setup` no longer calls Python **optional**. It runs the jump-table scripts, and
the row now says what is lost without it and that a Windows Store alias does not
count.

## 2.32.3 — "the Python that was not a Python" (2026-09-03)

**Every title on a machine with the Windows Store Python alias has been silently
losing static jump-table recovery.** `rexauto`-only; **SDK unchanged**.

Bully - Scholarship Edition logged:

```
extract_funcs failed -> skipping jump tables
```

`extract_funcs.py` was fine — run by hand against the same port it extracts
**41,371 functions** and exits 0. What was not fine was the interpreter running
it. `shutil.which("python")` returns

```
%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe
  -> AppInstallerPythonRedirector.exe
```

which is not an interpreter at all. It is the Store's App-execution alias: it
prints *"Python was not found…"* and exits **9009**. It sits on PATH ahead of a
real install, so `detect_env()` picked it, the Setup panel reported **Python:
found**, and the jumptables stage failed on every title with a message that named
nothing.

This is not a Bully problem. It costs **every title** its static `bctr` recovery —
the pass that on Gears of War Judgment found 109 tables and 3,142 case targets.
Dante's Inferno had the same `extract-funcs-fail` recorded in its state and it was
attributed to something else.

**Every Python candidate is now run before it is accepted** (`python -c "import
sys; print(sys.version_info[0])"`, result cached). The stub is rejected and the
frozen build resolves `C:\Program Files\Python310\python.exe`.

**The message now says why**: exit code, which interpreter was used, the last line
of its output, and that static recovery is lost for that title. The silence is
what hid this.

## 2.32.2 — "the xctd decoder was never in the build" (2026-09-03)

**The XCTD stage has never worked in a packaged rexauto.** `rexauto`-only; **SDK
unchanged**.

Bully - Scholarship Edition stopped one stage in:

```
xctd: building decoder (one-time)
clang: error: no such file or directory:
  ...\_MEI000073942\thirdparty\libmspack\lzxd.c
```

The stage compiles `tools/xctd_rip.cpp` against the vendored libmspack to decode
assets a title ships transparently compressed on disc. The PyInstaller line never
bundled `thirdparty/libmspack` or `tools/`, so in a frozen build those sources are
simply not there. It has been broken for as long as there have been releases and
only shows on a title that uses the format — Gears of War Judgment and Dante's
Inferno do not, which is why it never surfaced.

Reading the code to fix that turned up a second defect: the decoder was built
**into** the directory PyInstaller unpacks and deletes on exit. What the code
calls a one-time build would have run on every launch, writing its `.o` files
into a tree that is gone next time. A frozen build now caches under
`%LOCALAPPDATA%\rexauto\xctd`; a source checkout still uses `tools/`.

Missing sources now produce a sentence saying what is wrong instead of leaking a
clang error.

Verified end-to-end through the packaged exe with the decoder cache deleted first,
so the compile ran from scratch: **950.6 MB → 1,996.9 MB** plaintext, originals
preserved under `<work>/<name>/xctd_originals`.

Note the stage rewrites the game folder **in place** (that is its job); the
originals are kept.

## 2.32.1 — "a landing cannot catch a branch from outside" (2026-09-03)

**Fixes a hole 2.32.0's gap fill left behind.** `rexauto`-only; **SDK unchanged**.

On Dante's Inferno the new pass closed 3,116 bytes of uncovered code and left
**one** unresolved-branch trap it could not cure. `0x829085A4` was in
`forced_landings`, the file was written, the manifest included it, and codegen
kept emitting `REX_FATAL("Unresolved branch from 0x829082B0 to 0x829085A4")` on
every round.

A forced landing emits a `loc_` label **inside** the function that owns the
address. That is enough only when the branch comes from inside that same routine
— a branch arriving from a *different* function cannot `goto` into another
function's body in C++, so the trap survives forever. The unresolved-branch heal
now cures an in-span target as a **chunk** (`parent = <owner>`): a real
function-table entry that does not split the owner.

This is the third time the same distinction has bitten: a *called* interior
address (2.30.0), a data pointer landing on a `loc_` (2.31.0), and now a branch
from outside. The rule is one line — **a `loc_` label only serves control
arriving from within the same routine; anything arriving from outside needs an
entry.**

Dante's Inferno, same 287-cure base and the same corrected ruler throughout:

| | coverage | functions | uncovered | holes |
|---|---|---|---|---|
| without gap fill (2.31.0) | 99.8948% | 36,195 | 8,396 B | 0 |
| with gap fill (2.32.0) | 99.9338% | 36,503 | 5,280 B | **1** |
| **with this fix** | **99.9338%** | 36,504 | 5,280 B | **0** |

Gears of War Judgment is unchanged and still clean: **99.9601%**, 60,137
functions, 0 holes. `REXAUTO_NO_GAPFILL=1` disables the pass, which is also how
the A/B above was measured.

## 2.32.0 — "measure it exactly, then close what it finds" (2026-09-03)

**The coverage measurement was wrong twice; both are fixed, and the corrected
measurement now drives a pass that closes the gaps it finds.** `rexauto`-only;
**SDK unchanged**.

### The ruler was wrong, and impossible numbers caught it

- **It summed emitted instructions.** Function extents *overlap* — a chunk
  re-emits part of its parent, a boundary override makes two functions cover the
  same code — so the sum double-counted. Gears of War Judgment measured
  **100.0209%**, a number that cannot exist and that would have shipped as
  success. Coverage is now the **union** of the extents.
- **Each extent ended at `start + 4×instructions`**, which is short whenever
  codegen emits fewer comment lines than the routine spans. Checked against an
  invariant the output itself provides — every `loc_` label must fall inside its
  own function's extent — that was **466 misses out of 236,440**. Extents now
  reach the furthest address the body names (a label, or a stored return
  address), and the same check returns **0 of 236,419**.
- **The denominator charged the port for alignment padding.** Inter-function
  padding is `0x00000000`/`nop` and has no code to recompile: 112,832 bytes on
  Judgment, three quarters of a point. Coverage of *real code* is now reported
  beside the raw-range figure.

The numbers in 2.30.0 and 2.31.0 were produced by the broken ruler and are
overstated. Everything below is measured with the corrected one on both sides.

### gap fill

`covered_ranges()` is now shared between the measurement and a new `stage_build`
pass, so the thing that measures coverage and the thing that closes it cannot
disagree about what "covered" means. Every stretch of the code range that carries
instructions and has no emitted C++ behind it is registered as a function start;
codegen re-runs, and the pass loops because closing one gap exposes the next.
Anything codegen then declines to define is dropped.

Two kinds of gap are skipped because there is nothing there to recompile:
alignment padding, and the import thunk table (two data words then
`mtctr`/`bctr`, resolved by the runtime, not recompiled).

| Gears of War Judgment | coverage of real code | functions | uncovered |
|---|---|---|---|
| v2.31.0 | 99.9424% | 59,761 | 8,464 B |
| **now** | **99.9601%** | **60,137** | 5,864 B |

Dante's Inferno gains the same way with no manual step — the pass ran four rounds
by itself: **36,195 → 36,503 functions, 99.9338%** of real code. Both titles hold
static closure at 100.0000% with zero holes.

100% is not the target and never was: the raw range is ~0.8% padding. The target
is *no gap that contains instructions*, and what remains on Judgment is 5,864
bytes, over half of it one 2,996-byte region at `0x8297F8E4` that control flow
never reaches.

## 2.31.0 — "a port shouldn't die on a function nobody found" (2026-09-03)

**Ports ship tolerant of undiscovered functions, the pointer scan stops throwing
most of its findings away, and two SDK miscompilations are repaired in the emitted
C++.** `rexauto`-only; **SDK unchanged** (`SDK_PIN` identical to 2.27.0).

Everything here came from building
[hells-gate-recomp](https://github.com/florinp93/hells-gate-recomp)'s patched
ReXGlue **v0.10.0** and running it against *our own* Dante's Inferno — same game,
same pipeline, one variable.

### The difference between "dies at 20 s" and "reaches gameplay" is three lines

An indirect call to an address the static scan never found is `REX_FATAL`: the
process dies, and the run-heal has to find every such address by launching and
crashing on it. Their patch replaces that with a log-and-return, and says why:
*"many indirect call targets are runtime-computed vtable entries that static
analysis cannot discover."*

Our 0.8.2 runtime **already has that behaviour**, behind `REX_HEAL_DISCOVER`, used
only while healing. Same exe, same build of Dante's Inferno:

| dispatcher | result |
|---|---|
| strict (the shipped default) | dies at 20 s on `0x82908134` |
| tolerant | **120 s, zero fatals** |

The build now writes `play <name>.cmd` beside the exe, which sets it. The `.exe`
keeps the strict default, because the heal needs a launch that stops at the first
missing function in order to find it. The two modes exist for opposite reasons and
until now only the healer's one was ever shipped. `REXAUTO_NO_PLAY_LAUNCHER=1`
skips writing it.

### The pointer scan was discarding most of what it found

v0.10.0 discovers **51 functions on Dante's Inferno that we did not have**, and 40
of them are plain pointers in the image data. Ours found those and dropped them,
because the target was already emitted as a `loc_` label inside another function
and registering it as a function would split the owner.

Neither ignoring nor registering was right: they are cured as **chunks**.
`label_owners()` reads which `DEFINE_REX_FUNC` precedes each `loc_`, and the scan
returns those hits with their owner, so each becomes `{ parent = <owner> }` — a
real function-table entry that leaves the owner whole. Same lesson as 2.30.0's
call-vs-branch fix, applied to discovery instead of healing.

| Dante's Inferno | by code bytes | functions |
|---|---|---|
| before | 99.6947% | 36,155 |
| importing their 51 by hand | 99.7160% | 36,204 |
| **finding them ourselves** | **99.7279%** | 36,195 |

Gears of War Judgment, which already had zero static holes, gains the same way:
**99.6582% → 99.6820%**, 59,726 → 59,761 functions.

### Two SDK miscompilations, repaired in the emitted C++

- **`vpkuwus`/`vpkuhus` in-place aliasing.** The pack writes the destination's
  narrow element array while still reading a source's wide one; same 128-bit
  storage, so `vpkuwus128 v63,v61,v63` corrupts its own next read. Sources are
  snapshotted first. Confirmed independently: v0.10.0 fixes it with intrinsics
  that load both sources before the single store.
- **`lvebx`/`lvehx`/`lvewx` are NOT a bug.** They are lowered as a full 16-byte
  `lvx`, which looks wrong and is not: v0.10.0 ships element-load builders and
  deliberately leaves them undispatched — *"the Xbox 360 Xenon CPU does not
  implement the standard AltiVec element-load semantics for these instructions."*
  An earlier attempt to "fix" this was a regression, caught by running their
  codegen over the same title, and is reverted. `REXAUTO_NO_VPACK_FIX=1` disables
  the repair that remains.

### Also

- `xenon-jumptables` ships inside rexauto (`vendor/`), `--add-data`'d into the exe.
  It used to need a hand clone, so `jumptables` recorded `{"skipped": "no-repo"}`
  and the whole indirect-jump class fell to the play-and-heal loop. Judgment: 109
  tables, 3,142 case targets, 19,194 functions IDA found that the scan had missed.
- `closure.py` reports **byte coverage** as the headline, the way decomp.dev and
  frogress do (matched bytes over total code bytes), because counting functions
  flatters: the easy ones are small and the hard ones are big.
- Four of our own bugs, all found by running the pipeline on a second title: a
  called interior address cured as a forced landing (a `loc_` label can never
  satisfy a call, so the heal looped forever on Dante `0x829083F0`); a landing
  emitted once per parent, whose duplicate TOML key made rexglue reject the entire
  config; a forced-landings include declared for a file that was never written;
  and Explorer `search-ms:` paths reaching `open()`.

## 2.30.0 — "the pointers were in the file all along" (2026-09-03)

**The recompiler now finds the function pointers sitting in the game's data, and
coverage is reported the way decomp projects report it.** `rexauto`-only; **SDK
unchanged** (`SDK_PIN` identical to 2.27.0).

### Recompilation, measured by code bytes

`closure.py`'s headline is now **byte coverage** — the share of the image's code
range with emitted C++ behind it. This is the convention the decomp community
settled on (decomp.dev / frogress report matched bytes over total code bytes and
call it the honest metric) and the reason carries over unchanged: counting
*functions* flatters, because the easy ones are small and the hard ones are big.

It is measured, not estimated: codegen writes one `// <mnemonic ...>` comment per
translated PowerPC instruction and every instruction is 4 bytes, so 4× that count
is exactly the code it translated. Denominator is `REX_CODE_SIZE` from the
generated header.

### Function pointers, read out of the image instead of found by crashing

A vtable entry, a callback array or a handler table is a dword in a **data**
section holding a code address. The recompiler's scan follows control flow, so it
never sees them, and the run-heal then discovers them one launch-and-crash at a
time. On Gears of War: Judgment **35 of its 46 cures were sitting in the image in
plain sight**.

The idea is [hells-gate-recomp](https://github.com/florinp93/hells-gate-recomp)'s
`dataSectionFunctionPointerScan`, which patches ReXGlue itself. We run on a
prebuilt 0.8.2 SDK and their patch targets 0.10.0, so the technique is
reimplemented on the rexauto side: scan the image, register into `functions.toml`
before the first build. Same filters — read only outside the code range (scanning
code too is what makes it noisy: 1,878 spurious candidates whole-image against
315 data-only), 4-byte aligned, non-null, target must land in code, target's own
first instruction must not be padding.

Two guards are ours, and both were earned:

- **Drop anything already emitted as a `loc_` label.** Inside a function body that
  address is a landing; registering it as a function splits the routine — the
  failure that made Judgment die 0.7 s into every launch.
- **Keep only what codegen actually defined.** A pointer can land where codegen
  declines to translate; the import thunk table is full of 16-byte stubs that look
  exactly like code pointers, and the first run of this produced 281 registrations
  and a link full of `undefined symbol: sub_8309B8C4`. ReXGlue's version skips
  that range with `importThunkTableStart()`; from outside the SDK there is no such
  accessor, so the emitted output is checked instead and the strays are dropped.
  15 of 281 on Judgment, 15 on Dante's Inferno.

The scan runs **before** the unresolved-branch cure, not after: registering a
function can expose a new unresolved branch, and the trap loop is what closes
those. Reversed, Judgment came out with 17 fresh holes and static closure fell off
100% — a real regression, caught and fixed before shipping.

### Measured

| | before | after |
|---|---|---|
| Gears of War: Judgment | 99.4313% | **99.6582%** |
| Dante's Inferno | 99.2619% | **99.5582%** |

Judgment: static closure stays 100.0000% (0 holes), functions 59,453 → 59,726, and
the port **survived a 150 s launch with zero fatals** — against a 0.7 s crash
before 2.28.1. Dante's Inferno: 0 holes, 35,927 → 36,134 functions; it still stops
on the ordinary unregistered-function class it was already stopping on at 12–20 s
before this change, so the run-heal still has work there.

`REXAUTO_NO_PTRSCAN=1` disables the scan — the regression gate compares against
baselines that predate it, and this deliberately changes generated code.

## 2.29.0 — "the pass nobody ran" (2026-09-03)

**xenon-jumptables ships inside rexauto, and coverage is measured instead of
asserted.** `rexauto`-only; **SDK unchanged** (`SDK_PIN` identical to 2.27.0).

### The static pass was there all along and almost nobody ran it

`detect_env()` only found xenon-jumptables as a hand-cloned checkout, so a normal
install recorded

```
jumptables   {"skipped": "no-repo"}
```

and the **entire indirect-jump class** fell through to the play-and-heal loop —
which finds those targets by launching the game and crashing on them, one launch
at a time. IDA installed, pipeline stage present, recovery never run.

It is now vendored (`vendor/xenon-jumptables`, BSD-3, 18 files, 133 KB) and
`--add-data`'d into the frozen exe. `detect_env()` resolves `JT_REPO`, then a copy
beside the app, then the bundled one under `sys._MEIPASS`, then a root checkout.
The Setup panel lists it as **bundled** — status only, nothing to install.

On Gears of War: Judgment, 165 s of analysis:

| recovered | |
|---|---|
| jump tables | 109 |
| case targets | 3,142 |
| functions IDA found that the recompiler's scan missed | 19,194 |

The generated C++ dispatches them as real `switch (ctx.ctr.u32)` blocks instead of
falling through to the runtime dispatcher.

### Coverage, measured from the emitted C++

`closure.py` reports two numbers, because a port has two unrelated kinds of
incompleteness and one percentage hides that:

- **static closure** — holes over static targets. A hole is not inferred: codegen
  bakes `REX_FATAL("Unresolved call/branch from …")` into the `.cpp` at every
  target it could not resolve, so the numerator is the recompiler's own admission.
- **indirect surface** — `REX_CALL_INDIRECT_FUNC` sites, which bind only when the
  guest runs, reported with the share now resolved statically by recovered tables.
  Never folded into a percentage: its true denominator needs every code path in
  the game executed, which is exactly why the run-heal exists.

Judgment: `static 100.0000% (0 holes / 499,631 targets), 59,446 functions, 45,178
indirect sites of which 1,871 targets resolved by 115 jump tables`.

`stage_build` logs it and stores it in the checkpoint; `publish_gabarito` carries
it into the gabarito `[meta]`, so a consumer sees the coverage without rebuilding.

Nothing here reuses `metrics/CLOSURE.md`: that came from `closure_cert`, which this
project's own audit records as unable to run on any port, with a coverage predicate
reducing to `a >= starts[0]` — a zero-width hole window on 27 of 29 titles, so its
"ZERO static holes" was forced, not earned.

### Judgment's gabarito is reachable again

`gabaritos/83190f99….toml` — 45 cures under the key its `default.xex` actually
hashes to. The old `34fa4496` entry is keyed to a different dump, so
`fetch_gabarito()` 404'd and the title always healed from scratch; it carries 14
cures against this run's 45, consistent with having been published from one of the
false "converged" verdicts fixed in 2.28.1. Both are kept and indexed.

The port now runs **143–343 s per launch with zero fatals**, against a 0.7 s crash
before 2.28.1.

## 2.28.1 — "the cure that never ran" (2026-09-03)

**The unresolved-branch heal was dead code, and it never needed a launch anyway.**
`rexauto`-only; **SDK unchanged** (`rexglue-sdk-win64.zip` and `SDK_PIN` identical
to 2.27.0).

### The crash

A fresh Gears of War: Judgment port built clean and then died 0.7 s into **every**
launch:

```
[FATAL] Unresolved branch from 0x830B0F48 to 0x830AFE58
```

Four run-heal iterations, four identical logs, and each one ended with

```
run-heal converged in 3 launch(es): exited without an invalid-function fatal
(other stop - likely GPU/runtime)
```

A false pass: the loop cured nothing, changed nothing between launches, and
reported convergence on a title that never reached its title screen.

### Defect 1 — the wrong word

`rexglue` emits **both** wordings, `Unresolved call` for a `bl` and `Unresolved
branch` for a `b`/`bc`. `unresolved_branches_from_runtime()` matched only `call`.
Against Judgment's own log the old pattern finds **0** targets; the fixed one finds
`0x830AFE58`. Since the loop otherwise heals only the invalid-function class, the
entire branch class was uncurable — `crash_mind_over_mutant`'s four identical runs
are the same signature.

With the pattern fixed the loop registers `0x830AFE58` on its first iteration and
Judgment goes from a 0.7 s crash to **107 s launches** discovering ordinary
indirect-call functions.

### Defect 2 — it was never a runtime question

The trap is a literal `REX_FATAL("Unresolved branch from 0x%08X to 0x%08X")` that
**codegen bakes into the .cpp**. `rexruntime.dll` carries no such string; it only
executes what was written. So the whole set is knowable from `generated/` the moment
codegen finishes.

`unresolved_branches_from_generated()` reads it there, and `stage_build` cures every
trap and re-runs codegen **before the first build** — instead of paying one
build + launch + crash per trap, and only ever finding the first one the guest
happens to reach. Traps that a cure exposes are picked up by the same loop.

The scanner returns both classes from a generated tree, `[]` for a missing directory,
and `[]` for an already-cured port.

### Also

`gabaritos/README.md` is regenerated from the files it indexes. It had dropped four
entries (`gears_of_war_judgment`, `gears_of_war_3`, `budokai3`,
`wwe_smackdown_vs_raw_2007`), listed `skate3` at 32 cures when the file holds 1789,
and listed `captain_america_super_soldier` as "game" with 1 cure instead of 218.

### Known, named, not fixed

**The Judgment gabarito is unreachable.** `gabaritos/34fa4496….toml` is keyed to a
`default.xex` whose sha256 is not the one the game ships:
`83190f99…`. `fetch_gabarito()` therefore 404s and the title always heals from
scratch. The cure set inside it is right for this binary — its boundary extension
`0x830AFE28 → 0x830B0F80` is exactly what resolves the fatal above — it simply can
never be fetched. Re-keying it needs a converged run to publish, which is not in
this release.

## 2.28.0 — "forty-four fragments" (2026-09-01)

**Games on Demand containers work, multi-part included.** `rexauto`-only; **SDK
unchanged** (`rexglue-sdk-win64.zip` and `SDK_PIN` identical to 2.27.0).

### The wall

Every retail-sized GoD is multi-part — `<TITLEID>/00007000/<hash>` plus
`<hash>.data/Data0000..DataNNNN`, 0xA290000 bytes each — and `extract.py` refused
all of them: *"multi-part GoD (44 data files) is not handled — convert to ISO"*.
Only the single-file SVOD layout (a repack curiosity) was readable, so in practice
"GoD support" meant "convert it to ISO first". Gears of War: Judgment, 44 fragments,
7.3 GB, was the test case.

### The fix (`extract.py`)

- **`_svod_reader` replicates xenia's `BlockToOffsetSVOD` / `ReadSVOD` in full.** A
  GDFX sector maps to *(fragment index, offset)*: 0x14388 data blocks per fragment,
  a 0x1000 Level-0 hash table before every 0x198 blocks, a Level-1 table before every
  0xA1C4 L0 tables, the end-of-fragment wrap into `+0x2000` of the next file. The
  fragments are opened from `<header>.data/` sorted by name and their count checked
  against the header's `data_file_count` (0x39D) — a partial copy fails up front,
  not as a corrupt asset later.
- **Layout detection follows xenia exactly.** EGDF (header flag → magic @0x2000),
  XSF (magic @0x12000, `XSF` stub @0x2000 — what every third-party converter emits),
  single-file (@0xD000). The old reader added `svod_base_offset` for every layout;
  xenia only adds it for single-file. That was masked before because the single-file
  path was the only one that ran.
- **The GDFX volume descriptor is read at the magic offset**, not via "sector 32":
  under EGDF those are different places. `_gdfx_extract` takes the reader's `vd`
  when it has one.
- **Contiguous runs are coalesced.** Blocks between two hash tables are physically
  contiguous, so `read_sector` reads up to 0x198 of them per syscall instead of one.
  The 7.3 GB title extracts in about ten seconds from a warm cache.
- **Point it at any folder above the header.** `find_god_header()` mirrors xenia's
  `ResolveFromFolder`: walks the tree (depth ≤ 4) for a CON/LIVE/PIRS package with
  SVOD volume type, preferring the one with its `.data` folder beside it. So the
  container box accepts `…\Gears of War Judgment`, `…\4D530A26`, or the header file.
- **Package meta comes from the LIVE header.** Title (*Gears of War: Judgment*),
  title ID, and cover art now show for GoD folders, and `read_package_meta` reports
  `format` (`GOD` / `STFS` / `ISO` / `XEX` / `FOLDER`), which the GUI chip displays
  instead of guessing from the extension.

### Verified

- **Every data block, against the container's own hashes.** The SVOD Level-0 tables
  hold the SHA-1 of each 0x1000 block pair. Reading the whole title back through the
  new reader and hashing: **1,797,408 pairs OK, 0 bad** — the 44-fragment mapping,
  including every fragment boundary and the wrap rule, is byte-exact.
- GDFX directory: 1,938 files, all present at the listed size; 34 files straddle a
  fragment boundary. `default.xex` parses as XEX2 (18 optional headers); all 1,841
  cooked UE3 packages open with the big-endian package tag.
- `rexauto.py <GoD folder> --only extract` runs the stage end-to-end.

### Also

- **Explorer `search-ms:` paths are resolved.** Opening a folder from a search-results
  view and copying the address bar yields `search-ms:displayname=…&crumb=location:…`;
  that used to reach `open()` and die with `OSError 22`. `normalize_container()` decodes
  the crumb, finds the named folder under it and uses the real path. A container that
  does not exist is now a one-line error, not a traceback.

## 2.27.1 — "the button that did nothing" (2026-09-01)

**Setup → Install ReXGlue SDK now actually installs it.** `rexauto`-only; **SDK
unchanged** (`rexglue-sdk-win64.zip` and `SDK_PIN` identical to 2.27.0).

### The bug

The one-click SDK install downloaded the bundle every time and then reported
"ReXGlue SDK installed" — and the Setup row kept saying **not found**. The download
was never the problem. `rexglue-sdk-win64.zip` is a plain install tree (`bin/`
`include/` `lib/` `share/` at its root), and `install_rexglue()` just `extractall()`'d
it next to the app. `detect_env()` looks for `rexglue\tool\rexglue.exe` and
`rexglue\sdk`, so nothing was ever found, while `<app>\bin`, `<app>\include`, … piled
up beside `rexauto.exe` on every click.

### The fix (`gui/setup.py`)

- The archive is laid out as `rexglue\sdk` (the CMake prefix the build stage passes as
  `CMAKE_PREFIX_PATH`) with `rexglue.exe` / `rexruntime.dll` / `TracyClient.dll`
  mirrored into `rexglue\tool`. A bundle already wrapped in `rexglue/` is still accepted.
- After extraction the installer re-runs `detect_env()` and only reports success if the
  SDK is actually visible. Before, success was unconditional.
- A truncated download, or a non-zip response (a GitHub error page), is reported as an
  error instead of raising inside `ZipFile`. Requests carry a User-Agent; timeout 60 s.

If you clicked the old button, the stray `bin/ cmake/ include/ lib/ licenses/ share/`
folders next to `rexauto.exe` can be deleted.

## 2.27.0 — "the copy nobody needed" (2026-08-20)

**Codegen is 20.7x faster and its output is byte-identical.** One line did it, and the
line was not slow because of what it computed — it was slow because it computed
something and threw it away, 65,000 times per title.

### The line

`phase_discover.cpp` opened each discovered function with:

```cpp
auto effectiveKnownFunctions = knownFunctions;
```

A deep copy of an `unordered_set` holding roughly 65,000 addresses — taken **once per
discovered function**, and discovery runs about 65,000 times on a large title. Order
n² node allocations against a per-call budget of ~1.9 ms.

The copy existed only so two `erase()` loops below it could subtract configured chunks.
Those loops read `chunksByParent`, which is populated exclusively from `functions.toml`
entries carrying `parent`. **The entire 30-title fleet contains exactly one such
entry** (`dragon_ball_z_burst_limit`). `forza_horizon` has none. So in almost every
call both loops iterated an empty map and the copy was discarded bit-identical to its
source.

It is now an overlay: build the excluded set — nearly always empty — and materialise a
copy only when something must actually be removed. The set is consulted downstream
solely through `contains()`, so the two formulations are equivalent by construction.
No threading, no ordering change, nothing to get subtly wrong.

### Measured, not estimated

Same manifest, same machine, the v2.26 pinned binary against this one:

| title | functions | before | after | |
|---|---:|---:|---:|---:|
| joust | 7,045 | 2.0s | 1.0s | 2.1x |
| ms_pac_man | 8,656 | 2.6s | 1.0s | 2.7x |
| budokai3 | 11,687 | 3.9s | 1.0s | 4.0x |
| halo_3 | 22,349 | 13.6s | 2.5s | 5.4x |
| gears_of_war_3 | 56,880 | 105.6s | 5.9s | 18.0x |
| gears_of_war_judgment | 59,681 | 110.5s | 6.4s | 17.2x |
| forza_horizon | 76,936 | 188.8s | 6.0s | 31.7x |
| grand_theft_auto_v | 94,334 | 234.4s | 8.2s | 28.6x |
| **total** | | **661.4s** | **31.9s** | **20.7x** |

Eight titles, chosen to span the fleet's size range; the other 22 were not timed.

The shape is the evidence. Dividing each *before* by the square of its function count
gives the same constant to within 2.6–4.0 × 10⁻⁸ across all eight — the signature of an
O(n²) defect, arrived at from the stopwatch without looking at the code. Divide each
*after* by function count, minus a ~0.7s process floor, and it is flat: 0.7–0.96 × 10⁻⁴
seconds per function. That is the linear work that was always there.

It also explains why Joust "only" gained 2.1x. Joust never had the problem. The fix
pays out where n is large, which is exactly where it hurt: `grand_theft_auto_v` cost
four minutes per codegen iteration, which in practice meant nobody iterated on it.

### Byte-identical, proven three ways

- The 30-title gate reports **28 PASS** and the two known intentional diffs.
- Codegen of `forza_horizon` and `gears_of_war_judgment` under the old and new
  compilers differs in **zero files** — so neither title's diff is attributable to this
  change.
- The whole-fleet gate now completes in **26 seconds**.

### The gate found the wrong compiler again

Preparing this release, a fleet run reported **19 REGRESSIONs** — the same count, and
the same cause, as the v2.26 investigation. `find_rexglue()` listed
`out/install/win-amd64` and not `out/install/win-amd64-pin`; those are different trees,
the first being whatever the box last built for itself. With the pin check switched off
by hand, the log's only record of which compiler produced the verdict was a path, and a
path is not an identity.

Two changes. The release build is now first in the candidate list. And when
`REXAUTO_SKIP_SDK_CHECK` is set, the gate prints the compiler's **sha256**, so a verdict
is attributable to a binary rather than to a directory name. The remedy the gate printed
on that bad run was `--bless`; taking it would have frozen a stale compiler's output as
the fleet's truth. That is the second time, and the reason the check exists.

### From Xenia Canary, validated in gameplay

Adopted by content — our `src/graphics` derives from `src/xenia/gpu` but is
restructured, so these are ports, not cherry-picks:

- **Empty resolve regions** now return true with a zero extent instead of failing.
  Eliminated 20,256 failed draws per run.
- **7-bit float mantissa** decoded with `<< 16`, not `<< 3`. The 3 was copied from the
  20e4 decoder, where it is correct; a 7-bit mantissa needs 23−7.
- **D3D12 scaled-resolve texture dimensions** now scale with the resolve factor. Vulkan
  already did this; D3D12 did not, and the result was device removal — live by default.
- **`XamMediaVerification`** implemented, killing a 600 ms spin in Gears of War
  Judgment (281 calls → 1).
- **`BaseHeap::Reset()`** recomputes `unreserved_page_count_`. Without it, a heap reset
  left the counter permanently stale and the guest was told it had less free physical
  memory than it did.
- **Three `PhysicalHeap` parent-reservation leaks** released instead of TODO'd.
- **`draw_resolution_scale`** defaults to 1, not 2.
- **Perf frame counters** wired up: `ResetFrameCounters()` and `WriteCsvFrame()` were
  fully implemented and called by nobody, and `PROFILE_FRAME_TIME_US` was never invoked
  at all — anything read from that column was leftover array contents.

### Community game patches

`gamepatches.py` matches the xenia-canary game-patches catalogue against a port. The
distinction it enforces is the one that matters for a *static* recompiler: a write
landing in `.text` becomes native code and cannot be toggled at runtime. Each patch
carries the worse of its writes' seals — **RECOMPILAR** or **RUNTIME** — rather than
promising a switch that cannot exist.

Gears of War Judgment ships with **Unlock FPS** and **Resolution Scaling Fix** applied.

### Known, named, not fixed

- **The thunk pool leaks.** `GetProcAddressByOrdinal` calls `AllocateThunk` on every
  invocation with no memoization and no reclamation, so a guest resolving the same
  export twice burns two 4-byte slots. `kThunkReserveSize` was raised 64KB → 1MB, which
  moves the wall from 16,384 calls to 262,144 and fixes nothing. The real fix is a cache
  keyed on (module, target) — which is also what hardware does — but that changes
  dispatcher behaviour fleet-wide and is not runtime-validated, so it is not here. An
  earlier version of that constant's comment cited a "Thunk address space exhausted"
  log line as evidence; **no such line exists in any fleet log, and the claim is
  withdrawn.**
- **`frame_time_us` does not yet validate.** It is measured now, but summed frame times
  come to 2.56x wall clock — almost certainly multi-thread contention on the static
  `last_frame_close`. Treat the column as indicative, not authoritative.

## 2.26.0 — "the law was off" (2026-08-19)

**The regression gate had been enforcing nothing, and nobody could rebuild the SDK it
was supposed to enforce against.** This release fixes both, and the discovery order
matters: the second bug was found *by* fixing the first.

### The gate could not find the fleet, or its compiler

`AUTOPORTS` was hardcoded to `C:\Skate3Recomp\autoports`. The fleet moved,
`projects()` returned `[]`, and `main()` exited *"no matching projects"* — a line that
reads like a mistyped filter, not like *the law is off*. `find_rexglue()` carried the
same dead root, so repairing only the fleet path would still have died one line later.
Both now honour `REXAUTO_WORK`, the env `rexauto.py --work` already reads, so the two
cannot drift apart again; the old roots stay last for a machine still laid out that way.
An absent root now says **DISARMED**.

### The gate never checked which compiler it ran

`regression_gate.py` contained **zero** references to `SDK_PIN`. Its first live run in
weeks gated all 30 titles against an SDK **93 commits behind** the pin — a tree checked
out on a different branch that nobody switched back — reported **19 REGRESSIONs**, and
printed `--bless` as the remedy. Blessing would have frozen a stale compiler's output as
the fleet's truth, permanently. With the pinned SDK the same run is **28/30
byte-identical**: not one title had regressed.

`verify_pin()` now runs before a single title is codegen'd, and the log names the full
path of the binary that produced the verdict. `REXAUTO_SKIP_SDK_CHECK=1` still allows
gating a deliberate SDK change — explicitly.

### The pinned SDK could not be rebuilt, and never matched its own commit

The shipped `rexglue.exe` already carried the `register_cpp.inja` whitespace trim, which
is a **later** commit than the `981cab8` the pin named — so building the named commit
produced a different binary and the pin was unreproducible by construction. Three
independent defects kept it that way:

- `CMakeLists.txt` aborts on MSVC without naming where Clang is expected.
- The preset carries `-march=x86-64-v3`; a raw `cmake` invocation misses it and
  `_mm_shuffle_epi8` fails to compile.
- `INJA_TEMPLATE_FILES` was a hand-kept list that **omitted `register_cpp.inja`**, so
  editing that template did not re-trigger the embed step: you rebuild, the binary keeps
  the old template, and the output is unchanged. Found by experiment — edit, rebuild,
  diff, find it identical. Fixed by adopting upstream `d33efdf` (GLOB_RECURSE +
  CONFIGURE_DEPENDS) by content.

**The SDK now builds from source and its codegen is byte-identical to the old pinned
binary.** `SDK_PIN` is re-pinned to that from-source build.

### Upstream harvest, measured

Two codegen fixes adopted by content from `rexglue/rexglue-sdk` (no shared ancestry):
`10cf1ad` recovers the function tail after a **conditional** `bcctr` — previously any
`bcctr` terminated the function, so everything after a conditional one was dropped — and
`f2b91f2` gives SEH funclets their owner's live non-volatiles. Both are **inert on this
fleet**: adopting them left all 30 titles byte-identical. That verdict was only possible
because the tree builds.

`650a8c3` (`vaddsws`) was **rejected with cause, again**: v2.25 already refused it
because our scalar version also sets `vscr_sat` and theirs does not. It is named here so
a future bulk merge does not silently reintroduce it.

### Static recovery reaches companion modules for the first time

`DEFINE_REX_FUNC` was matched prefix-blind, so every companion module's generated sources
read as **empty**: `func_bodies()` returned `{}`, every deep-extract candidate looked
"swallowed", and the pure-add gate dropped all of them — **46,131 candidates across the
fleet's 10 companion modules, 0 accepted, every time**. Failing closed is why nobody
noticed; it also meant static recovery never contributed one function to Halo 3, FIFA,
Forza Horizon, Sonic or Spider-Man's extra modules. An entrypoint emits no prefix, so
single-module titles are byte-identical by construction.

And deep-extract folded every accepted candidate as a bare `{}` **function head** — but
an address INTERIOR to an emitted function can never be one: registering it asks the
recompiler to split a routine it emitted whole, which it declines by design. So the gate
dropped it and the address was lost. `stage_deepextract` now partitions against the
recompiler's own emitted grid: gap candidates go to the unchanged pure-add gate, interior
candidates go to a landing gate whose contract is that the emitted function SET must be
unchanged and `count_dangling` must be 0, or the whole batch reverts.

### Proof

Full-fleet gate **PASS, 30/30 byte-identical**, pin verified. `func_bodies()` over the
companion modules: **0 -> 139,854**, with the budokai3 entrypoint control unchanged at
11,452. Landings folded with the function set unchanged and 0 dangling: budokai3 +115
(holes 85 -> 84), spider_man/gamelogic +2,526 (holes 2,410 -> 2,364), sonic_adventure +68.

Three baselines re-blessed, each with its cause recorded. `gears_of_war_judgment` is
blessed on the **owner's decision, not on analysis**: its baseline predates the current
pin and its inputs have not changed since 2 Jul, but the previous content is
unrecoverable and the v2.25 jump-table hypothesis does not hold — three of its four
changed files contain no switch case at all.

### Known issues

- One **unexplained transient**: `joust` failed codegen Validate once with 4 unresolved
  calls, then passed twice with nothing changed. Not reproduced; recorded rather than
  dismissed.
- Every port's `CMakeCache.txt`, `_build.bat` and `game_root.txt` carried the dead root
  too. Fixed on this machine's fleet; a port tree from elsewhere needs the same.
- Graphics artefacts under `draw_resolution_scale=2x2` at 1440p are a rendering
  configuration, not a recompilation regression.

## 2.25.0 — "both upstreams, merged" (2026-07-27)

**The fork stops drifting.** rexglue has two living upstreams and we had fallen
behind both: 82 commits behind `mchughalex/rexglue-skate3` (the Skate 3 fork our
SDK descends from) and nothing at all from `rexglue/rexglue-sdk` 0.9.0-dev. This
release harvests both.

### What Skate 3 recomp v2.0 actually had for us

Their headline — "the whole game renders natively, 2x the frame rate at a
quarter of the GPU power" — is a renderer **hand-written for Skate 3**: it hooks
the game's own functions (`Sk8::PresentationEntity::BindConstants`), reads the
scene out of guest memory at reverse-engineered struct offsets, and redraws it
with bespoke HLSL. ~1.2 MB of title-specific C++. That does not generalize and
we did not take it.

What we took is everything underneath it, by **full merge** of
`skate3-sdk-clean@7eb0faf`:

- **Native RHI (nrhi)** — a title-agnostic D3D12 + Vulkan abstraction and the
  native-guest-output renderer hook. **Inert here**: no title registers a
  renderer, so `TryRenderNativeGuestOutput` returns false and the emulated path
  runs exactly as before (verified: no `native`/`nrhi` activity in any launch
  log). It is the door, not the room.
- **Fixes that apply to every title today** — SDL audio credit-pacing
  starvation on large device quanta (the robotic/slowed audio); the timer queue
  blocks instead of yield-spinning; forced-exit watchdog on window close plus
  the process heap lock held across close-time thread suspension (a real
  teardown deadlock: UI thread in `RtlpFreeHeap`, suspended guest thread in
  `RtlpAllocateHeap`); W^X for guest pages; the D3D9 half-pixel offset applied
  in **host** pixels under resolution scaling, which kills resolve-boundary
  seams — we ship 2x2 scaling, so this is live; `GetExecutablePath` via
  `GetModuleFileNameW` instead of `_get_wpgmptr`; NVIDIA
  prefer-max-performance application profile so a load-screen lull cannot park
  the GPU in a low P-state; discrete-GPU preference and a device picker; atomic
  cvar saves with malformed-config recovery.
- Their imgui pin turned out to be a **private, unpublished fork** (it adds
  `ImFontConfig::RasterizerGamma`, which exists in no ocornut branch). The font
  gamma tweak is now compiled in only when the member exists, so the tree builds
  against stock imgui.

### What rexglue 0.9.0-dev had for us

No shared git ancestry — their public history is squashed at "Release v0.8.0",
so `git merge-base` is empty and adoption is by content. Seven picks:

- **DLL `code_base` via `ReXModule_GetImageInfo` (#371)** — companion modules
  now hand the runtime their real image/code layout, so indirect calls into them
  resolve correctly. Verified live: Spider-Man's `gamelogic` module initializes
  its function table at `code=880D0000-886B2F60` with no layout error.
- **Jump-table targets that are known functions stay separate (#370)** — a case
  that used to *call* its landing (`sub_X(ctx, base)`: fresh frame, returns to
  the dispatcher instead of continuing the flow) now does `goto loc_X`, the
  correct lowering of a computed goto. Same bug class as `forced_landings` and
  switch-on-CTR, and the kind of miscompile that never crashes, so run-heal
  never sees it.
- XMA loop wrap emitting garbage and dropping the loop-end frame; ffmpeg buffer
  flush on context release; `XPresenceInitialize`; message-box title/body
  byte-swap; opt-in per-device `FILE_SHARE_DELETE`.

Five upstream commits were **rejected with cause**, not skipped: their inline
XMA decode (we already decode inline, with FPSCR save/restore — theirs would
double-decode), their SIMD `vaddsws` (our scalar version also sets `vscr_sat`,
theirs does not), a `vpkd3d128` revert with no stated rationale, and
"always overwrite generated output" (it kills the hash-skip that keeps heal
rounds incremental). The cooperative `TerminateTitle` drain is deferred — it is
better design than ours, but it rewrites teardown and deserves its own runtime
round.

### Proof

Codegen changed in 18 titles, and the change is an improvement: fleet-wide only
**3 addresses** left a function table (budokai3 x2, crash_mind_over_mutant x1)
and **all 3 became in-function labels** — 0 orphans, 0 dangling labels across
2456 generated files, the rest dead-block removal. Baselines re-blessed, gate
then **30/30 byte-identical**. Runtime: `gears_of_war_3`, `gta_san_andreas`,
`budokai3` and `spider_man_shattered_dimensions` all boot, live 30s, 0 FATAL.
The other 26 titles carry codegen proof only.

## 2.24.0 — "GTA V reaches gameplay" (2026-07-10)

**The milestone: GTA V (545408A7, 2-disc) boots from "insert installation
disc" all the way INTO GAMEPLAY.** v2.23 cured the install-content chain
(enumerator, CD_ROM volume semantics, content mounts, XamSwapDisc completion
event); this release adds the five RAGE boot walls that stood between the
install gate and the game itself — every one a general runtime fix, verified
against the engine's actual expectations:

- **Startup notifications reach every system listener (SDK 80e886c).** RAGE
  registers its own XamNotify listener and waits for the boot notification
  set; delivering only to the first listener left the engine parked forever.
- **XNetGetEthernetLinkStatus reports a live LAN link (a83b685).** The
  network-init gate refused to complete on "no cable"; a dead link parked
  boot before the render loop.
- **XexCheckExecutablePrivilege(11) answers INSECURE (16a4948)** — routes the
  engine's cache traffic to the direct path instead of the privileged one.
- **update: is always mounted — empty when no TU (e063379).** RAGE treats
  device-not-found on update: as fatal; an empty mount is the truthful state.
- **Writable gamecache:/commoncrc: mounts (885018a)** — the engine's scratch
  volumes for streamed/verified data.

Known issue: gameplay freezes intermittently — under investigation (next
release). The install-disc flow generalizes: any 2-disc title stages its
install packages into the user content root and boots the play disc.

SDK_PIN -> 80e886c binaries. Gate: fleet codegen re-verified.

## 2.23.0 — "sibling imports bound" (2026-07-10)

**The Halo 3 "L360" wall, root-caused and fixed structurally (SDK 81ccf82).**
A guest module's imports from a SIBLING guest module (L360.dll <-
WavesLibDLL.dll) were recompiled as raw XEX placeholder thunks — the 360
loader rewrites those at bind time, but recompiled code compiles the
pre-bind bytes: the leftover `mtctr r11; bctr` used a stale r11 that pointed
back into the CALLER, so the first sibling call recursed caller<->thunk until
guest stack overflow (~3ms after the Waves DLLs load). Fix on both sides:
codegen patches each unresolved-import thunk in the loaded image into a real
IAT-slot dispatch (`lis/lwz r11, slot; mtctr; bctr` — exactly 16 bytes) and
registers it as a function; the runtime binds the type-0 slots against the
sibling's export table after every module load, before DllMain, load-order
independent. Single-module titles have none of these: fleet gate 30/30
byte-identical by construction.

**The pipeline got dramatically cheaper on multi-XEX (the fifadllzf case
study, 101k funcs):**
- companion deep-extract and jump-table recovery are now ONE-SHOT per module
  (they re-ran on every build: ~8min of serial IDA + ~15min of codegen
  probes re-paid per heal round; now a module heal round is codegen +
  incremental build + run — zero IDA)
- the pure-add gate skips its opening baseline probe when the caller just
  codegen'd (284s) and the fold re-codegen when nothing was accepted (284s)
- clang OOM is an auto-fixed build failure: halve --parallel, retry
  incrementally, and the lesson PERSISTS in the port statefile (heal-loop
  rebuilds inherit it; the heal loop has the same handler)
- heal rounds no longer pay a full cmake reconfigure (configure only when
  CMakeCache.txt is missing; input changes force it via a stamp)
- the IDA cache key is content-based (analysis scripts + funclist), so
  tooling commits in xenon-jumptables stop invalidating the fleet's cache
- STFS extraction streams (constant memory) — a 2.1GB entry (GTA V install
  parts) MemoryError'd the old whole-file read

**Multi-disc installs: the GTA V "insert installation disc" wall, killed
end-to-end.** An IDA decompile of the game's install state machine
(sub_8299EE40) + xenia-canary source + a working xenia session log pinned
the full chain, and each link got its fix:
1. `XamContentCreateEnumeratorInternal` implemented (was a stub — the
   install discovery enumerated EMPTY and the game asked for the disc);
2. the game volume answers `FILE_DEVICE_CD_ROM` (retail from-disc branch;
   answered DISK the game took the installed-to-HDD-build path);
3. content packages stage EXTRACTED in the real content root
   (`Documents/<title>/0000000000000000/<title_id>/00000002/<pkg>/`) with
   `.header` files generated from the true PIRS metadata ("gtav - part0");
4. content-mount device paths carried a trailing separator that broke
   `<pkg>:\partN.rpf` resolution (double backslash = empty path component);
5. `XamSwapDisc` now signals its completion KEVENT — the one-arg stub
   swallowed the handle, so after the install gate PASSED the game waited
   forever ("stuck loading").
Result: GTA V streams from its mounted install packages and reaches the
loading screen; remaining walls are ordinary run-heal cures. The extractor's
STFS reader streams now (constant memory — the 2.1GB part rpfs MemoryError'd
the old whole-file read, truncation audit preserved). Full `--install-disc`
automation lands next.

xenon-jumptables (live-referenced): closure_cert splitimm exactness — the
whole 30-port fleet now certifies ZERO static holes; extract_funcs matches
companion-module prefixed symbols (the empty-known-list root cause) and
gained a 65x init.h fast path.

SDK_PIN -> 81ccf82 binaries. Gate: 30/30 codegen byte-identical.

## 2.22.0 — "silent-miscompile guard" (2026-07-10)

The ">100%" axis: hunting codegen that is WRONG but never crashes (run-heal
is structurally blind to it). A 15-agent audit of every risk builder vs the
PPC/Xenon/IEEE specs produced 5 candidate bugs; ground-truthing each against
the live source proved 4 are deliberate, game-validated choices (32-bit
carry/CR0 = the 360 ABI's dominant case; vcmpbfp NaN = matches Xenia's
lowering; denormal flush = the documented Skate 3 audio fix) and exactly one
real: **NORMPACKED64 (4:20:20:20) unpack decoded x=y=z to 0.0 always** — the
20-bit sign-extend `int32_t(u64<<44)>>44` was undefined behavior (shift >=
width) and the cast dropped the field (SDK 78af0a8). No shipping port emits
NORMPACKED64, so the fleet gate is byte-identical 30/30 by construction: a
latent cure, zero risk.

**The class is now guarded forever:** `tools/codegen_ub_lint.py` (SDK
09a18ee) statically flags any `intN_t(...) >> K` / `.uNN >> K` with K >=
width inside the emitted templates — green on the current builders,
regression-tested against the pre-fix pattern, exit 1 so it can gate commits.
It also surfaces (non-failing) same-helper flag inconsistencies like the
known FLOAT16_2/FLOAT16_4 RTZ/RTE split, so a NEW divergence gets a human
look.

**rexauto: companion-module deep-extract fixed (was a silent no-op).** For a
multi-XEX companion, stage_jumptables runs before the module's sources exist
and wrote an EMPTY functions-list; stage_deepextract then fed that empty
known-set to IDA, every already-emitted function came back as a "candidate"
(fifadllzf: 92188) and the pure-add gate rightly rejected the lot —
accepted=0 on every companion, and real cures (FIFA Street's 0x827838A0
FATAL) were discarded with the noise. The known-list is now refreshed from
the generated sources whenever it is missing/empty; healthy entrypoints are
untouched.

SDK_PIN -> 09a18ee binaries. Gate: 30/30 byte-identical (budokai3's flagged
diff was a stale baseline — two deep-extract cures folded after its last
bless, proven identical under the v2.21 and v2.22 rexglue alike — re-blessed).

## 2.14.0 — "truthful diagnostics" (2026-07-03)

A Gears of War 3 report ("recompilação falhou") unraveled a three-bug chain
that could send any port's run-heal into a forever loop — all three fixed:

**Ghost targets (SDK codegen, fleet-wide 1-line diff).** The generated
`REX_CALL_INDIRECT_FUNC` only wrote `ctx.last_indirect_target` on the
fallback path; when a call hit an unregistered slot (which holds the trap),
the trap fired reporting a STALE address from an earlier resolved call. The
heal then chased functions that were already registered, forever. The target
is now written unconditionally, and the trap logs `GetFunction(target)`
before aborting — table-miss vs call-path bugs are now distinguishable at a
glance ("trap diagnostics").

**Stale-exe heal rounds (rexauto, since v2.10).** Restoring the init
header's old mtime after added-DECLARE-only diffs is unsound with the PCH:
clang validates the precompiled header against the header's CONTENT, so the
stale PCH failed every subsequent compile. A changed header now always
keeps its new mtime (PCH and TUs rebuild).

**Always-0 build exit code (rexauto, since forever).** `_build.bat` ended
with `echo RC=%errorlevel%` — the echo RESET the errorlevel, so a failed
rebuild returned success and the heal silently relaunched the stale exe.
The bat now propagates the real build exit code (`exit /b`).

Also: per-project tomls are repaired automatically when a writer corrupts
line endings into doubled carriage returns (rexglue's parser hard-fails on those; seen once
in the wild on a frozen-exe jumptables run).

SDK pin: `rexglue.exe` → `c3c1139d`, `rexruntime.dll` → `1840f9ad`
(SDK commit `f9a5ebb`). The codegen diff is one macro line in every port's
init.h — judged and re-blessed fleet-wide.

## 2.13.1 — "game icons" (2026-07-03)

Built exes now carry the game's marketplace tile as their Windows icon.
`exeicon.py` converts the cached XboxUnity tile (the same one the GUI shows)
into a proper multi-size icon group (16/24/32/48/64, classic 32bpp DIBs) and
injects it post-link with the Win32 resource-update API — no .rc file, no
CMake/codegen change, output `.text` untouched. Hooked into `do_build`, so
every heal relink re-brands automatically; best-effort (offline/no-tile just
skips). Fleet pass branded all 18 existing exes.

## 2.13.0 — "transparent decode" (2026-07-03)

**Captain America: Super Soldier reaches GAMEPLAY** — and the wall it was
stuck behind is now a permanent pipeline capability.

**New stage `xctd`** (between extract and init): titles whose assets ship
transparently compressed (XCompress LZXTDECODE, magic `0F F5 12 ED`; the
Xbox 360 KERNEL decompresses these on real hardware) are pre-decompressed in
place, originals kept in `xctd_originals/`. Our runtime stubs
`XFileXctdCompressionInformation`, so the game takes its "not compressed"
path — serving plaintext is exactly what it expects. No-op (0 files) for
every other title: fleet regression-free by construction.

The format was cracked empirically (a 4-agent workflow root-caused a 1-byte
parser desync: zero padding before 128KB boundaries can be ODD-length) and
confirmed against public prior art (QuickBMS `unxmemlzx`, UniPyX). The
decoder (`tools/xctd_rip.cpp` + vendored libmspack lzxd, built on demand
with the pipeline's clang) handles the full matrix:
- header flags: window, pad boundary, segment count, 20-bit/BE32 size table
- raw payloads (segments=0), single-stream bundles, multi-segment archives
- BOTH segment layouts: contiguous chunk stream (Captain America `DATA.*`)
  and zoned/`k*zbs` (XBLA titles — 'Splosion Man/Ms. 'Splosion Man carry 237
  XCTD files each, 834 MB → 2.4 GB)
- BOTH LZX dialects: XMemCompress omits the CAB realign pad byte after
  odd-sized UNCOMPRESSED blocks — per-segment retry with a dialect switch in
  the vendored lzxd. Every decode is fully verified (frame accounting +
  exact produced sizes) before anything is swapped.

**Runtime: XUsbcam stub exports enabled** (SDK commit `1a9ac64`): the
'Splosion Man pair imports the camera API and could not link. Pure export
addition (no existing title touches the stubs); pin → `9b2c8fb1`.

**Run-heal: absorbed-gap/vtable-thunk cure.** When an address is registered
but the runtime still flags it after an exe resync, the cause is a neighbour
function whose emitted body absorbed a functions-list gap containing the
target (Captain America: a 16-byte virtual-call thunk at 0x822A2040 inside
0x822A2010's body). The heal now shrinks the containing function with an
end-override at the target — a true boundary, since the game just
indirect-called it — and retries. Proved live: CA converged in 5 launches,
survived 150s clean.

## 2.12.0 — "clean runtime, long watch" (2026-07-03)

Two operational hardenings, no new features:

**Run-heal confirm window floors at 150s.** Gears of War Judgment converged
"clean" at the old 47s window and then FATAL'd ~71s into gameplay — a function
(`sub_824CA490`) that only loads late. The heal ROUNDS stay fast (22s); only
the initial discover pass and the final convergence check now run ≥150s, so
late-loading indirect targets are cured up front instead of surfacing as a
mid-gameplay crash. Gears re-verified: converged in 1 launch, survived 150s.

**Runtime rebuilt without the exploratory texture-dump path** (a GPU debug
feature, cvar-gated OFF, never shipped in any release). No tracked runtime
source changed (fiber HEAD `afec3c0` + GapFill `9efdddc`); C++ links are not
byte-reproducible so the SDK pin is re-generated to the shipped dump-free
binary (`rexruntime.dll` → `1258109c`). Validated: Gears 150s survive + GTA-SA
gate PASS (codegen 99 files byte-identical, runtime boots alive 0 FATALs,
runtime baseline blessed).

## 2.11.0 — "codegen fast" (2026-07-02)

One SDK codegen fix, found by measuring instead of guessing: GapFill's
absorbed-cleanup was **O(gapfills × total functions)** — ~1.8 billion
`containsAddress` probes at 42k functions, and quadratic growth on bigger
titles (a real slice of GTA V's ~5min-per-codegen passes). Replaced with a
backward walk of the existing sorted-base index (the same one
`getFunctionContaining` uses), bounded by the largest function size. Same
predicate, same removal set ⇒ **byte-identical output**, proven by the
regression gate twice (blessed fleet PASS, 29–125 files identical per title).

**Measured: 8.2s → 31ms per codegen pass on GTA-SA (264×).** Every title pays
this on EVERY codegen pass (setjmp scan, image dump, pure-add gate passes, heal
retries), so it compounds: a fresh GTA V port runs 6–8 codegens.

Honest engineering note: a parallel-Discover attempt was built, adversarially
reviewed, and REVERTED this same session — it gave ~0 warm-run gain (the 19s
"Discover cost" was cold-cache illusion; warm is 2.3s) and the review proved a
real determinism hazard on chunk/parent titles. The gate + adversarial review
did their job. The wall-clock win came from fixing the algorithm, not from
throwing cores at it.

SDK_PIN: `rexglue.exe` → `7e9591d4` (codegen-only); `rexruntime.dll` unchanged
(`20aec5ac`).

## 2.10.0 — "build fast" (2026-07-02)

Pipeline speed, all byte-identical output. **rexauto-only; SDK unchanged**
(`rexglue.exe` `06b93244`, `rexruntime.dll` `20aec5ac`). Every change is
output-neutral by construction and proven by the regression gate (codegen
byte-identical fleet-wide) plus targeted empirical checks. Driven by an 18-agent
codegen-engine audit that measured where the time actually goes.

- **Heal loop 70s → 6s (11×), measured on GTA San Andreas.** Two build-flag
  fixes: (1) **PCH resurrected** — the v2.4.0 precompiled-header win had silently
  regressed (the injection ran before `<name>_init.h` existed, so its exists()
  guard skipped it; a fleet audit found 1/18 ports actually had the PCH). Now
  wired after codegen, idempotent. (2) **`-gline-tables-only`** on RelWithDebInfo:
  keep function symbols + line tables (cdb guest stacks still resolve) and drop
  the variable/type debug info that bloated the PDB (~100MB → ~63MB) and dominated
  every relink. Both output-neutral (PCH caches the AST, debug flag is debug-info
  only) — `.text` stays byte-identical.
- **Global IDA cache.** The jump-table IDA pass — the pipeline's one serial
  single-core minutes-long sink — is fully determined by the image bytes. Keyed
  by sha256(image) + section ranges + xenon-jumptables rev; a re-port of the same
  game (or a wiped work dir) copies the cached switch_tables.toml + .i64 instead
  of re-analyzing (GTA SA: 2min5s → instant; the .i64 reuse also speeds
  deep-extract). `REXAUTO_NO_IDA_CACHE=1` to disable.
- **setjmp/jumptables image-dump merge.** The setjmp stage ran a FULL codegen
  purely to dump the guest image, which the jumptables stage then re-dumped
  identically. The image dump is the raw decompressed sections
  (project_recompiler.cpp:251), independent of setjmp/functions.toml — proven
  byte-identical across two dumps. jumptables now reuses the setjmp stage's image
  + ranges: **−1 full codegen per port** (GTA SA ~46s, GTA V ~4min).
- **pure-add gate: fewer codegen passes.** deep-extract's pure-add gate re-ran a
  full codegen to restore generated/ (thrown away — stage_build re-materializes it
  from the authoritative toml) and a redundant final safety pass when the loop had
  already converged. Both dropped; the accepted set (the only thing that reaches
  functions.toml) is unchanged. GTA V's ~14min gate roughly halves.

Audit also mapped the SDK-side wins (parallelize the Discover/GapFill worklist to
use all cores, GapFill decode-once) — those need an SDK change + pin bump and land
in a later release, gated the same way.

## 2.9.0 — "fibers" (2026-07-02)

Guest fiber support in the runtime + truncated-container guards in the pipeline.
**SDK runtime changed** (`rexruntime.dll` → `20aec5ac`, `rexglue.exe` unchanged
`06b93244` ⇒ codegen byte-identical fleet-wide; gate all-blessed PASS + runtime
spot-checks).

- **Guest fibers (SDK runtime):** `XThread::Reenter` + `reenter_exception`, and
  `KeSetCurrentStackPointers` now unwinds the host stack back to `XThread::Execute`
  and re-enters guest code at the new fiber's LR when the guest swaps fibers — the
  exact mechanism mainline Xenia uses. Reentry addresses (often MID-function: the
  fiber's own `bl SwapContext` return site) resolve via `ResolveIndirectFunction`,
  so unregistered sites flow into the standard run-heal machinery instead of
  silently ending the thread. Gated on `X_KTHREAD::fiber_ptr`: titles that never
  fiber-switch (the entire pre-Korra fleet) never take the new path. Unlocks the
  PlatinumGames digital titles (Korra/Transformers Devastation/TMNT) and the
  Halo 3/Reach/4 + Forza 2 class (xenia-project label
  kernel-KeSetCurrentStackPointers, 15 titles). Proven live on Korra (58411447):
  dead-at-boot → engine fully up (~20 guest worker threads, input polling, XMA
  audio, real render pipeline; shader storage 6→8).
- **Truncated-container guards (extract):** an incomplete download extracts
  SILENTLY broken — `Stfs.read_chain` reads past-EOF blocks as empty, so a
  truncated STFS yields short/0-byte files that later surface as unexplainable
  runtime behavior. extract now audits every written file against its table
  length and FAILS with the file list + "re-download" hint; a folder source gets
  a 0-byte-file audit with a loud warning. This exact class cost an hours-long
  hunt on Korra: the final wall was a 0-byte `Nickelodeon.usm` intro movie from a
  truncated `.zip.part` download — the game opens it, our kernel honestly reports
  size 0, CRI Mana errors, and the title black-screens forever. Recompilation and
  runtime were correct end-to-end (proven by instrumenting every link of the
  chain: wrapper status poll → Mana internal state → CriFs GetFileSize →
  GetFileSizeEx → NtQueryInformationFile(class 34) → VFS → the file really is 0
  bytes on disk).

## 2.8.0 — "launch once" (2026-07-02)

Verification stops re-launching the game. A cured title now launches **twice ever**
(a priming boot + one long-window confirmation) and then **zero times** on every
subsequent pipeline run, via a persisted convergence receipt. Directly targets "the
pipeline keeps opening and closing the game". **rexauto-only; SDK unchanged**
(`rexglue.exe` `06b93244`, `rexruntime.dll` `4e75b494`).

- **Convergence receipt (Tier 0 = 0 launches):** `<name>_runheal_receipt.json`,
  fingerprinted by sha256(exe) + sha256(rexruntime.dll) + sha256(xex) [+ title-update]
  + game root. Matching receipt (verified with a window ≥ the one requested now) ⇒
  runheal doesn't launch at all; any real change (codegen/cure/SDK/re-rip/game swap)
  changes a hash and re-verifies automatically. `REXAUTO_FORCE_RUNHEAL=1` or deleting
  the receipt forces a live check. Receipts are minted only on POSITIVE evidence:
  game still alive at window end, real code range known (not the fallback window),
  a log actually produced.
- **Merged discover+confirm (Tier 1 = minimal launches):** the old
  discover(22s)×N → fatal(22s) → confirm(47s) dance is one loop of discover-mode runs.
  Soundness: a discover run that logs **zero targets at all** no-op'd nothing, so its
  execution is bit-identical to a clean run — it doubles as the confirmation. Heal
  rounds keep the short window for fast iteration; only the deciding clean run pays 47s.
- **Second-boot coverage kept:** the deciding run must not be the guest state's
  first-ever boot (first boot creates saves/caches; load-existing-state paths — the
  v2.6.0 xam_content crash class — only run on boot 2). A priming marker keyed to the
  guest fingerprint (not stale log files) tracks this.
- **Honest multi-XEX verdicts:** convergence keys on zero **logged** targets, not zero
  in-range. An out-of-image/misaligned call that discover mode no-op'd would FATAL a
  production run — that now yields a "production_fatal" verdict with the uncurable
  list, never a "survived" receipt (old flow's fatal-confirm honesty, preserved).
- **Corrupted-continuation guard:** when a discover run logs uncurable targets
  alongside curable ones, everything after the first no-op ran on corrupt state — one
  fatal-mode run at the SAME window re-reads ground truth before anything is
  registered; a clean fatal re-read is treated as inconclusive, never as a verdict.
- **`_code_range` actually reads the range now:** a doubled "default" path segment made
  it silently fall back to the generic 0x82000000–0x84000000 window for EVERY game
  since inception — the out-of-image guard never used the real per-title code range.
  Fixed; the fallback (exact=False) additionally blocks receipt minting.
- **Failures fail:** no-exe / no-log / rebuild-failed paths raise SystemExit (like
  every other stage) instead of writing a truthy state mark that made the next
  pipeline run print "skip runheal (done)" for a stage that verified nothing.
- Hardened by two adversarial review workflows (4 + 2 agents): 11 findings folded in
  (zero-logged vs zero-filtered convergence, no-evidence receipts, guest-image
  fingerprinting, receipt window honoring, --publish-gabarito on receipt hits,
  label-heal rc re-check, chronological exemplars, plain-retry bounding).
- Proven live on budokai3: run 1 = "converged in 2 launch(es)" + receipt; run 2 =
  "receipt matches → not launching the game". Codegen untouched ⇒ byte-identical
  fleet-wide.

## 2.7.1 — "resync" (2026-07-02)

Run-heal no longer declares a false wall on a stale exe. **rexauto-only; SDK unchanged**
(`rexglue.exe` `06b93244`, `rexruntime.dll` `4e75b494`). Codegen byte-identical across the
fleet (gate: 8 blessed games PASS, only the two intentionally re-ported games changed).

- **Resync-before-stuck** (`stage_runheal`): when a FATAL names a function that is ALREADY
  registered in the current sources, the running exe can lag the codegen — an earlier
  codegen (deep-extract gate churn, or a prior no-op heal) leaves `register.cpp` newer than
  the linked exe, so the built exe's dispatch tables never got `SetFunction(addr)` → a
  SPURIOUS "invalid or unregistered function" fatal on a function that source-registers
  fine. Run-heal now forces one codegen+relink to resync the exe and retries; only if the
  same address STILL fatals after a clean relink is it declared a genuine wall (anti-loop
  guarded, one resync per address).
- **Why it matters:** this exact stale-exe case made **dbz** (`dragon_ball_z_ultimate_tenkaichi`)
  look like an unfixable runtime wall at `0x82415F90` (a registered vtable method) when a
  plain relink converged it. The false "stuck" verdict is the kind of thing that wrongly
  concedes a title as "not recompilable".
- **Fleet: 4 → 5 stable.** dbz re-ported on the v2.7.0 pipeline now converges (47s, into
  gameplay context). **budokai3** re-generated fresh (no hand-tuned `.WORKING74`/`.corrected69`
  cruft): 75 switch tables (≥ the old 74), +69 deep-extracted functions, run-heal a **no-op**
  (deep-extract cured everything statically) — the pipeline alone matched months of manual
  tuning. Both blessed.

## 2.7.0 — "cure once" (2026-07-02)

Static function/vtable recovery is now a **pipeline stage** — a game's "invalid
function" cures are found up front from ONE deep IDA pass instead of by launching the
game N times, so run-heal is left as a rare backstop. Directly targets the "I keep
re-curing every game in runtime" pain. **rexauto-only; SDK unchanged** (`rexglue.exe`
`06b93244`, `rexruntime.dll` `4e75b494`; `deep_extract.py` already in the bundled
xenon-jumptables).

- **New `deepextract` stage** (`extract → init → setjmp → jumptables → deepextract →
  build → runheal → run`): reuses the `.i64` the jumptables stage already produced
  (copied, never the original), runs a deep IDA pass (funcmap ∪ vtable data-xref) to
  harvest the function/vtable-target set the linear scan misses — ~96% of what run-heal
  otherwise discovers dynamically.
- **The pure-addition gate** (`deepextract.py`): a candidate is folded in ONLY if adding
  it is a pure addition — it codegens to its OWN new function with a real (non-stub)
  body, introduces no dangling `goto` (a split), and changes no pre-existing function's
  body. Inspects the ACTUAL codegen output, so it structurally forbids the crash-mask (a
  return-only stub that would turn a real "invalid function" abort into a silent return).
- **run-heal kept as the backstop** for the genuinely-dynamic residue (~4%).
- Proven on joust: 282 candidates → gate accepts 67 (drops 215 as swallow/stub/split) →
  builds + boots + survives 47s, run-heal a no-op (`discover round 1: 0 new`). The wall
  `0x823010C8` (a live vtable-dispatch crash) is cured statically, before the game runs.
- Opt-in on IDA (no idat / no `.i64` → skip → byte-identical), fully additive
  (superset-only `{}`). Zero regression: codegen byte-identical across the fleet.

## 2.6.0 — "Gears boots" (2026-07-01)

A one-line **runtime** fix (found by a multi-agent IDA-Pro diagnosis) makes Gears of
War Judgment boot **stably deep into startup** — it now survives 47s+ alive with no
fatal (was a non-deterministic ~5s crash). **SDK runtime changes** (`rexruntime.dll`);
`rexglue.exe` (codegen) is byte-identical, so the fleet's generated code is untouched.

- **Root cause (a dangling guest string_view):** `xeXamContentCreate` captured
  `root_name = root_name.value()` into its deferred-completion lambda. `MappedPtr<char>::
  value()` returns a `std::string_view` over GUEST memory (no copy); the completion runs
  ~100ms later, by which time the guest recycled the buffer. If the recycled bytes were
  not valid UTF-8, the content-path conversion (the checked utfcpp API) threw
  `utf8::invalid_utf8` → `REX_FATAL("...threw 'Invalid UTF-8'")`. Even when benign, the
  save package mounted under a garbage root, so the `SG0_0:` save device never resolved.
- **Fix:** own the bytes at call time — `root_name = std::string(root_name.value())`.
  Semantics-preserving for any well-behaved caller; only fixes the recycled-buffer case.
  On Gears: the crash is gone AND `SG0_0:` now mounts (via `\Device\Content\N\`).
- Zero regression: codegen byte-identical across all 10 baselined fleet games (rexglue
  unchanged); the change is one localized, semantics-preserving capture. `SDK_PIN`
  `rexruntime.dll` bumped; `rexglue.exe` `06b93244` unchanged. Gears baseline re-blessed.
- Honest limit: the remaining Gears walls (a media-verification watchdog it tolerates,
  intro-movie playback) are runtime/GPU emulation, not recompilation.

## 2.5.1 — "boot deeper" (2026-07-01)

Run-heal now keeps hand-written asm routines WHOLE instead of splitting them, so
Gears of War Judgment boots far past its intro decompressor (GPU up → movies →
networking → media verification, vs the old ~1s crash). **rexauto-only; SDK
unchanged** (`rexglue.exe` `06b93244`, `rexruntime.dll` `0ce11411`).

- **Root cause:** the intro decompressor (`sub_830AFE28`, a switch-on-ctr state machine
  with a shared-tail loop-back) had an under-recovered jump table (IDA found 7 of ~10
  landings). At runtime the missing landings hit `default: REX_CALL_INDIRECT_FUNC` →
  "invalid function"; the play-and-heal loop then registered them as standalone `{}`
  functions, which SPLIT the routine — turning a healable "invalid function" into an
  UN-healable `REX_FATAL("Unresolved branch")` when the split copy's loop-back branched
  into the parent.
- **`heal.register_or_seed`:** an unregistered-function address that falls INSIDE an
  existing function's `end`-override span is a landing of that routine, not a new
  function → route it to forced_landings (keeps the routine whole), never a `{}` split.
- **`heal.extend_switch_table`:** such a landing is also added as a `case` to the
  routine's bctr switch table (so the dispatch resolves it instead of hitting the
  default), paired with its forced_landings `loc_`. Under-recovered bctr tables now
  self-heal at runtime.
- Zero regression: `regression_gate.py` codegen byte-identical across all 10 baselined
  fleet games (these are run-heal changes; they never touch a passing game's data).
  Gears baseline re-blessed. Remaining Gears walls (media-verification DRM loop, an
  "Invalid UTF-8" async completion) are runtime/kernel-emulation, not recompilation.

## 2.5.0 — "Gears builds" (2026-07-01)

Adds **Gears of War Judgment** — the fleet's largest title (59,396 functions, 124 codegen
units, XGD3) — as a port that **builds, boots, and converges**, via a small opt-in
**codegen** SDK change. `rexruntime.dll` is byte-identical, so no game's runtime changes.

- **Root cause:** a hand-written computed-goto routine (`sub_830AFE28`, a stateful
  decompressor loop) is dispatched by a `bctr` jump table whose landings the heuristic
  `detectJumpTable` under-recovers — 3 stay dangling `goto loc_T` with no block →
  permanent `use of undeclared label` stall. Splitting the landings into functions
  passes the build but severs the loop's back-edge into a runtime `REX_FATAL`; the
  routine must stay whole.
- **Fix (SDK, codegen-only):** new `forced_landings = [0x..]` config array. During block
  discovery, after normal flow, a listed address inside a function that normal control
  flow did NOT reach is seeded as an in-function block — its `loc_` label is emitted and
  the routine stays one whole function. Empty list ⇒ seed inert ⇒ **byte-identical**.
- **Fix (rexauto, self-healing):** the undeclared-label heal now writes the exact
  landing addresses to `<game>_forced_landings.toml`, wires it into the manifest, and
  re-codegens — converging any title with this defect, no per-game hack.
- **Zero regression, proven:** codegen byte-identical across all 10 baselined fleet games
  (`regression_gate.py`); Gears builds → 91 MB exe → boots → run-heal converges with no
  invalid-function FATAL (the decompressor runs — the split approach would have crashed).
  SDK: `rexglue.exe` new codegen pin; `rexruntime.dll` unchanged (`0ce11411`).

## 2.4.2 — "cover art" (2026-07-01)

Cover art for **ISO / GoD / folder** targets in the desktop app. Xbox 360 discs don't
embed cover art (it's a marketplace tile, not on the disc), so ISO targets used to show
a blank card. Now the GUI fetches the game's tile by `title_id`.

- **title_id from the disc's `default.xex`** — a new XEX2 parser reads the execution-info
  header (validated: SVR07 → `545107E0`, skate3 → `454108E6`). `read_package_meta` now
  fills `title_id` for raw XEX / folder / GDFX ISO targets. The ISO reader walks the GDFX
  at every XGD base offset (0x0, XGD2 `0xFD90000`, XGD3 `0x2080000`, …) — proven on real
  Captain America (XGD2), Gears of War Judgment (XGD3), and skate3 (base 0x0) images, and
  it correctly returns nothing for a non-Xbox disc (e.g. the PS3 Skate 3).
- **Cover fetched from XboxUnity** by title_id (`fetch_title_icon`), cached under `covers/`
  so it's pulled once per title. Best-effort and offline-safe — a network failure just
  falls back to the placeholder; a title with no tile is negative-cached.
- No pipeline/codegen change; SDK unchanged (`95010481` / `0ce11411`).

## 2.4.1 — "right target, clear signal" (2026-07-01)

Desktop-app (GUI) + extract UX fixes. No pipeline/codegen change; SDK unchanged.

### GUI state reset on target change
- **Name stuck on the old target.** The name only auto-derived when the field was
  empty/"game", so after the first target set `name="skate_3"`, picking a new target
  kept the old name → the new game recompiled into the wrong project dir. A `nameAuto`
  flag now re-derives the name on every target change until the user types their own.
- **Cover not reset.** Switching from an STFS package (has an embedded thumbnail) to an
  ISO (none) left the *previous* game's art on the card. The no-cover branch now clears
  the stale cover and shows a neutral placeholder (Xbox 360 discs don't embed cover art —
  it's a marketplace tile, not on the disc; the title still shows below the card).

### Clearer extract error on the wrong disc
- Feeding a **PlayStation 3** disc (or any non-Xbox ISO9660 image) failed with an opaque
  `unsupported container (magic=b'\x00\x00\x00\x04')`. extract now probes for an ISO9660
  PVD + PS3 markers (`PS3_GAME`/`EBOOT.BIN`/`PS3_DISC.SFB`) and says plainly it's a PS3
  disc and that rexauto needs the Xbox 360 version (a very common mistake with
  multi-platform games like Skate 3).

### SDK
- Unchanged (`SDK_PIN` still `95010481` / `0ce11411`); `rexglue-sdk-win64.zip` identical
  to 2.3.0/2.4.0. Only `rexauto.exe` changed.

## 2.4.0 — "parse once" (2026-07-01)

Fleet-wide **build-perf** release — the recompile is faster with **zero codegen change**
(the generated C++ and every title's binary stay byte-identical; the regression gate is
unaffected because a PCH touches compile speed, not emitted code).

### Precompiled header for the `<name>_init.h` monolith
- Every generated recomp TU opens with `#include "<name>_init.h"` — a huge header (tens of
  thousands of `DECLARE_REX_FUNC` externs + heavy C++23 STL; skate3's is 1.56 MB / 48.6k
  lines). Its front-end parse was a fixed per-TU floor paid once **per TU** (a 24-function
  TU still cost ~3.7s = pure header parse).
- rexauto now injects `target_precompile_headers(<name> PRIVATE generated/default/<name>_init.h)`
  into every port at build time so clang parses it **once**. Idempotent; extra recompiled
  modules (e.g. skate3's EAWebkit, which include their own init header) are marked
  `SKIP_PRECOMPILE_HEADERS`. Opt out with `REXAUTO_NO_PCH=1`.
- **Measured** (skate3, eawebkit as an in-build no-PCH control): default-module per-TU
  compile **9.9s → 7.83s (~21%)**, small TUs **3.71s → 1.17s (3×)**. Proven on skate3
  (multi-module) and joust (single-module). Single-module titles — most of the fleet — get
  the full per-TU cut on the wall-clock (no un-PCH'd module tail).
- **Output-neutral by construction**: a PCH caches the parsed AST, never the emitted code.

### Profiling note (why this is the lever)
A 16-agent profile of the real `.ninja_log` found the recompile wall-clock lives in two
co-dominant ~90s sinks: a compile phase that is **already 16-thread-saturated** (link is a
negligible ~1s) and a **100%-serial IDA jump-table pass**. So the win is not "more CPU/RAM"
(compile is maxed; link/IDA don't parallelize) but **cutting redundant work** — hence the
PCH. Next on the roadmap: caching IDA's `.i64` database (40–175s off every re-run).

### SDK
- Unchanged from 2.3.0 (`SDK_PIN` still rexglue.exe `95010481` / rexruntime.dll `0ce11411`);
  `rexglue-sdk-win64.zip` is identical. Only `rexauto.exe` changed (the PCH injection).

## 2.3.0 — "the Yukes crack" (2026-07-01)

Cracked **WWE SmackDown vs Raw 2007** (Yukes engine, title 545107E0) — it now boots
to the in-game menu (playable). The community needed a custom `rexglue-sdk-yukes`
fork "with fixes this game depends on"; a 16-agent diff of their **working** build
against ours found the truth was inverted — *ours is the newer, superset SDK*, and
the blocker was a **regression in our own runtime**. Three SDK runtime fixes, all
**codegen-untouched → the whole fleet's generated C++ stays byte-identical**
(regression gate: 10/10 blessed titles identical, skate3 runtime PASS).

### The fatal fix — FPSCR host-thread MXCSR mask leak (fleet-wide)
- `XHostThread::Execute` ran guest FP over a context that never called `InitHost()`,
  so its cached MXCSR was `0` (memset). The guest's flush-mode toggles then wrote
  `MXCSR=0`, **unmasking the inexact FP exception** → the next inexact float op
  trapped as `STATUS_FLOAT_INEXACT_RESULT` (`0xC000008F`) ~13s into play. Guest
  `XThread::Execute` already inits FP; host worker threads did not.
- Fix (`xthread.cpp`): `thread_state_->context()->fpscr.InitHost()` at the top of
  `XHostThread::Execute`. **Generalizes to every title** with host-thread guest
  dispatch, and obsoletes the two per-path re-mask band-aids (audio / xma decoder)
  that were whack-a-moling this exact `STATUS_FLOAT_INEXACT_RESULT`.

### Writable `cache:` VFS device (fleet-wide)
- Yukes titles decompress their PAC asset packs into the Xbox 360 `CACHE:` scratch
  partition; with no device mounted every `CACHE:\...` open returned `0xC000000F`.
  `Runtime::SetupVfs` now mounts a **writable** `HostPathDevice`
  (`cache_root_/guest_cache`) + `RegisterSymbolicLink("cache:")`. Any title that
  uses the 360 cache partition now works.

### Ranged physical-alloc offset (xenia parity)
- Enabled the xenia `ignore_offset_for_ranged_allocations` behaviour in
  `MmAllocatePhysicalMemoryEx` (drop the physical offset for a ranged request; the
  in-code note names WWE SvR `545107E0`/`545108B4`). **Ranged-only** → the common
  `MmAllocatePhysicalMemory` path is byte-identical.

### Fleet / gate
- **SVR07 added as a tracked title** (codegen baseline blessed, 58 files).
- SDK commit `b363c08` (rexglue-skate3 `fork-base`); `SDK_PIN` bumped to
  rexglue.exe `95010481` / rexruntime.dll `0ce11411`. Every runtime change is
  additive/corrective and the regression gate proves no fleet title regressed.
- Sibling Yukes/THQ titles (e.g. WWE SvR 2008 `545108B4`) now inherit all three
  fixes for free — the first game of a family is the hard one; the rest are cheap.

## 2.2.0 — "parity, proven" (2026-06-30)

A full parity audit against the community build (mchughalex/skate3recomp, source
cloned and diffed dimension-by-dimension) confirmed our Skate 3 is **ahead** — same
app layer (DLC/marketplace, ISO installer, profiles, host-side ultrawide, EAWebkit
menus, fonts all byte-identical), a **superset** of recompiled-code coverage, and it
ships the **Title-Update-3-patched image** (the "ours is retail" worry was false:
manifest setjmp/longjmp = their TU3 addresses, and `game/*.xexp` SHA-256 match). The
audit found **three** real user-facing things their build system wired that ours did
not — now closed, generically.

### New pipeline stage: `codegen_patches`
- **`codegen_patches.py`, wired into `do_codegen`**: a declarative, per-project
  `<name>_codegen_patches.toml` splices host-side hooks into the generated
  `<name>_recomp.*.cpp` after codegen converges and before compile. Two reusable
  kinds — `literal` (exact find→replace in the one file matching every `require`) and
  `insert_before_call_after_anchor` (find the first generated guest call after an
  anchor and inject a line). Each patch is **idempotent** (`marker`) and **hard-fails**
  if its anchor is gone (a codegen re-layout must never silently drop a shipped
  behaviour). No config → no-op (fleet byte-identical). This generalizes the
  community's hand-written `cmake/ApplySkate3CodegenPatches.cmake` to the whole fleet.

### Skate 3 parity gaps closed
- **Projection-FOV hook** — the `skate3_field_of_view` / SimpleSettings FOV slider was
  inert (the host fn in `src/skate3_fov.cpp` was compiled but never called from
  generated code). A `literal` codegen patch now injects the override at the
  projection-matrix site. The slider changes FOV.
- **Ultrawide game-frustum hook** — host-side Hor+/NDC ultrawide already worked, but the
  guest cull-frustum wasn't widened (objects culled at screen edges under ultrawide). An
  `insert_before_call_after_anchor` patch injects `Skate3UltrawideGameFrustumPatchScope`
  at the frustum-setup call.
- **Win32 Per-Monitor-V2 DPI manifest** — added `src/skate3_app.manifest` (PerMonitorV2 +
  Common-Controls v6) and linked it via `LINKER:/MANIFESTINPUT`. Fixes high-DPI window
  blur and the skewed monitor-size feed into ultrawide aspect derivation.
- skate3.exe rebuilt against the shipped **v1.9 SDK** (rexruntime `c503f763`); all three
  patches verified compiled/embedded (`Skate3MaybeOverrideProjectionFovRadians`,
  `Skate3UltrawideGameFrustumPatchScope`, `PerMonitorV2` all present in the exe).

The community's demo_path boot-automation (off-by-default QA cvar) and interactive TU
installer wizard remain intentional non-gaps — we pre-stage the identical verified TU3
payloads at build time instead.

## 2.1.0 — "the long-tail, closed" (2026-06-30)

Closes the one open item from 2.0.0: the **switch-on-ctr heal long-tail** that made
sustained Skate 3 play crash non-deterministically (~85s in, at guest `0x82E57160`).
Our fork's `build_bctr` lowers each recovered jump table as `switch (ctx.ctr.u32)` with a
`case 0xTARGET:` per landing; a landing that isn't a registered function/chunk falls back
to `REX_CALL_INDIRECT_FUNC`, which FATALs at runtime if that guest address isn't in the
function table. The community build sidesteps this by lowering switches on an *index*
(inline `goto`), so it never needs the landings registered — we do.

### Headline
- **New pipeline stage `jt_landings`** (`jt_landings.py`, wired into `do_codegen`): after a
  clean codegen it scans the generated tree for every `case 0xT:` that still dispatches
  indirectly, and registers each as a **chained, contiguous chunk** of its enclosing
  function (`end(i)=start(i+1)`, `parent` chained). `classifyTarget` then treats each `case`
  target as a real entry, so `build_bctr` emits a direct `sub_T(...)` call instead of the
  indirect FATAL. A re-codegen converges (the second pass finds none). Fully generic —
  detects the landings of *any* function from the SDK's own table recovery; no IDA pass.
- **Skate 3 now plays sustained**: the 52 residual landings (`0x8270B3D0`×6, `0x829A9280`×5,
  `0x82E56878`×41 incl. the `0x82E57160` crasher) register automatically. Validated **alive
  after 300s (5 min), 0 FATAL, the crasher gone**. Ours is now equal-or-better than the
  community build for sustained play, from the pipeline, with no per-title hand editing.

### Safety / zero-regression
- **No-op for titles whose switches already resolve** (`heal()` returns 0) → codegen stays
  **byte-identical** for the other 9 fleet titles (verified: none have unregistered
  landings). The stage only ever *adds* chunks for genuinely-unregistered landings.
- **Gabarito-seeded configs are safe**: chunks are inserted at the end of the `[functions]`
  table regardless of whether `[meta]` leads (gabarito) or trails (plain port) it, so a
  fresh "clone and re-run" reproduces the playable build (seed → codegen → heal → converge).
- **Idempotent**: re-running against an already-healed config detects 0 and leaves the file
  byte-identical.

## 2.0.0 — "skate 3 born playable" (2026-06-30)

The release where the rexauto pipeline produces a **playable Skate 3** from an Xbox 360
container — plus a runtime-quality gate and several cross-game pipeline fixes, all with
**zero regression** across the 10-title fleet (codegen byte-identical: budokai3, joust,
dragon_ball_z_ultimate_tenkaichi, msmauto, laracroftandtheg, mssplosionman, game,
rayman3hd, skate3, final_exam).

### Headline
- **Skate 3 reaches gameplay from the pipeline** (Title Update 3.0.3.0): boots and runs to
  the `gameplay context reached` milestone in normal mode — it previously only booted.
  Sustained play still registers deep jump-table targets as they surface (the switch-on-ctr
  heal long-tail; the community build sidesteps it via switch-on-index). The runtime gate
  scores "reached gameplay" as the pass and tracks the rest for the heal loop.

### Pipeline
- **Auto-Title-Update** — detect and apply an Xbox 360 TU (`.xexp`) automatically and
  generically; the loader applies the delta in memory at both codegen and runtime, so we
  recompile *and* run the exact patched version. No-op for base-only titles.
- **TU-aware setjmp / exception-guard detection** — the setjmp stage force-dumps a fresh
  image and scans the *patched* (title-update) image, so the CRT structured-exception
  guard is found at its TU address and handled via `ppc_setjmp`. This auto-handles it for
  any TU title with **no per-game hand-coded exception shim** (the community hand-codes
  one per title, per version). Fixes a stale-image-dump bug that mis-detected the guard on
  the un-patched base image.
- **App-glue factory** — a declarative `<game>_appglue.toml` (`[identity]`, `[[alias]]`,
  `[overlay]`, `[dlc]`, `[title_update]`) emits the per-title host glue into the generated
  app's `OnPostSetup`. Forward-looking infra so new titles *declare* host glue instead of
  hand-porting a full app. Gated/no-op: no toml → byte-identical app.
- **RelWithDebInfo by default** — same optimization as Release plus symbols + line info,
  so a crash in the recompiled code points straight at the generated `sub_XXXX` + line
  (the heal/debug loop's biggest pain). Set `REXAUTO_BUILD_TYPE=Release` for a stripped,
  smaller distribution build. Codegen is unaffected → zero-regression for the codegen gate.
  (Maps imported libs to their Release variant under RelWithDebInfo to avoid an
  `_ITERATOR_DEBUG_LEVEL` link mismatch against the SDK's debug spdlog.)

### Gate
- **Runtime tier** (`regression_gate.py --runtime`) — build + headless launch + a
  play-health metric (boots / alive / no new FATAL / reached a gameplay marker) vs a
  blessed runtime baseline. Catches runtime-only and app-glue regressions the codegen tier
  cannot. HEAVY titles get a longer run floor (`REXGATE_RUN_SECONDS_HEAVY`) so late
  gameplay markers are reliably captured.

### SDK (bundled, pinned)
- **vtable-landing discovery fix** — mid-function vtable landings are statically
  registered (`addFunction`, no `registerChunk`): restores coverage while staying
  Budokai3-safe. Runtime carries the caller `lr` in the invalid-call FATAL + GPU
  command-ring memory fixes (battle-freeze).

### Fixes
- `heal.py`: stop doubling CR in CRLF `functions.toml` on rewrite.
- `extract.py`: `xex2_version` bit-order fix (caught by the real Skate 3 TU).

## 1.3 and earlier
Switch-on-CTR `build_bctr`, the jump-table resolver (xenon-jumptables), the boundary/heal
loop, and the all-games codegen regression gate. See git history.
