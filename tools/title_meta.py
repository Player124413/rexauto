#!/usr/bin/env python3
"""Emit title metadata for the Android build: name, title_id, cover.

    python tools/title_meta.py <default.xex|container> <out_dir>

Writes <out_dir>/cover.png (best-effort: STFS thumbnail or XboxUnity tile)
and prints/exports (GITHUB_OUTPUT) title=..., title_id=..., slug=...
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import extract as _extract  # noqa: E402


def main():
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    meta = {}
    try:
        meta = _extract.read_package_meta(src) or {}
    except Exception:
        pass
    xex = src if os.path.isfile(src) and _extract._looks_like_xex(src) else None
    if not xex and os.path.isdir(src):
        xex = _extract._find_default_xex(src)
    tid = (meta.get("title_id") or "").upper()
    if not tid and xex:
        try:
            tid = (_extract._xex_title_id(open(xex, "rb").read(0x4000)) or "").upper()
        except Exception:
            pass
    title = meta.get("title")
    db = {}
    try:
        db = json.load(open(os.path.join(ROOT, "tools", "xbox360_titles.json"), encoding="utf-8"))
    except Exception:
        pass
    if tid and db.get(tid):
        title = db[tid]  # the DB name beats a file-name guess
    if not title or title.lower() in ("game", "default"):
        title = _extract.title_from_filename(src) or "Xbox 360 Game"
    cover = meta.get("cover")
    if not cover and tid:
        try:
            cover = _extract.fetch_title_icon(tid)
        except Exception:
            cover = None
    if cover:
        open(os.path.join(out, "cover.png"), "wb").write(cover)
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or "game"
    res = {"title": title, "title_id": tid or "00000000", "slug": slug, "cover": bool(cover)}
    print(json.dumps(res, ensure_ascii=False))
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            for k, v in res.items():
                f.write("%s=%s\n" % (k, v))


if __name__ == "__main__":
    main()
