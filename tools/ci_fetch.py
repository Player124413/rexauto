#!/usr/bin/env python3
"""Fetch the game input for the one-click GitHub Actions workflow.

    python tools/ci_fetch.py <url> <dest_dir>

Accepts anything a phone can hand over as a link:
  * a direct URL to default.xex, an .iso, a GoD/STFS package or an archive
  * a Google Drive share link (any of the usual shapes) -- via gdown
  * a Dropbox share link (dl=0 is rewritten to dl=1)
  * .zip / .7z / .rar archives -> extracted; the default.xex (or ISO) inside
    is what the pipeline gets

Prints the container path rexauto.py should receive on the last stdout line
and writes it to $GITHUB_OUTPUT (container=...) when running in Actions.
"""
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

UA = "rexauto-ci/1.0"
ARCHIVE_EXT = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")
CONTAINER_EXT = (".iso", ".xex", ".xexp", ".bin", ".img")
XEX_MAGIC = b"XEX2"


def log(msg):
    print("[ci_fetch] " + msg, flush=True)


# ---------------------------------------------------------------- downloading
def _gdrive_id(url):
    u = urllib.parse.urlparse(url)
    if "drive.google.com" not in u.netloc and "docs.google.com" not in u.netloc:
        return None
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", u.path)
    if m:
        return m.group(1)
    q = urllib.parse.parse_qs(u.query)
    if "id" in q:
        return q["id"][0]
    return None


def _filename_from_response(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?\"?([^\";]+)", cd, re.I) or \
        re.search(r'filename="?([^";]+)"?', cd, re.I)
    if m:
        return os.path.basename(urllib.parse.unquote(m.group(1).strip()))
    name = os.path.basename(urllib.parse.urlparse(url).path)
    return urllib.parse.unquote(name) or "download.bin"


def download(url, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    gid = _gdrive_id(url)
    if gid:
        log("Google Drive link -> gdown (id=%s)" % gid)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "gdown"])
        import gdown  # noqa: E402
        out = gdown.download(id=gid, output=os.path.join(dest_dir, ""), quiet=False, fuzzy=True)
        if not out or not os.path.isfile(out):
            raise SystemExit("gdown could not fetch the file -- is the link shared as "
                             "'Anyone with the link'?")
        return out
    if "dropbox.com" in url:
        url = re.sub(r"[?&]dl=0", lambda m: m.group(0)[0] + "dl=1", url)
        if "dl=1" not in url:
            url += ("&" if "?" in url else "?") + "dl=1"
    if "mega.nz" in url or "mega.co.nz" in url:
        log("MEGA link -> megatools not available on the runner; trying mega.py")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "mega.py"])
        from mega import Mega  # noqa: E402
        out = Mega().login().download_url(url, dest_dir)
        return str(out)
    log("GET " + url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp:
        name = _filename_from_response(resp, url)
        path = os.path.join(dest_dir, name)
        total = int(resp.headers.get("Content-Length") or 0)
        got, last = 0, -1
        with open(path, "wb") as f:
            while True:
                chunk = resp.read(1 << 22)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    if pct // 10 != last:
                        last = pct // 10
                        log("  %d%% (%.1f / %.1f MB)" % (pct, got / 1e6, total / 1e6))
        log("saved %s (%.1f MB)" % (path, got / 1e6))
    if got < 1024:
        head = open(path, "rb").read(512)
        if b"<html" in head.lower() or b"<!doctype" in head.lower():
            raise SystemExit("the link returned an HTML page, not a file -- use a DIRECT "
                             "download link (or a Google Drive / Dropbox share link)")
    return path


# ---------------------------------------------------------------- unpacking
def _sevenzip():
    for c in ("7z", r"C:\Program Files\7-Zip\7z.exe", "7za", "7zr"):
        p = shutil.which(c) or (c if os.path.isfile(c) else None)
        if p:
            return p
    return None


def unpack(path, dest_dir):
    low = path.lower()
    if not low.endswith(ARCHIVE_EXT):
        return None
    out = os.path.join(dest_dir, "unpacked")
    os.makedirs(out, exist_ok=True)
    log("archive -> extracting to %s" % out)
    if low.endswith(".zip") and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(out)
    else:
        sz = _sevenzip()
        if not sz:
            raise SystemExit("no 7z available to unpack %s" % path)
        subprocess.check_call([sz, "x", "-y", "-o" + out, path])
    return out


# ---------------------------------------------------------------- selection
def _is_xex(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == XEX_MAGIC
    except OSError:
        return False


def pick_container(root):
    """Return the path rexauto.py should get: a folder containing default.xex,
    an ISO/GoD/STFS file, or the folder itself."""
    if os.path.isfile(root):
        if _is_xex(root):
            folder = os.path.dirname(root)
            want = os.path.join(folder, "default.xex")
            if os.path.abspath(root) != os.path.abspath(want):
                log("renaming %s -> default.xex" % os.path.basename(root))
                shutil.move(root, want)
            return folder
        return root  # iso / GoD / STFS: extract.py sniffs the format
    xexes, isos, others = [], [], []
    for r, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(r, fn)
            fl = fn.lower()
            if fl == "default.xex":
                xexes.append(p)
            elif fl.endswith(".iso"):
                isos.append(p)
            elif _is_xex(p) and not fl.endswith(".xexp"):
                others.append(p)
    if xexes:
        return os.path.dirname(sorted(xexes, key=len)[0])
    if isos:
        return sorted(isos, key=lambda p: -os.path.getsize(p))[0]
    if others:
        return pick_container(others[0])
    return root  # let extract.py decide (GoD folders etc.)


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    url, dest = sys.argv[1].strip(), os.path.abspath(sys.argv[2])
    if not url:
        raise SystemExit("empty URL")
    got = download(url, os.path.join(dest, "dl"))
    root = unpack(got, dest) or got
    container = pick_container(root)
    log("container for rexauto: %s" % container)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("container=%s\n" % container)
    print(container)


if __name__ == "__main__":
    main()
