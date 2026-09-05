#!/usr/bin/env python3
"""gamepatches.py -- catalogo de patches da comunidade, casado com um port.

Fonte: github.com/xenia-canary/game-patches (patches para o emulador Xenia).
Cada arquivo `<TITLEID> - <Nome>.patch.toml` traz varios blocos [[patch]], e cada
um lista escritas na imagem do jogo ([[patch.be32]], [[patch.be8]], ...).

O PONTO DELICADO, e a razao deste modulo existir: o rexauto e um recompilador
ESTATICO. Uma escrita que cai em .text altera INSTRUCAO -- ela precisa entrar
antes do codegen (vira codigo nativo compilado) e NAO pode ser ligada/desligada
em runtime. Uma escrita que cai em dados pode, em tese, ser aplicada em runtime.
Prometer um toggle instantaneo para a primeira classe seria mentir para o
usuario, entao cada patch recebe um SELO:

  RECOMPILAR -- tem ao menos uma escrita em .text; exige rebuild do port
  RUNTIME    -- todas as escritas caem fora de .text

O selo do patch e sempre o PIOR selo entre as suas escritas.

Uso:
    python gamepatches.py list <port_dir>            # relatorio
    python gamepatches.py apply <port_dir> <nome>... # gera o _guest_patches.toml
"""
import glob
import json
import io
import os
import re
import struct
import sys
import urllib.request

CATALOG_API = "https://api.github.com/repos/xenia-canary/game-patches/contents/patches"
RAW_BASE = "https://raw.githubusercontent.com/xenia-canary/game-patches/main/patches/"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "gamepatches")

# larguras suportadas pelo formato da comunidade -> bytes
WIDTHS = {"be8": 1, "be16": 2, "be32": 4, "be64": 8, "f32": 4, "f64": 8}
# as que carregam ponto flutuante: o "value" e um double no TOML, nao inteiro
FLOATS = {"f32": ">f", "f64": ">d"}


def _pack(kind, value):
    """Bytes big-endian de uma escrita, do jeito que aparecem na imagem."""
    if kind in FLOATS:
        return struct.pack(FLOATS[kind], float(value))
    return int(value).to_bytes(WIDTHS[kind], "big")


# --------------------------------------------------------------------------
# identificacao do port
# --------------------------------------------------------------------------
def _xex_title_id(head):
    """title_id (8 hex) do header XEX2 -- mesma logica de extract.py:_xex_title_id."""
    if head[:4] != b"XEX2":
        return None
    try:
        cnt = struct.unpack_from(">I", head, 0x14)[0]
        if cnt > 4096:
            return None
        for i in range(cnt):
            key, val = struct.unpack_from(">II", head, 0x18 + i * 8)
            if key == 0x00040006 and val + 0x10 <= len(head):
                return "%08X" % struct.unpack_from(">I", head, val + 0x0C)[0]
    except Exception:
        return None
    return None


def _module_info(port_dir, man_path):
    """Um modulo do port: nome, faixa de codigo e faixa de imagem.

    O out_directory_path vem do PROPRIO manifesto. Assumir "generated/default"
    quebrava calado em todo port multi-modulo: o Spider-Man gera em
    generated/gamelogic e generated/default, e a busca ia procurar o init.h do
    gamelogic dentro de default.
    """
    name = os.path.basename(man_path)[: -len("_manifest.toml")]
    # Le os campos do manifesto com regex em vez de um parser TOML: o tomllib so
    # existe no Python 3.11+, e o rexauto e congelado a partir do 3.10 --
    # importa-lo aqui deixava o modulo inteiro inalcancavel nesta maquina.
    try:
        man_txt = open(man_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None

    def field(key):
        m = re.search(key + r'\s*=\s*"([^"]*)"', man_txt)
        return m.group(1) if m else None

    out_dir = field("out_directory_path") or ("generated/" + name)
    # Os defines nao estao sempre no mesmo header: a ReXGlue 0.8.2 os poe em
    # <name>_init.h e a v0.10.0 em <name>_pch.h. Fixar um nome so fazia este
    # modulo desistir calado ("nenhum modulo utilizavel") em todo port gerado
    # pelo SDK novo. Mesma ordem de busca de rexauto._codegen_ranges.
    gen_dir = os.path.join(port_dir, *out_dir.split("/"))
    cb = cs = ib = None
    for pat in (name + "_init.h", name + "_pch.h", "*.h"):
        for h in sorted(glob.glob(os.path.join(gen_dir, pat))):
            try:
                txt = open(h, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue

            def g(key):
                m = re.search(key + r"\s+0x([0-9A-Fa-f]+)", txt)
                return int(m.group(1), 16) if m else None

            cb, cs, ib = g("REX_CODE_BASE"), g("REX_CODE_SIZE"), g("REX_IMAGE_BASE")
            if None not in (cb, cs, ib):
                break
        if None not in (cb, cs, ib):
            break
    if None in (cb, cs, ib):
        return None
    img = os.path.join(os.path.dirname(port_dir.rstrip("\\/")), name + "_image.bin")
    size = os.path.getsize(img) if os.path.exists(img) else 0x10000000
    src = field("file_path") or ""
    return {"name": name, "manifest": man_path, "out_dir": out_dir, "image": img,
            "code_base": cb, "code_size": cs, "code_end": cb + cs,
            "image_base": ib, "image_end": ib + size,
            "source": src, "is_xex": src.lower().endswith(".xex")}


def port_modules(port_dir):
    """Todos os modulos do port, o executavel principal primeiro."""
    mods = []
    for f in sorted(os.listdir(port_dir)):
        if f.endswith("_manifest.toml"):
            mi = _module_info(port_dir, os.path.join(port_dir, f))
            if mi:
                mods.append(mi)
    if not mods:
        raise SystemExit("nenhum modulo utilizavel em %s -- rode o codegen antes" % port_dir)
    mods.sort(key=lambda m: (not m["is_xex"], m["name"]))
    return mods


def owning_module(mods, addr):
    """Qual modulo contem este endereco guest, se algum."""
    for m in mods:
        if m["image_base"] <= addr < m["image_end"]:
            return m
    return None


def port_identity(port_dir):
    """(nome do modulo principal, title_id, [modulos])."""
    mods = port_modules(port_dir)
    main = mods[0]
    xex = os.path.normpath(os.path.join(port_dir, "..", "game", "default.xex"))
    tid = None
    if os.path.exists(xex):
        with open(xex, "rb") as f:
            tid = _xex_title_id(f.read(0x10000))
    return main["name"], tid, mods


# --------------------------------------------------------------------------
# catalogo
# --------------------------------------------------------------------------
def _cached(path, fetch):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return open(path, encoding="utf-8", errors="ignore").read()
    body = fetch()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return body


def catalog_index():
    """[{name, download_url}] -- baixado sob demanda e cacheado (o repo nao tem
    LICENSE, entao nada e redistribuido junto do rexauto)."""
    def fetch():
        req = urllib.request.Request(CATALOG_API, headers={"User-Agent": "rexauto"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        return json.dumps([{"name": x["name"]} for x in data if x["name"].endswith(".patch.toml")])
    return json.loads(_cached(os.path.join(CACHE, "index.json"), fetch))


def _port_has_tu(port_dir):
    """O port esta em title update? (existe .xexp estagiado no game dir)"""
    game = os.path.normpath(os.path.join(port_dir, "..", "game"))
    for root, _, files in os.walk(game):
        for f in files:
            if f.lower().endswith(".xexp"):
                return True
    return False


def files_for_title(tid, port_dir=None):
    """Arquivos de patch do titulo, JA FILTRADOS pela variante certa.

    O catalogo traz variantes por build -- "<TID> - Nome.patch.toml" (base) e
    "<TID> - Nome (TU4).patch.toml" (title update). Os enderecos sao TOTALMENTE
    diferentes entre elas: no Judgment o "Unlock FPS" e 0x8255DE08 na base e
    0x8255E220 no TU4. Aplicar a variante errada escreve em cima de instrucao
    aleatoria -- quebra silenciosa e dificil de rastrear. Por isso escolhemos UMA.

    Criterio definitivo seria o hash do codigo (o que o Xenia usa); enquanto ele
    nao existe, usamos o sinal disponivel: port sem .xexp -> variante base."""
    idx = catalog_index()
    hits = [x["name"] for x in idx if x["name"].upper().startswith(tid.upper())]
    if len(hits) <= 1 or port_dir is None:
        return hits
    has_tu = _port_has_tu(port_dir)
    tu_like = [h for h in hits if re.search(r"\(TU\d+\)", h)]
    base_like = [h for h in hits if h not in tu_like]
    chosen = (tu_like or hits) if has_tu else (base_like or hits)
    if len(chosen) == 1:
        return chosen
    # Still tied -- Forza Horizon publishes a retail file and an "(E3 Demo)" one,
    # and the .xexp signal says nothing about those. Refusing outright is what
    # made the screen show nothing at all for that title, so pick the plainest
    # name (a variant qualifies itself in parentheses; the retail build does not)
    # and let the caller override with `variant`. A saved choice always wins.
    saved = read_variant(port_dir)
    if saved and saved in chosen:
        return [saved]
    plain = sorted(chosen, key=lambda h: (h.count("("), len(h)))
    return [plain[0]]


def variants_for_title(tid, port_dir=None):
    """Every catalogue file for this title that could be the port's build, in the
    order the screen should offer them (the chosen one first). One entry means
    there is nothing to choose."""
    idx = catalog_index()
    hits = [x["name"] for x in idx if x["name"].upper().startswith(tid.upper())]
    if len(hits) <= 1 or port_dir is None:
        return hits
    has_tu = _port_has_tu(port_dir)
    tu_like = [h for h in hits if re.search(r"\(TU\d+\)", h)]
    base_like = [h for h in hits if h not in tu_like]
    plausible = (tu_like or hits) if has_tu else (base_like or hits)
    if len(plausible) <= 1:
        return plausible
    picked = files_for_title(tid, port_dir)
    first = picked[0] if picked else plausible[0]
    return [first] + [h for h in plausible if h != first]


def _variant_file(port_dir):
    return os.path.join(port_dir, "patch_variant.txt")


def read_variant(port_dir):
    """The catalogue file the user picked for this port, or None."""
    try:
        v = io.open(_variant_file(port_dir), encoding="utf-8").read().strip()
    except OSError:
        return None
    return v or None


def write_variant(port_dir, name):
    """Remember the pick next to the port, so a later build applies the same file."""
    try:
        if name:
            io.open(_variant_file(port_dir), "w", encoding="utf-8").write(name + "\n")
        elif os.path.exists(_variant_file(port_dir)):
            os.remove(_variant_file(port_dir))
    except OSError:
        pass


def fetch_patch_file(fname):
    def fetch():
        url = RAW_BASE + urllib.request.quote(fname)
        req = urllib.request.Request(url, headers={"User-Agent": "rexauto"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
    return _cached(os.path.join(CACHE, safe), fetch)


# --------------------------------------------------------------------------
# parse + classificacao
# --------------------------------------------------------------------------
def parse_patches(text):
    """[{name, desc, author, enabled_upstream, writes:[{width,address,value}]}]"""
    out = []
    cur = None
    pending_width = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "[[patch]]":
            cur = {"name": None, "desc": "", "author": "", "enabled_upstream": False,
                   "writes": [], "unsupported": set()}
            out.append(cur)
            pending_width = None
            continue
        m = re.match(r"\[\[patch\.([a-z0-9]+)\]\]", line)
        if m:
            pending_width = m.group(1)
            if cur is not None and pending_width not in WIDTHS:
                # Um tipo de escrita que nao sabemos empacotar. Ignorar e seguir
                # seria pior do que parar: aplicar so PARTE de um patch muda o
                # jogo de um jeito que nem o autor nem o usuario pediram.
                cur["unsupported"].add(pending_width)
            continue
        if cur is None:
            continue
        m = re.match(r'(\w+)\s*=\s*(.+)', line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "name":
            cur["name"] = v.strip('"')
        elif k == "desc":
            cur["desc"] = v.strip('"')
        elif k == "author":
            cur["author"] = v.strip('"')
        elif k == "is_enabled":
            cur["enabled_upstream"] = v.lower().startswith("true")
        elif k == "address" and pending_width in WIDTHS:
            cur.setdefault("_addr", []).append(int(v, 0))
        elif k == "value" and pending_width in WIDTHS:
            addrs = cur.get("_addr") or []
            if addrs:
                try:
                    raw = float(v) if pending_width in FLOATS else int(v, 0)
                    data = _pack(pending_width, raw)
                except (ValueError, OverflowError, struct.error):
                    cur["unsupported"].add(pending_width)
                    addrs.pop(0)
                    continue
                cur["writes"].append({"width": WIDTHS[pending_width],
                                      "kind": pending_width,
                                      "address": addrs.pop(0),
                                      "data": data})
    for p in out:
        p.pop("_addr", None)
    return [p for p in out if p["name"]]


def classify(patch, mods):
    """Selo do patch + motivo. O selo e o PIOR entre as escritas.

    Uma escrita e avaliada contra o modulo que a CONTEM, nao contra o modulo
    principal. Com a checagem feita so no principal, uma escrita no .text de um
    modulo companheiro caia em "fora da imagem" e o patch saia selado RUNTIME --
    prometendo um toggle para algo que na verdade vira codigo nativo compilado.
    """
    if patch.get("unsupported"):
        return ("INVALIDO",
                "escrita de tipo nao suportado (%s) -- aplicar so parte do patch"
                % ", ".join(sorted(patch["unsupported"])), [])
    if not patch["writes"]:
        return "INVALIDO", "patch sem escritas", []
    reasons, touched, seal = [], [], "RUNTIME"
    for w in patch["writes"]:
        a = w["address"]
        m = owning_module(mods, a)
        if m is None:
            return "INVALIDO", "escrita 0x%08X fora de todas as imagens" % a, []
        if m not in touched:
            touched.append(m)
        if m["code_base"] <= a < m["code_end"]:
            seal = "RECOMPILAR"
            reasons.append("0x%08X em .text de %s" % (a, m["name"]))
    why = "; ".join(reasons[:3]) if reasons else "todas as escritas fora de .text"
    return seal, why, touched


# --------------------------------------------------------------------------
# comandos
# --------------------------------------------------------------------------
def variant_fit(fname, mods):
    """Quantas escritas de um arquivo do catalogo caem no .text DESTE port.

    Duas variantes do mesmo title_id (base, TU, demo) trazem os MESMOS patches em
    enderecos diferentes. Escolher a errada nao falha: ela nopeia instrucao valida
    em outro lugar, que e miscompile silencioso. Entao a ambiguidade reporta o
    ajuste medido e o operador escolhe -- adivinhar aqui seria o pior caminho.
    """
    try:
        patches = parse_patches(fetch_patch_file(fname))
    except Exception:
        return None
    n_in = n_out = n_total = 0
    for p in patches:
        for w in p["writes"]:
            n_total += 1
            m = owning_module(mods, w["address"])
            if m and m["code_base"] <= w["address"] < m["code_end"]:
                n_in += 1
            else:
                n_out += 1
    return {"file": fname, "patches": len(patches), "writes": n_total,
            "in_text": n_in, "outside_text": n_out}


def cmd_list(port_dir, only_file=None):
    name, tid, mods = port_identity(port_dir)
    print("port=%s  title_id=%s  %d modulo(s)" % (name, tid, len(mods)))
    for m in mods:
        print("  %-28s .text=0x%08X..0x%08X  imagem=0x%08X..0x%08X%s"
              % (m["name"], m["code_base"], m["code_end"],
                 m["image_base"], m["image_end"], "  <- xex" if m["is_xex"] else ""))
    if not tid:
        raise SystemExit("sem title_id (nao achei ../game/default.xex)")
    files = [only_file] if only_file else files_for_title(tid, port_dir)
    if not files:
        print("\nnenhum patch da comunidade para %s" % tid)
        return
    for fname in files:
        print("\n== %s" % fname)
        for p in parse_patches(fetch_patch_file(fname)):
            seal, why, touched = classify(p, mods)
            mods_txt = ",".join(m["name"] for m in touched)
            print("  [%-10s] %-34s %s" % (seal, p["name"][:34], why))
            if len(mods) > 1 and touched:
                print("               modulo(s): %s" % mods_txt)
            if p["desc"]:
                print("               %s" % p["desc"][:90])
            if p["author"]:
                print("               autor: %s" % p["author"])


def _write_guest_patches(port_dir, mod, entries):
    """Um arquivo de patches por MODULO, porque e por modulo que a imagem e
    parcheada antes da analise -- escrever tudo no manifesto principal faria as
    escritas do companheiro cairem num espaco de enderecos que nao e o delas.

    Emite [[image_patch]], que e o schema que o rexglue le. Cada bloco carrega
    tambem "expect": os bytes que a imagem tinha no momento da conversao. Se um
    dia o port for reconstruido de um dump de outra build do jogo, o codegen
    recusa em vez de nopear uma instrucao que nao e a que o autor do patch viu.
    """
    out = os.path.join(port_dir, mod["name"] + "_guest_patches.toml")
    img = b""
    img_path = mod.get("image")
    if img_path and os.path.exists(img_path):
        img = open(img_path, "rb").read()
    base = mod["image_base"]

    def expect_at(addr, n):
        off = addr - base
        if not img or off < 0 or off + n > len(img):
            return None
        return img[off:off + n]

    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Gerado por gamepatches.py -- patches da comunidade\n")
        f.write("# (github.com/xenia-canary/game-patches), aplicados na imagem guest\n")
        f.write("# ANTES da analise, entao viram codigo nativo permanente.\n")
        f.write("# modulo: %s\n" % mod["name"])
        if not img:
            f.write("# AVISO: a imagem nao estava disponivel, entao os blocos sairam\n"
                    "#        sem 'expect' -- o codegen aplica sem conferir a build.\n")
        n = 0
        for fname, p, seal, writes in entries:
            f.write("\n# [%s] %s\n" % (seal, p["name"]))
            if p["desc"]:
                f.write("#   %s\n" % p["desc"])
            f.write("#   autor: %s | fonte: %s\n" % (p["author"] or "?", fname))
            for w in writes:
                f.write("[[image_patch]]\n")
                f.write('name    = "%s"\n' % p["name"].replace('"', "'"))
                f.write("address = 0x%08X\n" % w["address"])
                f.write('data    = "%s"\n' % w["data"].hex().upper())
                exp = expect_at(w["address"], len(w["data"]))
                if exp is not None:
                    f.write('expect  = "%s"\n' % exp.hex().upper())
                n += 1
    return out, n


def _add_include(man, inc_name):
    txt = open(man, encoding="utf-8", errors="ignore").read()
    inc = '"%s"' % inc_name
    if inc in txt:
        return False
    m = re.search(r'(includes\s*=\s*\[)([^\]]*)\]', txt)
    if not m:
        raise SystemExit("nao achei 'includes' em %s" % man)
    txt = txt[:m.end(2)] + ", " + inc + txt[m.end(2):]
    open(man, "w", encoding="utf-8", newline="\n").write(txt)
    return True


# --------------------------------------------------------------------------
# API de biblioteca -- o que a GUI e o rexauto chamam
# --------------------------------------------------------------------------
def catalog(port_dir, only_file=None):
    """O catalogo em forma de dados, para quem nao e um terminal.

    Nunca levanta: quem chama e uma tela ou um build. Uma falha de rede ou um
    titulo sem patches nao pode derrubar nenhum dos dois -- vira "error" e a
    lista sai vazia.
    """
    out = {"name": None, "title_id": None, "modules": [], "patches": [],
           "files": [], "variants": [], "variant": None, "empty": False, "error": None}
    try:
        name, tid, mods = port_identity(port_dir)
    except SystemExit as e:
        out["error"] = str(e)
        return out
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    out["name"], out["title_id"] = name, tid
    out["modules"] = [{"name": m["name"], "code_base": m["code_base"],
                       "code_end": m["code_end"], "image_base": m["image_base"],
                       "image_end": m["image_end"]} for m in mods]
    if not tid:
        out["error"] = "sem title_id (nao achei ../game/default.xex)"
        return out
    try:
        files = [only_file] if only_file else files_for_title(tid, port_dir)
        variants = [only_file] if only_file else variants_for_title(tid, port_dir)
    except SystemExit as e:
        out["error"] = str(e)
        return out
    except Exception as e:
        out["error"] = "catalogo indisponivel: %s" % e
        return out
    out["files"] = list(files)
    out["variants"] = list(variants)
    out["variant"] = files[0] if files else None
    if not files:
        # Not an error: most titles simply have no published patches. Saying so
        # is the difference between an empty panel and an answer.
        out["empty"] = True
    applied = applied_names(port_dir)
    for fname in files:
        try:
            patches = parse_patches(fetch_patch_file(fname))
        except Exception as e:
            out["error"] = "nao consegui ler %s: %s" % (fname, e)
            continue
        for p in patches:
            seal, why, touched = classify(p, mods)
            out["patches"].append({
                "file": fname, "name": p["name"], "desc": p["desc"],
                "author": p["author"], "seal": seal, "why": why,
                "writes": len(p["writes"]),
                "modules": [m["name"] for m in touched],
                "applied": p["name"] in applied,
                "selectable": seal != "INVALIDO",
            })
    return out


def applied_names(port_dir):
    """Nomes dos patches ja gravados nos _guest_patches.toml do port.

    Lido do arquivo e nao de um estado paralelo: o arquivo E a verdade, e e ele
    que o codegen consome. Um marcador separado poderia divergir dele.
    """
    names = set()
    try:
        entries = os.listdir(port_dir)
    except OSError:
        return names
    for f in entries:
        if not f.endswith("_guest_patches.toml"):
            continue
        try:
            txt = open(os.path.join(port_dir, f), encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        names.update(re.findall(r'^name\s*=\s*"([^"]*)"', txt, re.M))
    return names


def _drop_include(man, inc_name):
    txt = open(man, encoding="utf-8", errors="ignore").read()
    new = re.sub(r',\s*"%s"' % re.escape(inc_name), "", txt)
    new = re.sub(r'"%s"\s*,\s*' % re.escape(inc_name), "", new)
    new = re.sub(r'\[\s*"%s"\s*\]' % re.escape(inc_name), "[]", new)
    if new == txt:
        return False
    open(man, "w", encoding="utf-8", newline="\n").write(new)
    return True


def clear(port_dir):
    """Desfaz tudo: apaga os arquivos de patch e tira os includes.

    Precisa existir porque desmarcar na tela tem de voltar o port ao estado sem
    patch -- deixar o arquivo e so tirar o include deixaria uma bomba armada
    para o proximo que reativasse o include a mao.
    """
    removed = []
    for mod in port_modules(port_dir):
        f = os.path.join(port_dir, mod["name"] + "_guest_patches.toml")
        if os.path.exists(f):
            os.remove(f)
            removed.append(os.path.basename(f))
        _drop_include(mod["manifest"], mod["name"] + "_guest_patches.toml")
    return removed


def apply(port_dir, wanted, only_file=None):
    """Grava os patches escolhidos e garante os includes. Devolve um resumo.

    Sempre parte do zero (clear antes): a lista marcada na tela e a verdade
    completa, entao aplicar por cima acumularia patches que o usuario desmarcou.
    """
    wanted = set(wanted or [])
    clear(port_dir)
    if not wanted:
        return {"patches": [], "writes": 0, "modules": 0, "needs_rebuild": False,
                "files": []}

    name, tid, mods = port_identity(port_dir)
    chosen = []
    for fname in ([only_file] if only_file else files_for_title(tid, port_dir)):
        for p in parse_patches(fetch_patch_file(fname)):
            if p["name"] in wanted:
                seal, why, touched = classify(p, mods)
                if seal == "INVALIDO":
                    raise ValueError("patch '%s' invalido: %s" % (p["name"], why))
                chosen.append((fname, p, seal))
    missing = wanted - {p["name"] for _, p, _ in chosen}
    if missing:
        raise ValueError("nao achei no catalogo: %s" % ", ".join(sorted(missing)))

    per_mod = {}
    for fname, p, seal in chosen:
        buckets = {}
        for w in p["writes"]:
            m = owning_module(mods, w["address"])
            buckets.setdefault(m["name"], []).append(w)
        for mname, writes in buckets.items():
            per_mod.setdefault(mname, []).append((fname, p, seal, writes))

    total, files = 0, []
    for mod in mods:
        entries = per_mod.get(mod["name"])
        if not entries:
            continue
        out, n = _write_guest_patches(port_dir, mod, entries)
        total += n
        _add_include(mod["manifest"], os.path.basename(out))
        files.append(os.path.basename(out))
    return {"patches": [p["name"] for _, p, _ in chosen], "writes": total,
            "modules": len(per_mod), "files": files,
            "needs_rebuild": any(s == "RECOMPILAR" for _, _, s in chosen)}


def cmd_apply(port_dir, wanted, only_file=None):
    """Casca de terminal em cima de apply() -- um caminho so para os dois."""
    try:
        r = apply(port_dir, wanted, only_file)
    except ValueError as e:
        raise SystemExit(str(e))
    for f in r["files"]:
        print("gravado: %s" % f)
    print("total: %d patch(es), %d escrita(s) em %d modulo(s)"
          % (len(r["patches"]), r["writes"], r["modules"]))
    if r["needs_rebuild"]:
        print("RECOMPILAR: rode o codegen + build do port para o patch entrar no codigo nativo.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    argv = sys.argv[1:]
    only = None
    if "--file" in argv:
        i = argv.index("--file")
        only = argv[i + 1]
        del argv[i:i + 2]
    if argv[0] == "list":
        cmd_list(argv[1], only)
    elif argv[0] == "apply":
        cmd_apply(argv[1], set(argv[2:]), only)
    else:
        raise SystemExit(__doc__)
