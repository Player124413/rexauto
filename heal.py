"""
heal.py — the two auto-heal mechanisms, parameterised (no hard-coded game).

boundary heal (build-time): the recompiler sometimes splits a function where the
compiler let control fall into the next one. The generated C++ then has
`goto loc_T` with loc_T declared in a different function -> "use of undeclared
label". Fix: extend the function F that owns the goto to swallow T
(F.end = smallest function start strictly > T). Same class the Skate 3 team fixed
by hand ~3500 times. Forced by a real compile error, so no guessing.

runtime heal (run-time): the dispatcher aborts with "invalid or unregistered
function at guest address 0xADDR" when execution reaches a function the recompiler
never discovered. Fix: register 0xADDR and rebuild. The play-and-heal loop.

Both write into one `[functions]` TOML (`{ end = .. }` overrides and `{}`
registrations coexist). Functions here are pure parsing/derivation; the driver
runs the build/run cycles.
"""
import bisect
import glob
import os
import re

DEFRE = re.compile(r"DEFINE_REX_FUNC\(sub_([0-9A-Fa-f]{8})\)")
UNDECL = re.compile(
    r"([^\s:]+\.cpp):(\d+):\d+: error: use of undeclared label 'loc_([0-9A-Fa-f]{8})'")
INVALID = re.compile(
    r"invalid or unregistered function at guest address 0x([0-9A-Fa-f]+)")
# codegen Validate: "0xTARGET from 0xCALLER: ... target not in any function"
UNRESOLVED = re.compile(
    r"0x([0-9A-Fa-f]+) from 0x[0-9A-Fa-f]+.*?target not in any function")


def _read_text(path):
    data = open(path, "rb").read()
    if b"\x00" in data[:64]:
        try:
            return data.decode("utf-16")
        except Exception:
            pass
    return data.decode("utf-8", "ignore")


def func_grid(gen_dir):
    """Per-file [(start_line, addr)] and the global sorted function starts."""
    per_file, starts = {}, set()
    for fp in glob.glob(os.path.join(gen_dir, "*.cpp")):
        rows = []
        with open(fp, "r", errors="ignore") as f:
            for i, line in enumerate(f, 1):
                m = DEFRE.search(line)
                if m:
                    a = int(m.group(1), 16)
                    rows.append((i, a))
                    starts.add(a)
        per_file[os.path.basename(fp)] = rows
    return per_file, sorted(starts)


def _func_at(rows, line):
    best = None
    for ln, a in rows:
        if ln <= line:
            best = a
        else:
            break
    return best


def load_overrides_full(toml_path):
    """addr -> {"end", "parent", "size", "name"} (each None if absent). Lossless for the
    [functions] entries -- preserves chunk `parent` links and custom names that the
    end-only loader used to silently drop (which would split those functions on rewrite)."""
    ov = {}
    if os.path.exists(toml_path):
        txt = _read_text(toml_path)
        fm = re.search(r'\[functions\](.*)', txt, re.S)   # ignore [meta] etc.
        body_all = fm.group(1) if fm else txt
        for m in re.finditer(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{([^}]*)\}', body_all):
            a = int(m.group(1), 16)
            b = m.group(2)

            def _hex(key, body=b):
                mm = re.search(key + r'\s*=\s*0x([0-9A-Fa-f]+)', body)
                return int(mm.group(1), 16) if mm else None
            nm = re.search(r'name\s*=\s*"([^"]*)"', b)
            ov[a] = {"end": _hex("end"), "parent": _hex("parent"),
                     "size": _hex("size"), "name": nm.group(1) if nm else None}
    return ov


def _fmt_entry(attrs):
    parts = []
    if attrs.get("size"):
        parts.append("size = 0x%X" % attrs["size"])
    if attrs.get("end"):
        parts.append("end = 0x%X" % attrs["end"])
    if attrs.get("parent"):
        parts.append("parent = 0x%X" % attrs["parent"])
    if attrs.get("name"):
        parts.append('name = "%s"' % attrs["name"])
    return "{ %s }" % ", ".join(parts) if parts else "{}"


def clamp_overlapping_ends(full):
    """Make the config satisfy the two invariants rexglue enforces.

    rexglue refuses the whole config on the first overlap ("Overlapping
    boundaries: 0x82AA6268+0x238 overlaps 0x82AA6270+0x230") and codegen aborts,
    so one bad entry costs the entire title.

    Two shapes, two repairs, and telling them apart matters:

    * NESTED -- A starts before B and ends at or after B's end. That is one
      routine with several entry points, not two routines: Forza Horizon had
      0x82AA6268/6270/62A8/62FC all ending at 0x82AA64A0. Keep the outermost
      span and make the inner ones CHUNKS of it. Shrinking them instead (which
      this function used to do) produces a config that loads and is wrong -- the
      chunks further in, like 0x82AA637C, end up with no owner and codegen dies
      in its Write phase on "Unresolved conditional branch".
    * PARTIAL -- A ends inside B but past B's start without containing it. There
      is no reading of that as nesting, so clip A to B's start.

    Chunks (`parent = ...`) are not boundaries here: a chunk is SUPPOSED to sit
    inside its parent, and treating one as a boundary would undo every chunk cure.

    Returns the number of entries changed.
    """
    changed = 0
    while True:
        owners = sorted(a for a, v in full.items() if not v.get("parent"))
        hit = False
        for i, a in enumerate(owners):
            end_a = full[a].get("end")
            if not end_a or i + 1 >= len(owners):
                continue
            b = owners[i + 1]
            if end_a <= b:
                continue
            end_b = full[b].get("end")
            if end_b is None or end_a >= end_b:
                # nested: b is an entry point inside a. Anything that hung off b
                # moves up to a as well -- the SDK resolves `parent` as a
                # FUNCTION, and a chunk whose parent is itself a chunk killed
                # codegen in its Write phase with a bare C++ exception.
                for c, cv in full.items():
                    if cv.get("parent") == b:
                        cv["parent"] = a
                full[b]["parent"] = a
                full[b]["end"] = None
            else:
                full[a]["end"] = b
            changed += 1
            hit = True
            break          # the owner set just changed; re-derive it
        if not hit:
            break

    # Second invariant: a chunk has to sit inside its parent. Re-homing one is
    # cheap; leaving it outside is not rejected by rexglue, it just stops meaning
    # anything, which is worse than an error.
    owners = sorted(a for a, v in full.items() if not v.get("parent"))
    for a, v in full.items():
        parent = v.get("parent")
        if not parent:
            continue
        pend = full.get(parent, {}).get("end")
        if parent < a and (pend is None or a < pend):
            continue
        owner = None
        for o in owners:
            if o >= a:
                break
            oend = full[o].get("end")
            if oend is None or a < oend:
                owner = o
        if owner is not None and owner != parent:
            v["parent"] = owner
            changed += 1
    return changed


def write_overrides_full(toml_path, ov):
    """Write addr -> {end,parent,size,name} losslessly. Preserves any [meta] block."""
    meta = ""
    if os.path.exists(toml_path):
        mm = re.search(r'(\[meta\].*?)\n\[functions\]', _read_text(toml_path), re.S)
        if mm:
            # _read_text reads binary, so a CRLF source file keeps its \r here; the
            # text-mode write below would then turn each \r\n into \r\r\n and break the
            # TOML parse. Normalise the carried-over [meta] block to \n first.
            meta = mm.group(1).rstrip().replace("\r\n", "\n").replace("\r", "\n") + "\n\n"
    header = ("# Boundary/function overrides auto-healed by rexauto.\n"
              "# `end` = extend a function the recompiler split mid-flow;\n"
              "# `parent` = a chunk (address-taken sub-entry) of a parent function;\n"
              "# `{}`  = a function discovered at runtime by the heal loop.\n\n")
    # Never hand rexglue a config it will reject: one overlapping boundary makes
    # it refuse the file outright and the title loses its whole cure set.
    clamp_overlapping_ends(ov)
    out = header + meta + "[functions]\n"
    for a in sorted(ov):
        out += '"0x%08X" = %s\n' % (a, _fmt_entry(ov[a]))
    open(toml_path, "w").write(out)


def load_overrides(toml_path):
    """Back-compat: addr -> end (or None). `parent`/`name`/`size` stay on disk and are
    preserved across writes -- see write_overrides."""
    return {a: v["end"] for a, v in load_overrides_full(toml_path).items()}


def write_overrides(toml_path, ov):
    """Back-compat for end-only callers. Merges the given {addr: end_or_None} onto the
    on-disk full set so chunk `parent` links (and names) are never dropped."""
    full = load_overrides_full(toml_path)
    for a, end in ov.items():
        attrs = full.get(a) or {"end": None, "parent": None, "size": None, "name": None}
        attrs["end"] = end
        full[a] = attrs
    write_overrides_full(toml_path, full)


def externally_called(gen_dir, addrs, owner):
    """Of `addrs`, the ones some OTHER routine calls as sub_X.

    An absorbed address becomes a chunk, and codegen then lowers a branch from
    the parent into it as `goto loc_X` instead of a call -- which is right for a
    sub-entry, except the label never gets emitted and the goto dangles forever.
    Forza Horizon's 0x82AA6444 is the case: `bdz 0x82aa6444` came out as
    `goto loc_82AA6444` once it was a chunk, while `beq cr6,0x82aa6424` one line
    above came out as `sub_82AA6424(ctx, base); return;`.

    An address with callers elsewhere is a function whatever else it also is, so
    it must never be absorbed -- 12 sites call sub_82AA6444 from another
    translation unit. Keeping it a function is what makes codegen emit the call.
    """
    if not addrs:
        return set()
    import glob
    names = {"sub_%08X" % a: a for a in addrs}
    hits = set()
    own = "sub_%08X" % owner
    for f in glob.glob(os.path.join(gen_dir, "*.cpp")):
        try:
            txt = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for n, a in names.items():
            if a in hits:
                continue
            # the definition itself and the parent's own body do not count
            c = txt.count(n)
            if c and ("DEFINE_REX_FUNC(%s)" % n) in txt:
                c -= 1
            if c > 0 and own not in txt:
                hits.add(a)
    return hits


def retire_failed_landings(forced_path, dangling, state_no_force):
    """Stop forcing a landing that demonstrably did not take.

    A forced landing asks the SDK for `loc_X:` inside the enclosing routine. When
    the address ALSO carries a function-table entry the SDK sometimes emits both
    (Gears of War Judgment does, and its four landings work) and sometimes emits
    only the entry, leaving the `goto loc_X` dangling forever -- Forza Horizon's
    0x82AA6444. There is no structural way to tell which: across the fleet, `ca`
    has 42 clashes on chunks that all got their label and 289 on plain functions
    that all did not, which is the exact opposite of Gears.

    So decide on evidence instead of shape. If a label is still dangling on a
    round where its address is ALREADY in forced_landings, forcing it failed;
    retire it and let codegen fall back to treating the target as a function, so
    the goto becomes a call. Retired addresses are remembered, otherwise the next
    round reads the same error and forces them straight back.

    Returns (retired_now, updated_no_force).
    """
    already = load_forced(forced_path)
    no_force = set(state_no_force or ())
    retired = sorted((set(dangling) & already) - no_force)
    if retired:
        write_forced(forced_path, sorted(already - set(retired)), replace=True)
        no_force |= set(retired)
    return retired, no_force


def heal_boundaries(build_log, gen_dir, toml_path, forced_path=None):
    """Add `end` extensions for every undeclared-label error. Returns count added.

    Everything happens against one in-memory config and lands in ONE write. That
    matters: write_overrides_full enforces the "a chunk lives inside its parent"
    invariant, so reparenting an entry onto F before F's end has grown makes the
    entry look out-of-span and it gets re-homed onto whatever function happened to
    cover it. Forza Horizon's landings ended up on 0x82AA5E84 that way instead of
    on the 0x82AA62FC they belong to.

    """
    txt = _read_text(build_log)
    errs = [(os.path.basename(m.group(1)), int(m.group(2)), int(m.group(3), 16))
            for m in UNDECL.finditer(txt)]
    if not errs:
        return 0
    per_file, starts = func_grid(gen_dir)
    full = load_overrides_full(toml_path)
    added = 0
    for fname, line, T in errs:
        rows = per_file.get(fname)
        if not rows:
            continue
        F = _func_at(rows, line)
        i = bisect.bisect_right(starts, T)
        if F is None or i >= len(starts):
            continue
        nextStart = starts[i]
        cur = (full.get(F) or {}).get("end")
        if cur is not None and cur >= nextStart:
            continue

        # Never reach across another registered entry. The SDK v0.10.0 discovers
        # jump-table landings as functions of their own (Forza Horizon's
        # 0x82AA62A8/62FC/6310/.../6470 are all DISCOVERED FunctionNodes), and a
        # function that spans one of them is an overlap rexglue refuses. Turning
        # them into chunks instead -- what this pass did earlier today -- is no
        # better: a chunk is emitted as its own sub_ and the `goto loc_` into it
        # never gets a label, so the build dangles forever. The landing cure is
        # the one that fits this SDK: a forced landing beside a discovered
        # function makes codegen emit both the label and the entry (that is how
        # Gears of War Judgment's four resolve). So when something sits between
        # F and the target, leave F alone and let that cure work.
        if any(F < a < nextStart and not full[a].get("parent") for a in full):
            continue
        entry = full.get(F) or {"end": None, "parent": None, "size": None, "name": None}
        entry["end"] = nextStart
        full[F] = entry
        added += 1

    write_overrides_full(toml_path, full)
    return added


def forced_landings_from_log(build_log):
    """Landing addresses from every "use of undeclared label 'loc_T'" compile error.
    A dangling goto is, by definition, an in-function jump-table landing the SDK's
    heuristic detectJumpTable under-recovered (an InternalLabel target with no block) --
    never a separate function -- so forcing the SDK to recover it as an in-function block
    is the safe, function-preserving fix (keeps a decompressor loop's back-edge intact)."""
    txt = _read_text(build_log)
    return sorted(set(int(m.group(3), 16) for m in UNDECL.finditer(txt)))


def unresolved_branches_from_runtime(txt):
    """Targets of runtime 'Unresolved call/branch from X to Y' fatals -- the
    codegen-baked class where a branch target is neither a discovered function nor a
    recovered landing, so codegen lowered it to REX_FATAL. The heal loop could never
    cure it (crash_mind_over_mutant sat through 4 identical runs).
    Cure = force the target as an in-function landing (never a {} split).

    codegen emits BOTH wordings -- "Unresolved call" for a bl and "Unresolved
    branch" for a b/bc. Matching only "call" made this cure dead code for every
    branch-class fatal: Gears of War Judgment died 0.7s into every launch on
    "Unresolved branch from 0x830B0F48 to 0x830AFE58" while the loop reported
    "converged ... (other stop - likely GPU/runtime)"."""
    return sorted({int(m, 16) for m in re.findall(
        r"Unresolved (?:call|branch) from 0x[0-9A-Fa-f]+ to 0x([0-9A-Fa-f]+)", txt)})


UNRESOLVED_GEN = re.compile(
    r'REX_FATAL\("Unresolved (?:call|branch) from 0x([0-9A-Fa-f]+) to 0x([0-9A-Fa-f]+)"')


def unresolved_branches_from_generated(gen_dir):
    """Targets of every unresolved call/branch trap BAKED INTO the generated sources.

    This class is decided at codegen time, not at runtime: rexglue emits a literal
    REX_FATAL("Unresolved branch from 0x%08X to 0x%08X") into the .cpp wherever a
    branch target is neither a discovered function nor a recovered landing. The
    runtime binary carries no such string -- it only executes what codegen wrote.

    So the whole set is knowable from `generated/` right after codegen, with no
    build, no launch and no crash. Harvesting it only from a runtime log (which is
    what unresolved_branches_from_runtime does) costs one build+launch+crash per
    trap and only ever finds the FIRST one the guest happens to reach."""
    out = set()
    if not os.path.isdir(gen_dir):
        return []
    for fn in sorted(os.listdir(gen_dir)):
        if not fn.endswith((".cpp", ".h")):
            continue
        for m in UNRESOLVED_GEN.finditer(_read_text(os.path.join(gen_dir, fn))):
            out.add(int(m.group(2), 16))
    return sorted(out)


def _emitted_symbols(gen_dir):
    """(function starts, loc_ labels) already present in generated/."""
    import os as _os
    defined, labels = set(), set()
    if not _os.path.isdir(gen_dir):
        return defined, labels
    for fn in _os.listdir(gen_dir):
        if not fn.endswith((".cpp", ".h")):
            continue
        t = _read_text(_os.path.join(gen_dir, fn))
        defined |= {int(a, 16) for a in
                    re.findall(r"DEFINE_REX_FUNC\(sub_([0-9A-Fa-f]+)\)", t)}
        labels |= {int(a, 16) for a in re.findall(r"\bloc_([0-9A-Fa-f]{8})\b", t)}
    return defined, labels


def label_owners(gen_dir):
    """loc_ label address -> the function that emits it.

    A data pointer whose target is only a `loc_` inside somebody else's body is
    still a real entry point: the game takes its address and calls through it.
    Skipping those is what made our pointer scan miss 40 of the 51 functions
    ReXGlue v0.10.0 finds on Dante's Inferno. They must not be registered as
    plain functions (that splits the owner), so the owner is recorded here and
    they are cured as chunks instead."""
    import os as _os
    owners = {}
    if not _os.path.isdir(gen_dir):
        return owners
    for fn in sorted(_os.listdir(gen_dir)):
        if not fn.endswith(".cpp"):
            continue
        cur = None
        with open(_os.path.join(gen_dir, fn), "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = DEFRE.search(line)
                if m:
                    cur = int(m.group(1), 16)
                    continue
                if cur is None:
                    continue
                lm = re.match(r"^loc_([0-9A-Fa-f]{8}):", line)
                if lm:
                    owners.setdefault(int(lm.group(1), 16), cur)
    return owners


def data_pointer_scan(image_path, gen_dir, image_base, code_base, code_size):
    """Function pointers the static scan missed, read out of the image's DATA.

    A vtable entry, a callback array or a jump-table-of-handlers is just a dword
    in a data section holding a code address. The recompiler's own scan follows
    control flow, so it never sees them, and the run-heal then finds them one at
    a time by launching the game and crashing on each -- on Gears of War Judgment
    35 of its 46 cures are sitting in the image in plain sight.

    Filters (the same shape hells-gate-recomp uses in ReXGlue's
    dataSectionFunctionPointerScan, which is where this idea comes from):
      - read only OUTSIDE the code range; scanning code itself is what makes this
        noisy (whole-image: 1,878 spurious candidates, data-only: 315)
      - 4-byte aligned value, not 0 and not 0xFFFFFFFF
      - value must land inside the code range
      - the target's own first instruction must not be 0x00000000/0xFFFFFFFF
        (alignment padding, not a function)

    Plus one rule of our own, which their SDK-side version does not need: drop
    anything already emitted as a `loc_` label. Inside an existing function body
    that address is a LANDING, and registering it as a function splits the
    routine -- the failure that made Judgment die 0.7s into every launch.

    Returns (new_functions, label_hits) -- the second being {addr: owner} for
    targets that are only a `loc_` inside another function, which are entries the
    game takes the address of and must be cured as CHUNKS, never as functions."""
    import array
    import sys as _sys
    import os as _os
    if not (image_path and _os.path.exists(image_path)) or not code_size:
        return []
    with open(image_path, "rb") as f:
        img = f.read()
    code_end = code_base + code_size
    words = array.array("I")
    words.frombytes(img[:len(img) - (len(img) % 4)])
    if _sys.byteorder == "little":
        words.byteswap()                      # image is big-endian
    lo_w = (code_base - image_base) // 4
    hi_w = (code_end - image_base) // 4
    defined, labels = _emitted_symbols(gen_dir)
    owners = label_owners(gen_dir)
    out, lab = set(), {}
    for i, v in enumerate(words):
        if lo_w <= i < hi_w:                  # skip the executable section
            continue
        if v == 0 or v == 0xFFFFFFFF or (v & 3):
            continue
        if not (code_base <= v < code_end):
            continue
        if v in defined:
            continue
        if v in labels:
            o = owners.get(v)
            if o is not None and o != v:
                lab[v] = o
            continue
        j = (v - image_base) // 4
        if j >= len(words):
            continue
        t = words[j]
        if t == 0 or t == 0xFFFFFFFF:         # padding, not code
            continue
        out.add(v)
    return sorted(out), lab


def load_forced(path):
    """Set of addresses in a `forced_landings = [..]` TOML (empty if absent)."""
    if not os.path.exists(path):
        return set()
    m = re.search(r"forced_landings\s*=\s*\[([^\]]*)\]", _read_text(path))
    return set(int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", m.group(1))) if m else set()


def write_forced(path, addrs, replace=False):
    """Merge addrs into the forced_landings TOML. Returns count newly added (0 => no
    change, so the file stays byte-identical on disk).

    `replace` writes exactly `addrs` instead of merging -- needed to REMOVE a
    landing, which merging can never do.
    """
    cur = load_forced(path)
    merged = set(addrs) if replace else (cur | set(addrs))
    if merged == cur and os.path.exists(path):
        return 0
    body = ", ".join("0x%08X" % a for a in sorted(merged))
    open(path, "w").write(
        "# Jump-table landings the heuristic detectJumpTable under-recovered -- forced to\n"
        "# be recovered as in-function blocks so build_bctr's `goto loc_T` resolves and the\n"
        "# enclosing routine stays whole. Auto-written by rexauto's undeclared-label heal.\n"
        "forced_landings = [%s]\n" % body)
    return len(merged) - len(cur)


def ensure_manifest_include(manifest_path, include_name):
    """Add include_name to the manifest's `includes = [..]` array if missing (idempotent).

    Refuses to declare a file that does not exist beside the manifest: rexglue
    treats a missing include as a hard manifest error ("included file not found")
    and codegen aborts before it starts. That is exactly what happened when the
    unresolved-branch heal cured its targets as plain functions (n_seed == 0, so
    no forced-landings file was ever written) and the include was added anyway."""
    if not os.path.exists(manifest_path):
        return
    if not os.path.exists(os.path.join(os.path.dirname(manifest_path), include_name)):
        return
    txt = _read_text(manifest_path)
    if include_name in txt:
        return
    m = re.search(r"(includes\s*=\s*\[)([^\]]*)(\])", txt)
    if not m:
        return
    items = m.group(2).rstrip()
    sep = ", " if items.strip() else ""
    new = m.group(1) + items + '%s"%s"' % (sep, include_name) + m.group(3)
    open(manifest_path, "w").write(txt[:m.start()] + new + txt[m.end():])


def extend_switch_table(addrs, switch_path, spans):
    """For each addr inside a function `end`-span that also contains a bctr switch table,
    add the addr to THAT table's labels. A runtime "invalid function" for an in-routine
    address is a jump-table landing the heuristic under-recovered: it hit the switch's
    `default: REX_CALL_INDIRECT_FUNC` because it was never a `case`. Adding it as a case
    makes build_bctr lower `case 0xA: goto loc_A;` (paired with a forced_landings loc_).
    `spans` = [(start,end)] of end-override routines. Returns count of labels added.

    DISABLED for ReXGlue v0.10.0, which is the floor now. That SDK lowers a
    recovered table as `switch (index) { case i: goto labels[i]; }` -- the
    labels array is POSITIONAL, duplicates included -- where 0.8.2 keyed the
    cases on the computed CTR value. Merging a landing in as a sorted, deduped
    set (what this did) silently re-pointed every case after the first change:
    on Forza Horizon it rewrote 10 of 325 tables, dropped 65 duplicate slots and
    the exe dispatched jump tables to the wrong blocks. A landing the table does
    not cover is reached through the SDK's own `default:` indirect dispatch once
    it is a forced landing / registered function, so nothing is lost by leaving
    the table exactly as IDA recovered it."""
    return 0
    if not addrs or not os.path.exists(switch_path):
        return 0
    txt = _read_text(switch_path)
    added = 0
    # walk each [[switch_tables]] block: capture its bctr `address` and `labels` array
    blocks = list(re.finditer(
        r'(\[\[switch_tables\]\].*?address\s*=\s*0x([0-9A-Fa-f]+).*?labels\s*=\s*)\[([^\]]*)\]',
        txt, re.S))
    for m in reversed(blocks):                       # reversed so earlier spans stay valid
        bctr = int(m.group(2), 16)
        routine = next(((s, e) for (s, e) in spans if s <= bctr < e), None)
        if not routine:
            continue
        s, e = routine
        cur = [int(x, 16) for x in re.findall(r'0x[0-9A-Fa-f]+', m.group(3))]
        want = [a for a in addrs if s <= a < e and a not in cur]
        if not want:
            continue
        merged = sorted(set(cur) | set(want))
        added += len(merged) - len(cur)
        body = ", ".join("0x%08X" % a for a in merged)
        txt = txt[:m.start()] + m.group(1) + "[" + body + "]" + txt[m.end():]
    if added:
        open(switch_path, "w").write(txt)
    return added


def register_or_seed(addrs, toml_path, forced_path, switch_path=None, called=False):
    """Partition unregistered-function addresses. Any addr that falls INSIDE an existing
    function's `end`-override span is a computed-goto/jump-table LANDING of that routine
    (an indirect target the runtime reached but the static scan left uncovered) -- route
    it to forced_landings so it becomes an in-function block, keeping the routine WHOLE.
    Registering it as a standalone {} instead would SPLIT the routine, and any internal
    loop-back branch into the parent (e.g. a decompressor's `blt -> loc_head`) then
    lowers to REX_FATAL("Unresolved branch") -- a crash the play-and-heal loop can never
    fix (it only heals "invalid function", not "unresolved branch"). Everything else is a
    genuine new function -> {} registration. Returns (n_registered, n_seeded)."""
    full = load_overrides_full(toml_path)
    spans = [(a, v["end"]) for a, v in full.items() if v.get("end")]

    def in_routine(x):
        return any(s <= x < e for s, e in spans)

    def owner_of(x):
        for s, e in spans:
            if s <= x < e:
                return s
        return None

    # A target inside another function's span is a LANDING only if control got
    # there by a branch. When the runtime says "Call to invalid or unregistered
    # function", it was CALLED -- and a forced landing can never satisfy a call:
    # it emits a `loc_` label, not a function-table entry, so the dispatcher keeps
    # rejecting the same address forever ("already registered but still flagged",
    # Dante's Inferno 0x829083F0 inside 0x82908284). The cure for a called
    # interior address is a CHUNK: `{ parent = <owner> }` gives classifyTarget a
    # chunkParent, so the call lowers to a real entry without splitting the owner.
    if called:
        chunks = [(a, owner_of(a)) for a in addrs if in_routine(a)]
        if chunks:
            full = load_overrides_full(toml_path)
            for a, owner in chunks:
                attrs = full.get(a) or {"end": None, "parent": None,
                                        "size": None, "name": None}
                if attrs.get("parent") is None:
                    attrs["parent"] = owner
                    full[a] = attrs
            write_overrides_full(toml_path, full)
        funcs = [a for a in addrs if not in_routine(a)]
        return register_functions(funcs, toml_path) + len(chunks), 0

    landings = sorted(a for a in addrs if in_routine(a))
    funcs = [a for a in addrs if not in_routine(a)]
    n_reg = register_functions(funcs, toml_path)
    # A landing needs BOTH the switch `case` (so the routine's bctr dispatches to it
    # instead of falling to `default: REX_CALL_INDIRECT_FUNC`) and the `loc_` block
    # (so `case 0xA: goto loc_A;` has a target). Add both; either being new is progress.
    n_case = extend_switch_table(landings, switch_path, spans) if (landings and switch_path) else 0
    n_seed = write_forced(forced_path, landings) if landings else 0
    return n_reg, n_seed + n_case


def invalid_functions_from_text(txt):
    """Distinct guest addresses the dispatcher flagged as unregistered."""
    return sorted(set(int(m.group(1), 16) for m in INVALID.finditer(txt)))


def invalid_functions_ordered(txt):
    """Like invalid_functions_from_text but preserving FIRST-OCCURRENCE order.
    In a discover-mode run everything logged after the first no-op'd uncurable
    call may be post-corruption garbage, so chronology picks honest exemplars."""
    return list(dict.fromkeys(int(m.group(1), 16) for m in INVALID.finditer(txt)))


def unresolved_calls_from_text(txt):
    """Tail-call targets codegen's Validate phase couldn't place in a function."""
    return sorted(set(int(m.group(1), 16) for m in UNRESOLVED.finditer(txt)))


def invalid_functions(run_log):
    return invalid_functions_from_text(_read_text(run_log))


def register_functions(addrs, toml_path):
    """Add bare `{}` registrations for addrs not already present. Returns count."""
    ov = load_overrides(toml_path)
    added = 0
    for a in addrs:
        if a not in ov:
            ov[a] = None
            added += 1
    if added:
        write_overrides(toml_path, ov)
    return added


# --- static dangling-label check ----------------------------------------------
_GOTO_RE = re.compile(r"\bgoto loc_([0-9A-Fa-f]{8})\b")
_LABEL_RE = re.compile(r"^\s*loc_([0-9A-Fa-f]{8}):")
_ANYDEF_RE = re.compile(r"DEFINE_REX_FUNC\(")


def dangling_gotos(gen_dir):
    """[(abs_cpp_path, line_no, target)] for every `goto loc_T` whose label is
    not declared in the SAME function body -- exactly what clang later reports
    as "use of undeclared label", found in seconds instead of after a full
    compile. C++ labels are function-scoped, so the check is per
    DEFINE_REX_FUNC body. Empty list => the build cannot fail on labels."""
    out = []
    if not os.path.isdir(gen_dir):
        return out
    for fp in sorted(glob.glob(os.path.join(gen_dir, "*.cpp"))):
        with open(fp, "r", errors="ignore") as f:
            lines = f.readlines()
        # split into function bodies
        bounds = [i for i, l in enumerate(lines) if _ANYDEF_RE.search(l)]
        bounds.append(len(lines))
        for bi in range(len(bounds) - 1):
            s, e = bounds[bi], bounds[bi + 1]
            labels, gotos = set(), []
            for i in range(s, e):
                l = lines[i]
                m = _LABEL_RE.match(l)
                if m:
                    labels.add(m.group(1).upper())
                if "goto loc_" in l:
                    for g in _GOTO_RE.finditer(l):
                        gotos.append((i + 1, g.group(1).upper()))
            for ln, t in gotos:
                if t not in labels:
                    out.append((fp, ln, int(t, 16)))
    return out


def write_synthetic_label_log(gen_dir, path):
    """Write clang-shaped "use of undeclared label" lines for every dangling
    goto in gen_dir so the existing heal (UNDECL regex, heal_boundaries,
    forced_landings_from_log) runs on it unchanged. Returns the count (0 =>
    nothing written, file removed)."""
    errs = dangling_gotos(gen_dir)
    if not errs:
        try:
            os.remove(path)
        except OSError:
            pass
        return 0
    with open(path, "w", encoding="utf-8") as f:
        for fp, ln, t in errs:
            f.write("%s:%d:24: error: use of undeclared label 'loc_%08X'\n" % (fp, ln, t))
    return len(errs)


def rewrite_dangling_gotos(gen_dir, log=lambda m: None):
    """LAST-RESORT safety net so a build can never die on 'use of undeclared
    label': every `goto loc_T` whose label is not in the same function body is
    rewritten in place to `{ REX_CALL_INDIRECT_FUNC(0xT); return; }` -- a tail
    call through the runtime dispatcher, which is exactly what a branch into
    another routine's body is (and what the SDK itself emits for a
    TargetKind::Function target). If T is not registered the dispatcher
    reports it at runtime and the run-heal registers it -- one launch, not a
    3-hour compile per round. Returns the number of rewritten sites."""
    errs = dangling_gotos(gen_dir)
    if not errs:
        return 0
    by_file = {}
    for fp, ln, t in errs:
        by_file.setdefault(fp, []).append((ln, t))
    n = 0
    for fp, sites in by_file.items():
        with open(fp, "r", errors="ignore") as f:
            lines = f.readlines()
        for ln, t in sites:
            i = ln - 1
            if i >= len(lines):
                continue
            pat = re.compile(r"goto loc_%08X\s*;" % t, re.I)
            new, k = pat.subn("{ REX_CALL_INDIRECT_FUNC(0x%08Xu); return; } "
                              "/* rexauto: dangling label -> tail call */" % t, lines[i])
            if k:
                lines[i] = new
                n += k
        with open(fp, "w", newline="") as f:
            f.writelines(lines)
    if n:
        log("  dangling-label safety net: %d goto(s) into other functions rewritten "
            "as dispatcher tail calls (%s)" % (
                n, ", ".join(sorted({"0x%08X" % t for _, _, t in errs})[:8])
                + (" ..." if len(errs) > 8 else "")))
    return n
