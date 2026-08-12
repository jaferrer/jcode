#!/usr/bin/env python3
"""Genera un vault de Obsidian uniendo el grafo de graft con la memoria de claude-mem.

    python3 graft_mem_vault.py <hub> <proyecto[,otro,...]> <dir-vault>
        [--min-refs=N] [--max-files=N] [--root=RUTA ...]
        [--no-docs] [--no-commits] [--no-sessions] [--build]

Entiende el paradigma HUB+SPOKE: la sesion se lanza desde el hub del proyecto
(~/ai/HUB/<PROY>) pero el codigo suele vivir en repos vinculados (spokes). Los
spokes no estan declarados en ninguna parte, asi que se descubren de la propia
claude-mem: cada ruta absoluta observada se remonta hasta su raiz git, y las
raices mas referenciadas son los spokes. Se agregan todos los wiring.json
disponibles (hub + spokes) en un unico grafo.

Ademas del codigo se incorporan los documentos trackeados en git: el markdown
(handoffs, ledgers, tracking, planes, CLAUDE.md, docs), que es la capa de plan, y
la configuracion yaml/toml mas el json de la config de agentes, que es la capa de
decision (perfiles, catalogos, wiring de MCP, los handoffs escritos en yaml).
graft no indexa ninguno de los dos, pero el markdown es entre el 8% y el 35% de
las rutas que registra claude-mem, y la configuracion explica tanto como un
README. Nada que huela a lock ni a secreto entra: el vault es texto plano.

Y se incorpora la sesion, que es la unica capa cuyo contenido ya venia escrito y
no se usaba: claude-mem redacta por prompt que se pidio, que se investigo, que se
aprendio y que quedo abierto. Las otras categorias cuentan QUE se toco; la sesion
cuenta POR QUE, que es lo que no se recupera releyendo el codigo.

El segundo argumento admite varios nombres separados por comas. claude-mem
archiva por directorio de trabajo, asi que un mismo proyecto puede acabar
repartido en varios nombres cuando se trabaja desde sus spokes: unirlos aqui
devuelve esas observaciones al vault al que pertenecen.

Estructura del vault:
    INDEX.md              entrada: raices, sesiones recientes, ranking, puntos ciegos
    code/<ruta>.md        una nota por fichero: simbolos, llamadas, memoria asociada
    docs/<ruta>.md        una nota por documento o config: contenido, ficheros citados
    memory/obs-<id>.md    una nota por observacion que engancha con el proyecto
    sessions/ses-<id>.md  una nota por sesion: peticion, aprendido, siguientes pasos
    commits/commit-<sha>.md  una nota por commit que toca un fichero del vault
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict

DB = os.path.expanduser(os.environ.get("CLAUDE_MEM_DB", "~/.claude-mem/claude-mem.db"))
HOME = os.path.expanduser("~")
MAX_SUFFIX_SEG = 3
MIN_REFS = 5  # referencias minimas para considerar una raiz git un spoke real
MAX_FILES = 5000  # por encima de esto el indice trae arbol vendorizado, no codigo del proyecto
MAX_DOCS = 2000  # tope de documentos por raiz, por si el repo lleva un sitio entero dentro
MAX_COMMITS = 1500  # tope de commits por raiz; mas historia que eso rara vez se consulta
# commits sin contenido: no explican nada y solo ensucian el grafo
RE_TRIVIAL = re.compile(
    r"^(wip|typo|fix typo|minor|tmp|temp|test|update|updates|cleanup|format|lint|"
    r"merge branch|merge pull request|bump|revert|\.+)\b", re.I)
MARCA = "generator: graft-mem-vault"  # firma que autoriza a limpiar un vault previo

# extensiones que graft parsea; sirven para reconocer rutas de codigo citadas en prosa
EXT_CODIGO = ("py", "ts", "tsx", "js", "jsx", "mjs", "cjs")
RE_RUTA_CODIGO = re.compile(r"[\w][\w./-]*\.(?:" + "|".join(EXT_CODIGO) + r")\b")


# nombres de nota resueltos para esta pasada; lo llena construir_nombres()
NOMBRES = {}


def slug_bruto(path):
    return re.sub(r"[^A-Za-z0-9._-]", "_", path)

# Configuracion que entra como nota de documento. El markdown es la capa de plan;
# el yaml/toml es la capa de decision (perfiles, catalogos, wiring de MCP, los
# handoffs que se escriben en yaml) y hasta ahora no tenia categoria en el vault.
EXT_CONFIG = ("yaml", "yml", "toml")
RE_RUTA_CONFIG = re.compile(r"[\w][\w./-]*\.(?:" + "|".join(EXT_CONFIG + ("json",)) + r")\b")
# .json solo dentro de la config de agentes: ahi es decision (settings, hooks, mcp).
# Fuera es lockfile o datos volcados, que no explican nada.
RE_JSON_CONFIG = re.compile(r"(^|/)\.(claude|codex)/[^/]*\.json$")
# El vault es texto plano: nada que suene a secreto o a lock entra. Solo se aplica a
# la configuracion, nunca al markdown, para no perder documentos legitimos por el
# nombre (tricks_ferrer/token-optimizer/README.md no es un secreto).
RE_SENSIBLE = re.compile(
    r"(.*lock.*|.*secretos?.*|.*secrets?.*|.*credenciales?.*|.*credentials?.*"
    r"|\.env.*|settings\.local)\.", re.I)
# resaltado por extension, para que un yaml siga leyendose como yaml en la nota
LENG = {"yaml": "yaml", "yml": "yaml", "toml": "toml", "json": "json"}
# campos del resumen de sesion de claude-mem, en el orden en que cuentan la sesion
CAMPOS_SESION = (("request", "Peticion"), ("investigated", "Investigado"),
                 ("learned", "Aprendido"), ("completed", "Completado"),
                 ("next_steps", "Siguientes pasos"), ("notes", "Notas"))


def slug(path):
    """Ruta -> nombre de nota, unico incluso en sistemas de ficheros insensibles."""
    return NOMBRES.get(path) or slug_bruto(path)


def construir_nombres(claves):
    """Asigna un nombre de nota unico a cada clave, sin colisiones de mayusculas.

    macOS (APFS) y Windows no distinguen mayusculas en los nombres de fichero,
    asi que dos rutas como .github/PULL_REQUEST_TEMPLATE.md y su gemela en
    minusculas —cosa que pasa cuando dos raices del mismo proyecto las escriben
    distinto— generan slugs distintos pero un unico fichero: la segunda escritura
    pisa a la primera y el enlace de la perdedora queda roto.

    Se resuelve por orden lexicografico para que el resultado no dependa de en
    que orden se recorrieron las raices: la primera se queda el nombre limpio y
    las demas llevan un sufijo derivado de su propia ruta.
    """
    import hashlib
    vistos = {}
    nombres = {}
    for k in sorted(claves):
        base = slug_bruto(k)
        bajo = base.lower()
        if bajo in vistos:
            h = hashlib.sha1(k.encode()).hexdigest()[:6]
            nombres[k] = f"{base}-{h}"
        else:
            vistos[bajo] = k
            nombres[k] = base
    return nombres


def esc(text):
    """Neutraliza [[...]] en texto ajeno: Obsidian lo leeria como wikilink."""
    return (text or "").replace("[[", "[ [").replace("]]", "] ]")


def jlist(blob):
    try:
        v = json.loads(blob or "[]")
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def obs_paths(row):
    """Rutas de una observacion, como (ruta, modificada)."""
    out = []
    for col, mod in (("files_modified", True), ("files_read", False)):
        try:
            for p in json.loads(row[col] or "[]"):
                if isinstance(p, str) and p:
                    out.append((p, mod))
        except (ValueError, TypeError):
            continue
    return out

def doc_admitido(rel):
    """Decide si un fichero entra como nota de documento.

    El markdown entra siempre. La configuracion entra si no huele a secreto ni a
    lock, y el .json solo si vive en la config de un agente.
    """
    base = os.path.basename(rel).lower()
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    if ext == "md":
        return True
    if RE_SENSIBLE.match(base):
        return False
    if ext in EXT_CONFIG:
        return True
    return ext == "json" and bool(RE_JSON_CONFIG.search(rel))


def fetch_rows(projects):
    if isinstance(projects, str):
        projects = [projects]
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    huecos = ",".join("?" * len(projects))
    return con.execute(
        "SELECT id, title, subtitle, text, facts, narrative, concepts, type,"
        "       created_at, files_modified, files_read, memory_session_id"
        "  FROM observations"
        f" WHERE project IN ({huecos})"
        "   AND (files_modified NOT IN ('', '[]') OR files_read NOT IN ('', '[]'))"
        " ORDER BY created_at_epoch DESC",
        tuple(projects),
    ).fetchall()

def fetch_sessions(projects):
    """Resumenes de sesion de claude-mem, agrupados por sesion.

    Es la unica capa del vault cuyo contenido ya venia escrito y no se usaba:
    claude-mem redacta un resumen por prompt (peticion, investigado, aprendido,
    completado, siguientes pasos) y nadie lo leia. Las observaciones cuentan QUE
    se toco; esto cuenta QUE se estaba intentando, que es lo que no se recupera
    releyendo el codigo.

    Se devuelven en orden de prompt: leidos en fila, cuentan la sesion.
    """
    if isinstance(projects, str):
        projects = [projects]
    if not os.path.exists(DB):
        return {}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    huecos = ",".join("?" * len(projects))
    try:
        filas = con.execute(
            "SELECT memory_session_id, request, investigated, learned, completed,"
            "       next_steps, notes, prompt_number, created_at"
            "  FROM session_summaries"
            f" WHERE project IN ({huecos}) AND memory_session_id IS NOT NULL"
            " ORDER BY created_at_epoch",
            tuple(projects),
        ).fetchall()
    except sqlite3.Error:
        return {}  # base de claude-mem antigua: el resto del vault se genera igual
    out = defaultdict(list)
    for r in filas:
        out[r["memory_session_id"]].append(r)
    return out


def git_root(path):
    """Raiz git que contiene path, o None. Solo sube hasta $HOME."""
    d = os.path.dirname(os.path.expanduser(path))
    while len(d) > len(HOME) and d != "/":
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return None


def discover_roots(rows, hub, min_refs=MIN_REFS, extra=()):
    """Descubre las raices de codigo del proyecto: el hub mas sus spokes.

    Los spokes salen de remontar cada ruta absoluta observada hasta su raiz git.
    Se descartan las raices poco referenciadas: son repos ajenos que una sesion
    toco de pasada, no spokes del proyecto.

    Las raices de --root se añaden sin filtro. Sirven para acotar un repo cuyo
    indice completo seria inservible: si el arbol trae Odoo o los addons de OCA
    dentro, se construye graft solo en el subdirectorio con el codigo propio y se
    declara ese subdirectorio como raiz, aunque no sea una raiz git.
    """
    refs = Counter()
    for r in rows:
        for p, _ in obs_paths(r):
            if p.startswith(("/", "~")):
                root = git_root(p)
                if root:
                    refs[root] += 1
    roots = [r for r, c in refs.items() if c >= min_refs]
    for r in extra:
        if r not in roots and os.path.isdir(r):
            roots.append(r)
    if hub not in roots and os.path.isdir(hub):
        roots.append(hub)
    # el hub primero: en caso de empate su copia manda
    roots.sort(key=lambda r: (r != hub, -refs[r], r))
    return roots, refs


STACK_SIZE_BIG = 65500  # igual que el alias graft-build-big de ~/.zshrc


def graft_build(root, timeout=300):
    """`graft build`; en repos grandes Node revienta el stack por recursion en
    el arbol de ficheros, y reintentamos con --stack-size ampliado (mismo
    workaround que el alias graft-build-big)."""
    r = subprocess.run(["graft", "build"], cwd=root, capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode == 0:
        return r
    stack_overflow = "stack size exceeded" in (r.stderr or "").lower()
    graft_bin = shutil.which("graft")
    if not stack_overflow or not graft_bin:
        return r
    return subprocess.run(
        ["node", f"--stack-size={STACK_SIZE_BIG}", graft_bin, "build"],
        cwd=root, capture_output=True, text=True, timeout=timeout)


def reconstruir_indices(roots, max_files=MAX_FILES):
    """Refresca con `graft build` los indices que ya existen, antes de leerlos.

    Solo los que ya existen: crear un indice nuevo en una raiz es una decision
    con consecuencias (un repo que trae Odoo o OCA dentro produce un indice
    inservible), y esa decision la toma quien monta el proyecto, no este script.

    Se saltan tambien los indices ya desmesurados: reconstruirlos tarda medio
    minuto y luego se descartan igual.
    """
    hechos, saltados = [], []
    for root in roots:
        wiring = os.path.join(root, "graft/.graph/wiring.json")
        if not os.path.exists(wiring):
            continue
        try:
            n = sum(1 for x in json.load(open(wiring)).get("nodes", [])
                    if x.get("kind") == "file")
        except (ValueError, OSError):
            n = 0
        if n > max_files:
            saltados.append(root)
            continue
        try:
            r = graft_build(root)
            (hechos if r.returncode == 0 else saltados).append(root)
        except (OSError, subprocess.SubprocessError):
            saltados.append(root)
    return hechos, saltados


def load_graphs(roots, max_files=MAX_FILES):
    """Agrega los wiring.json de todas las raices en un unico grafo.

    Clave de nodo: la ruta relativa al repo, siempre. Dentro de las raices de un
    mismo proyecto, la misma ruta relativa es el mismo fichero logico visto desde
    el hub y desde el spoke, y su historia debe vivir en una sola nota. Si el
    body_hash difiere es que los checkouts han divergido: se fusiona igualmente y
    se anota el numero de variantes.

    Separarlos seria peor por partida doble: partiria la historia del fichero en
    dos y, al crear dos claves para la misma ruta, volveria ambigua esa ruta y
    desactivaria las capas 2 y 3 del matcher.

    Se descartan los indices desmesurados: un graft build que atrapo node_modules,
    OCA o un arbol Docker completo aporta decenas de miles de ficheros ajenos que
    ahogarian el vault. ponytail: el umbral es un numero fijo (MAX_FILES); si algun
    dia hace falta, el criterio fino seria mirar la proporcion de ficheros
    ignorados por git dentro del indice.
    """
    por_ruta = defaultdict(list)  # rel -> [(root, nodo_fichero)]
    simbolos = defaultdict(list)  # (root, rel) -> [nodos]
    aristas = []                  # (root, src_rel, tgt_rel)
    sin_graft = []
    contaminados = []

    for root in roots:
        wiring = os.path.join(root, "graft/.graph/wiring.json")
        if not os.path.exists(wiring):
            sin_graft.append(root)
            continue
        try:
            g = json.load(open(wiring))
        except (ValueError, OSError):
            sin_graft.append(root)
            continue
        n_ficheros = sum(1 for n in g.get("nodes", []) if n.get("kind") == "file")
        if n_ficheros > max_files:
            contaminados.append((root, n_ficheros))
            continue
        for n in g.get("nodes", []):
            path = n.get("path")
            if not path:
                continue
            if n.get("kind") == "file":
                por_ruta[path].append((root, n))
            else:
                simbolos[(root, path)].append(n)
        for e in g.get("edges", []):
            src = e["source"].split("#")[0]
            tgt = e["target"].split("#")[0]
            if src != tgt:
                aristas.append((root, src, tgt))

    clave = {}  # (root, rel) -> clave de nota
    variantes = {}  # rel -> numero de versiones distintas del fichero
    for rel, apariciones in por_ruta.items():
        variantes[rel] = len({n.get("body_hash") for _, n in apariciones})
        for root, _ in apariciones:
            clave[(root, rel)] = rel

    ficheros = defaultdict(lambda: {"rels": set(), "roots": set(), "simbolos": [], "variantes": 1})
    for (root, rel), k in clave.items():
        ficheros[k]["rels"].add(rel)
        ficheros[k]["roots"].add(root)
        ficheros[k]["simbolos"] += simbolos.get((root, rel), [])
        ficheros[k]["variantes"] = max(ficheros[k]["variantes"], variantes.get(rel, 1))

    enlaces = set()
    for root, src, tgt in aristas:
        ks, kt = clave.get((root, src)), clave.get((root, tgt))
        if ks and kt and ks != kt:
            enlaces.add((ks, kt))

    return ficheros, enlaces, clave, sin_graft, contaminados


def load_docs(roots, max_docs=MAX_DOCS):
    """Recoge los documentos trackeados en git de cada raiz.

    Markdown (la capa de plan) mas configuracion yaml/toml y el json de la config
    de agentes (la capa de decision): un handoff en yaml o instalador/catalogo.yaml
    explican tanto como un README y antes no entraban en ninguna categoria.

    Solo trackeados: `git ls-files` deja fuera de un plumazo las cachés de
    herramientas (graft/, graphify-out/, node_modules/), que estan gitignoradas.
    Si una raiz no es un repo git se recorre a mano con el mismo criterio.

    Como el codigo, se fusionan por ruta relativa: el mismo handoff visto desde
    el hub y desde el spoke es un solo documento.
    """
    docs = defaultdict(lambda: {"rels": set(), "roots": set(), "abs": None})
    clave = {}
    patrones = ["*.md", "*.json"] + [f"*.{e}" for e in EXT_CONFIG]
    for root in roots:
        rels = []
        try:
            r = subprocess.run(
                ["git", "ls-files", *patrones], cwd=root,
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                rels = [l for l in r.stdout.splitlines() if l and doc_admitido(l)]
        except (OSError, subprocess.SubprocessError):
            pass
        if not rels:  # raiz sin git, o subdirectorio: recorrido manual
            for base, dirs, fs in os.walk(root):
                dirs[:] = [d for d in dirs if d not in
                           (".git", "graft", "graphify-out", "node_modules", ".tldr")]
                for f in fs:
                    rel = os.path.relpath(os.path.join(base, f), root)
                    if doc_admitido(rel):
                        rels.append(rel)
        if len(rels) > max_docs:
            rels = sorted(rels)[:max_docs]
        for rel in rels:
            ruta = os.path.join(root, rel)
            if not os.path.isfile(ruta):
                continue
            clave[(root, rel)] = rel
            docs[rel]["rels"].add(rel)
            docs[rel]["roots"].add(root)
            if docs[rel]["abs"] is None:
                docs[rel]["abs"] = ruta
    return docs, clave


def load_commits(roots, clave_por_raiz, max_commits=MAX_COMMITS):
    """Mensajes de commit de cada raiz, atados a los ficheros que tocaron.

    Complementa a claude-mem sin solaparse: una observacion registra el
    razonamiento de la sesion (lo que se investigo, lo que se descarto) y un
    commit registra lo aceptado, redactado a posteriori. Y el enlace es exacto,
    no heuristico: git sabe con certeza que ficheros toco cada commit, asi que
    aqui no hace falta el matcher por sufijo.

    Cubre sobre todo los puntos ciegos: en EVOTEKNIC el 96% de los ficheros sin
    memoria de claude-mem si tienen historial de commits.
    """
    # los separadores se piden con los escapes de git (%x01/%x1f) y no como
    # caracteres literales: un NUL dentro de argv no se puede pasar a exec
    SEP, CAMPO = "\x01", "\x1f"
    commits = {}
    por_fichero = defaultdict(list)
    for root in roots:
        claves = clave_por_raiz.get(root)
        if not claves:
            continue
        try:
            r = subprocess.run(
                ["git", "-C", root, "log", f"-{max_commits}", "--no-merges",
                 "--format=%x01%H%x1f%an%x1f%aI%x1f%s%x1f%b%x1f",
                 "--name-only"],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                continue
        except (OSError, subprocess.SubprocessError):
            continue
        for bruto in r.stdout.split(SEP)[1:]:
            partes = bruto.split(CAMPO)
            if len(partes) < 5:
                continue
            sha, autor, fecha, asunto, cuerpo = partes[:5]
            if RE_TRIVIAL.match(asunto.strip()):
                continue
            tocados = set()
            for rel in partes[5].splitlines() if len(partes) > 5 else []:
                k = claves.get(rel.strip())
                if k:
                    tocados.add(k)
            if not tocados:
                continue  # solo toco ficheros que no estan en el vault
            corto = sha[:10]
            if corto not in commits:
                commits[corto] = {"sha": sha, "autor": autor, "fecha": fecha[:10],
                                  "asunto": asunto.strip(), "cuerpo": cuerpo.strip(),
                                  "files": sorted(tocados), "root": os.path.basename(root)}
                for k in tocados:
                    por_fichero[k].append(corto)
    return commits, por_fichero


def write_commit_notes(out, commits, titulos_codigo):
    for corto, c in commits.items():
        L = [
            "---",
            f"commit: {c['sha']}",
            f'date: "{c["fecha"]}"',
            f'author: "{c["autor"]}"',
            f'repo: "{c["root"]}"',
            f"files: {len(c['files'])}",
            "tags: [commit]",
            "---",
            f"# {esc(c['asunto'])}",
            "",
            f"*{c['fecha']} · {c['autor']} · `{corto}` · {c['root']}*",
            "",
        ]
        if c["cuerpo"]:
            L += [esc(c["cuerpo"]), ""]
        L += ["## Ficheros", ""] + [f"- [[{slug(f)}]]" for f in c["files"]] + [""]
        with open(os.path.join(out, "commits", f"commit-{corto}.md"), "w") as fh:
            fh.write("\n".join(L))


def build_matcher(clave):
    """Tres capas, de mas precisa a mas permisiva.

    1. (raiz, ruta relativa exacta) — cuando la ruta observada cae dentro de una
       raiz conocida. Es la que aporta el paradigma HUB+SPOKE.
    2. sufijo de ruta mas largo que coincida con una ruta relativa completa.
    3. sufijo corto (1-3 segmentos) y solo si es inequivoco — red de seguridad
       para rutas relativas sueltas que claude-mem guarda sin raiz.
    """
    exacto = {}
    por_rel = defaultdict(set)
    por_sufijo = defaultdict(set)
    max_seg = 1
    for (root, rel), k in clave.items():
        exacto[(root, rel)] = k
        por_rel[rel].add(k)
        seg = rel.split("/")
        max_seg = max(max_seg, len(seg))
        for i in range(1, min(MAX_SUFFIX_SEG, len(seg)) + 1):
            por_sufijo["/".join(seg[-i:])].add(k)
    return exacto, por_rel, por_sufijo, max_seg


def match(raw, roots, exacto, por_rel, por_sufijo, max_seg):
    """Ruta observada -> clave de nota, o None."""
    p = os.path.expanduser(raw)
    if p.startswith("/"):
        # de la raiz mas especifica a la mas general: si scripts/ es raiz dentro
        # del hub, una ruta suya debe resolverse contra scripts/, no contra el hub
        for root in sorted(roots, key=len, reverse=True):
            if p.startswith(root + "/"):
                k = exacto.get((root, os.path.relpath(p, root)))
                if k:
                    return k
    seg = p.strip("/").split("/")
    for i in range(min(max_seg, len(seg)), 0, -1):
        c = por_rel.get("/".join(seg[-i:]))
        if c and len(c) == 1:
            return next(iter(c))
    for i in range(min(MAX_SUFFIX_SEG, len(seg)), 0, -1):
        c = por_sufijo.get("/".join(seg[-i:]))
        if c and len(c) == 1:
            return next(iter(c))
    return None


def link_observations(rows, roots, matcher):
    obs, por_fichero = [], defaultdict(list)
    for r in rows:
        tocados, modificados = set(), set()
        for raw, mod in obs_paths(r):
            k = match(raw, roots, *matcher)
            if k:
                tocados.add(k)
                if mod:
                    modificados.add(k)
        if not tocados:
            continue
        obs.append({"row": r, "files": sorted(tocados), "modified": modificados})
        for k in tocados:
            por_fichero[k].append(r["id"])
    return obs, por_fichero


def write_code_notes(out, ficheros, enlaces, por_fichero, titulos,
                     commits_por_fichero=None, commits=None):
    sale = defaultdict(set)
    entra = defaultdict(set)
    for a, b in enlaces:
        sale[a].add(b)
        entra[b].add(a)

    for k, info in sorted(ficheros.items()):
        vistos, unicos = set(), []
        for n in sorted(info["simbolos"], key=lambda n: n.get("span") or ""):
            firma = (n.get("name"), n.get("span"))
            if firma not in vistos:
                vistos.add(firma)
                unicos.append(n)
        ids = por_fichero.get(k, [])
        etiquetas = sorted(os.path.basename(r) for r in info["roots"])
        L = [
            "---",
            f'graft_path: "{sorted(info["rels"])[0]}"',
            f'roots: [{", ".join(etiquetas)}]',
            f'variants: {info["variantes"]}',
            f"symbols: {len(unicos)}",
            f"observations: {len(ids)}",
            f"commits: {len((commits_por_fichero or {}).get(k, []))}",
            "tags: [code]",
            "---",
            f"# {k}",
            "",
            f"*Presente en: {', '.join(etiquetas)}*",
            "",
        ] + ([
            f"> ⚠ Los {info['variantes']} checkouts han divergido: el contenido difiere"
            " entre raices. Los simbolos listados son la union de todas las versiones.",
            "",
        ] if info["variantes"] > 1 else [])
        if unicos:
            L += ["## Simbolos", ""]
            for n in unicos:
                sig = (n.get("signature") or n.get("name") or "").replace("\n", " ")[:120]
                L.append(f"- **{n.get('name')}** · {n.get('kind')} · `{n.get('span')}` — `{sig}`")
            L.append("")
        for etiqueta, rel in (("Llama a", sale), ("Llamado por", entra)):
            if rel.get(k):
                L += [f"## {etiqueta}", ""] + [f"- [[{slug(t)}]]" for t in sorted(rel[k])] + [""]
        if ids:
            L += ["## Memoria", ""]
            L += [f"- [[obs-{i}]] — {esc(titulos.get(i, ''))}" for i in ids]
            L.append("")
        shas = (commits_por_fichero or {}).get(k, [])
        if shas and commits:
            L += ["## Commits", ""]
            for sh in shas:
                c = commits.get(sh)
                if c:
                    L.append(f"- [[commit-{sh}]] — {c['fecha']} {esc(c['asunto'])[:88]}")
            L.append("")
        with open(os.path.join(out, "code", slug(k) + ".md"), "w") as fh:
            fh.write("\n".join(L))


def codigo_citado(texto, claves, roots, matcher):
    """Rutas de codigo o de configuracion citadas en la prosa -> claves de nota.
    Es lo que cierra el triangulo plan <-> codigo <-> incidencias: un plan de
    migracion que nombra models/sale_order.py acaba enlazando con la nota de ese
    fichero y con su historial de incidencias. Con la configuracion dentro, un
    README que nombra instalador/catalogo.yaml enlaza tambien con el catalogo.
    """
    hits = set()
    for regex in (RE_RUTA_CODIGO, RE_RUTA_CONFIG):
        for cand in set(regex.findall(texto)):
            k = match(cand, roots, *matcher)
            if k and k in claves:
                hits.add(k)
    return sorted(hits)


def write_doc_notes(out, docs, por_fichero, titulos, claves, roots, matcher):
    for k, info in sorted(docs.items()):
        try:
            texto = open(info["abs"], encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        ids = por_fichero.get(k, [])
        etiquetas = sorted(os.path.basename(r) for r in info["roots"])
        citado = codigo_citado(texto, claves, roots, matcher)
        ext = k.rsplit(".", 1)[-1].lower() if "." in k else ""
        clase = ext or "txt"
        L = [
            "---",
            f'doc_path: "{k}"',
            f'doc_kind: "{clase}"',
            f'roots: [{", ".join(etiquetas)}]',
            f"observations: {len(ids)}",
            f"code_referenced: {len(citado)}",
            "tags: [doc]",
            "---",
            f"# {k}",
            "",
            f"*Documento de: {', '.join(etiquetas)}*",
            "",
        ]
        if citado:
            L += ["## Ficheros que cita", ""] + [f"- [[{slug(c)}]]" for c in citado] + [""]
        if ids:
            L += ["## Memoria", ""]
            L += [f"- [[obs-{i}]] — {esc(titulos.get(i, ''))}" for i in ids]
            L.append("")
        if ext == "md":
            L += ["---", "", esc(texto)]
        else:
            # no es markdown: se encierra en un bloque para que Obsidian no lo
            # interprete y para que el yaml siga leyendose como yaml. Dentro del
            # bloque los [[...]] no son enlaces, asi que basta con neutralizar las
            # comillas que cerrarian el bloque antes de tiempo.
            L += ["---", "", f"```{LENG.get(ext, '')}",
                  texto.replace("```", "'''"), "```"]
        with open(os.path.join(out, "docs", slug(k) + ".md"), "w") as fh:
            fh.write("\n".join(L))


def write_memory_notes(out, obs, con_sesiones=False):
    for o in obs:
        r, files = o["row"], o["files"]
        sid = r["memory_session_id"] if con_sesiones else None
        L = [
            "---",
            f"obs_id: {r['id']}",
            f'date: "{r["created_at"]}"',
            f'type: "{r["type"]}"',
            f"files: {len(files)}",
        ]
        if sid:
            L.append(f'session: "{sid}"')
        L += [
            "tags: [memory]",
            "---",
            f"# {esc(r['title']) or '(sin titulo)'}",
            "",
        ]
        if r["subtitle"]:
            L += [f"*{esc(r['subtitle'])}*", ""]
        if r["narrative"]:
            L += [esc(r["narrative"]), ""]
        elif r["text"]:
            L += [esc(r["text"]), ""]
        hechos = jlist(r["facts"])
        if hechos:
            L += ["## Hechos", ""] + [f"- {esc(str(f))}" for f in hechos] + [""]
        L += ["## Ficheros", ""]
        for f in files:
            marca = " **(modificado)**" if f in o["modified"] else ""
            L.append(f"- [[{slug(f)}]]{marca}")
        L.append("")
        if sid:
            L += ["## Sesion", "", f"- [[ses-{slug_bruto(sid)}]]", ""]
        conceptos = jlist(r["concepts"])
        if conceptos:
            etiquetas = "".join(f"#{re.sub(r'[^A-Za-z0-9_-]', '-', str(c))} " for c in conceptos)
            L += ["## Conceptos", "", etiquetas, ""]
        with open(os.path.join(out, "memory", f"obs-{r['id']}.md"), "w") as fh:
            fh.write("\n".join(L))

def link_sessions(obs, resumenes):
    """Sesion -> observaciones y ficheros que la componen.

    Solo entran las sesiones que dejaron alguna observacion enganchada al vault:
    una sesion cuyo trabajo no toco este proyecto no tiene de donde colgarse.
    """
    ses = {}
    for o in obs:
        sid = o["row"]["memory_session_id"]
        if not sid:
            continue
        d = ses.setdefault(sid, {"obs": [], "files": set(), "fecha": ""})
        d["obs"].append(o["row"]["id"])
        d["files"].update(o["files"])
        fecha = (o["row"]["created_at"] or "")[:10]
        if fecha and (not d["fecha"] or fecha < d["fecha"]):
            d["fecha"] = fecha  # el dia en que arranco, no el del ultimo apunte
    for sid, d in ses.items():
        d["resumenes"] = resumenes.get(sid, [])
    return ses


def write_session_notes(out, sesiones, titulos):
    """Una nota por sesion: lo que se pidio, lo que se aprendio y que quedo abierto.

    Las otras cuatro categorias responden al QUE (que fichero, que cambio, que se
    observo). Esta responde al POR QUE, que es lo que no se recupera releyendo el
    codigo, y agrupa en un solo nodo las observaciones sueltas de una tanda.
    """
    for sid, d in sesiones.items():
        res = d["resumenes"]
        titulo = ""
        for r in res:
            if r["request"]:
                titulo = r["request"].strip().splitlines()[0]
                break
        L = [
            "---",
            f'session: "{sid}"',
            f'date: "{d["fecha"]}"',
            f"observations: {len(d['obs'])}",
            f"files: {len(d['files'])}",
            f"prompts: {len(res)}",
            "tags: [session]",
            "---",
            f"# Sesion {d['fecha']} — {esc(titulo)[:88] or '(sin resumen)'}",
            "",
        ]
        if not res:
            L += ["> claude-mem no guardo resumen de esta sesion: queda la agrupacion"
                  " de sus observaciones.", ""]
        for r in res:
            L += [f"## Prompt {r['prompt_number']}", ""]
            for col, etiqueta in CAMPOS_SESION:
                valor = (r[col] or "").strip()
                if valor:
                    L += [f"**{etiqueta}**", "", esc(valor), ""]
        L += ["## Observaciones", ""]
        L += [f"- [[obs-{i}]] — {esc(titulos.get(i, ''))}" for i in d["obs"]] + [""]
        L += ["## Ficheros", ""] + [f"- [[{slug(f)}]]" for f in sorted(d["files"])] + [""]
        with open(os.path.join(out, "sessions", f"ses-{slug_bruto(sid)}.md"), "w") as fh:
            fh.write("\n".join(L))


def write_index(out, hub, project, roots, refs, sin_graft, contaminados,
                ficheros, docs, obs, por_fichero, commits=None, commits_por_fichero=None,
                sesiones=None):
    ranking = sorted(((k, v) for k, v in por_fichero.items() if k in ficheros),
                     key=lambda kv: -len(kv[1]))[:25]
    doc_rank = sorted(((k, por_fichero.get(k, [])) for k in docs if por_fichero.get(k)),
                      key=lambda kv: -len(kv[1]))[:15]
    config = [k for k in docs if not k.lower().endswith(".md")]
    L = [
        "---",
        "tags: [index]",
        MARCA,
        "---",
        f"# {project} — codigo, documentos, memoria y sesiones",
        "",
        f"- hub: `{hub}`",
        f"- proyecto claude-mem: `{project}`",
        f"- ficheros en el grafo agregado: **{len(ficheros)}**",
        f"- documentos: **{len(docs)}** "
        f"(markdown {len(docs) - len(config)}, configuracion {len(config)})",
        f"- observaciones enganchadas: **{len(obs)}**",
        f"- sesiones: **{len(sesiones or {})}**",
        f"- commits: **{len(commits or {})}**",
        f"- notas con memoria asociada: **{len(por_fichero)}/{len(ficheros) + len(docs)}**",
        f"- notas con historia (memoria o commits): "
        f"**{len(set(por_fichero) | set(commits_por_fichero or {}))}/{len(ficheros) + len(docs)}**",
        "",
        "## Raices (hub + spokes)",
        "",
        "| raiz | referencias | graft |",
        "|------|-------------|-------|",
    ]
    contam = {r: n for r, n in contaminados}
    for r in roots:
        if r in contam:
            estado = f"⚠ descartado ({contam[r]} ficheros — arbol vendorizado)"
        elif r in sin_graft:
            estado = "✗ sin indexar"
        else:
            estado = "✓"
        L.append(f"| `{r}` | {refs.get(r, 0)} | {estado} |")
    if sin_graft:
        L += ["", "> Spokes sin indexar. Para incluirlos:", "> ```bash"]
        L += [f"> cd {r} && graft build" for r in sin_graft]
        L += ["> ```"]
    if sesiones:
        # la sesion es la unica entrada que cuenta el POR QUE, asi que va arriba
        recientes = sorted(sesiones.items(), key=lambda kv: kv[1]["fecha"], reverse=True)[:10]
        L += ["", "## Sesiones recientes", ""]
        for sid, d in recientes:
            titulo = ""
            for r in d["resumenes"]:
                if r["request"]:
                    titulo = r["request"].strip().splitlines()[0]
                    break
            L.append(f"- [[ses-{slug_bruto(sid)}]] — {d['fecha']} · "
                     f"{len(d['obs'])} obs · {esc(titulo)[:76] or '(sin resumen)'}")
    L += ["", "## Codigo con mas historia", ""]
    L += [f"{i}. [[{slug(p)}]] — {len(ids)} observaciones"
          for i, (p, ids) in enumerate(ranking, 1)] or ["- (ninguno)"]
    if doc_rank:
        L += ["", "## Documentos con mas historia", ""]
        L += [f"{i}. [[{slug(p)}]] — {len(ids)} observaciones"
              for i, (p, ids) in enumerate(doc_rank, 1)]
    if commits_por_fichero:
        crank = sorted(((k, v) for k, v in commits_por_fichero.items() if k in ficheros),
                       key=lambda kv: -len(kv[1]))[:15]
        if crank:
            L += ["", "## Codigo con mas commits", ""]
            L += [f"{i}. [[{slug(p)}]] — {len(v)} commits"
                  for i, (p, v) in enumerate(crank, 1)]
    L += ["", "## Ficheros sin ninguna historia (puntos ciegos)", ""]
    ciegos = sorted(set(ficheros) - set(por_fichero) - set(commits_por_fichero or {}))[:25]
    L += [f"- [[{slug(p)}]]" for p in ciegos] or ["- (ninguno)"]
    with open(os.path.join(out, "INDEX.md"), "w") as fh:
        fh.write("\n".join(L))


def limpiar(out):
    """Borra las notas de una generacion anterior para que no queden huerfanas.

    Solo actua si el destino lleva nuestra firma en INDEX.md: nunca toca un
    directorio que no hayamos generado nosotros.
    """
    index = os.path.join(out, "INDEX.md")
    subdirs = [d for d in ("code", "docs", "memory", "commits", "sessions")
               if os.path.isdir(os.path.join(out, d))]
    if not subdirs and not os.path.exists(index):
        return
    if not os.path.exists(index) or MARCA not in open(index).read():
        sys.exit(
            f"{out} ya existe y no lleva la firma de graft-mem-vault.\n"
            "No lo toco por si contiene notas tuyas. Borralo o elige otro destino."
        )
    for d in subdirs:
        ruta = os.path.join(out, d)
        for f in os.listdir(ruta):
            if f.endswith(".md"):
                os.remove(os.path.join(ruta, f))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    min_refs, max_files = MIN_REFS, MAX_FILES
    extra_roots, con_docs, con_commits, con_build = [], True, True, False
    con_sesiones = True
    for a in sys.argv[1:]:
        if a.startswith("--min-refs="):
            min_refs = int(a.split("=", 1)[1])
        elif a.startswith("--max-files="):
            max_files = int(a.split("=", 1)[1])
        elif a.startswith("--root="):
            extra_roots.append(os.path.abspath(os.path.expanduser(a.split("=", 1)[1])))
        elif a == "--no-docs":
            con_docs = False
        elif a == "--no-commits":
            con_commits = False
        elif a == "--no-sessions":
            con_sesiones = False
        elif a == "--build":
            con_build = True
    if len(args) != 3:
        sys.exit(__doc__)
    hub = os.path.abspath(os.path.expanduser(args[0]))
    projects = [x.strip() for x in args[1].split(",") if x.strip()]
    project = projects[0]  # el primero da nombre al vault
    out = os.path.abspath(os.path.expanduser(args[2]))

    rows = fetch_rows(projects)
    if not rows:
        sys.exit("claude-mem no tiene observaciones con rutas para "
                 + ", ".join(repr(x) for x in projects))

    roots, refs = discover_roots(rows, hub, min_refs, extra_roots)
    reconstruidos = []
    if con_build:
        reconstruidos, _ = reconstruir_indices(roots, max_files)
    ficheros, enlaces, clave, sin_graft, contaminados = load_graphs(roots, max_files)
    docs, clave_docs = load_docs(roots) if con_docs else ({}, {})
    if not ficheros and not docs:
        sys.exit(f"ninguna raiz aporta un indice usable — ejecuta `graft build` en {hub}"
                 f" o sube --max-files si el indice es legitimamente grande")

    combinada = dict(clave)
    combinada.update(clave_docs)
    matcher = build_matcher(combinada)
    obs, por_fichero = link_observations(rows, roots, matcher)
    sesiones = link_sessions(obs, fetch_sessions(projects)) if con_sesiones else {}

    # los commits se atan por la ruta exacta que registra git, sin heuristica
    clave_por_raiz = defaultdict(dict)
    for (root, rel), k in combinada.items():
        clave_por_raiz[root][rel] = k
    commits, commits_por_fichero = (
        load_commits(roots, clave_por_raiz) if con_commits else ({}, {}))

    NOMBRES.clear()
    NOMBRES.update(construir_nombres(set(ficheros) | set(docs)))

    limpiar(out)
    for sub in ("code", "docs", "memory", "commits", "sessions"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    titulos = {o["row"]["id"]: (o["row"]["title"] or "") for o in obs}
    # la prosa puede citar codigo o configuracion; el markdown no se autocita
    citables = set(ficheros) | {k for k in docs if not k.lower().endswith(".md")}
    write_code_notes(out, ficheros, enlaces, por_fichero, titulos,
                     commits_por_fichero, commits)
    write_doc_notes(out, docs, por_fichero, titulos, citables, roots, matcher)
    write_memory_notes(out, obs, bool(sesiones))
    write_session_notes(out, sesiones, titulos)
    write_commit_notes(out, commits, titulos)
    write_index(out, hub, ", ".join(projects), roots, refs, sin_graft, contaminados,
                ficheros, docs, obs, por_fichero, commits, commits_por_fichero, sesiones)

    con_mem_docs = sum(1 for k in docs if por_fichero.get(k))
    config_docs = [k for k in docs if not k.lower().endswith(".md")]
    contam = dict(contaminados)
    print(f"vault: {out}")
    print(f"  raices: {len(roots)} ({len(roots) - len(sin_graft) - len(contaminados)} usadas)")
    for r in roots:
        if r in contam:
            marca = f"⚠ {contam[r]} ficheros, descartado"
        else:
            marca = "✗" if r in sin_graft else "✓"
        print(f"    {marca} {r}  ({refs.get(r, 0)} refs)")
    print(f"  notas de codigo   : {len(ficheros)}")
    print(f"  notas de documento: {len(docs)} ({len(docs) - len(config_docs)} md"
          f" + {len(config_docs)} config, {con_mem_docs} con memoria asociada)")
    print(f"  notas de memoria  : {len(obs)} de {len(rows)} observaciones con rutas")
    if con_sesiones:
        con_resumen = sum(1 for d in sesiones.values() if d["resumenes"])
        print(f"  notas de sesion   : {len(sesiones)} "
              f"({con_resumen} con resumen de claude-mem)")
    if con_commits:
        cubiertos = len(set(commits_por_fichero) | set(por_fichero))
        print(f"  notas de commit   : {len(commits)} "
              f"({len(commits_por_fichero)} ficheros con historial)")
        print(f"  con historia (memoria o commits): {cubiertos}/{len(ficheros) + len(docs)}")
    else:
        print(f"  con memoria asociada: {len(por_fichero)}/{len(ficheros) + len(docs)}")
    if reconstruidos:
        print(f"  indices de graft reconstruidos: {len(reconstruidos)}")
    if contaminados:
        print("  indices descartados por desmesurados (arbol vendorizado dentro del repo).")
        print("  arreglalo acotando el `graft build`, o forzalos con --max-files=N")
    if sin_graft:
        print("  spokes sin indexar (ejecuta `graft build` para incluirlos):")
        for r in sin_graft:
            print(f"    {r}")


if __name__ == "__main__":
    main()
