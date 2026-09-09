#!/usr/bin/env python3
"""CI helper for the one-click workflow: install / verify the pinned ReXGlue SDK.

    python tools/ci_sdk.py url       -> prints the SDK zip URL for this release
    python tools/ci_sdk.py install   -> downloads + lays it out via gui/setup.py
    python tools/ci_sdk.py verify    -> detect_env() + SDK pin check, exits 1 on failure
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "gui"))


def _emit(ev):
    print("[setup:%s] %s" % (ev.get("level"), ev.get("text")), flush=True)


def cmd_url():
    import setup
    sys.stdout.write(setup.REXGLUE_URL)


def cmd_install():
    import setup
    ok = setup.install_rexglue(_emit)
    sys.exit(0 if ok else 1)


def cmd_verify():
    import rexauto
    import setup
    e = rexauto.detect_env()
    for k in ("rexglue", "sdk", "clang", "clangxx", "vcvars", "python", "jt_repo", "idat"):
        print("%-8s %s" % (k, e.get(k)))
    bad = rexauto.sdk_pin_mismatch(e) if setup.pin_enforced() else None
    if bad:
        print("::error::SDK pin mismatch on %s: got %s want %s" % (bad[0], bad[2][:16], bad[1][:16]))
        sys.exit(1)
    missing = [k for k in ("rexglue", "sdk", "clang", "clangxx", "vcvars") if not e.get(k)]
    if missing:
        print("::error::missing tools: %s" % ", ".join(missing))
        sys.exit(1)
    print("toolchain OK")


if __name__ == "__main__":
    {"url": cmd_url, "install": cmd_install, "verify": cmd_verify}[sys.argv[1]]()
