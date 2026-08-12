#!/usr/bin/env python3
"""Consulta el vault del proyecto actual sin cargarlo en contexto.

    vault_lookup.py <lo-que-sea>          busca; el tipo de consulta se deduce solo
    vault_lookup.py <termino> --all       sin topes
    vault_lookup.py --ses <id>            abre la sesion: que se pidio y que se aprendio
    vault_lookup.py --global <termino>    busca en TODOS los vaults, no solo el de este repo
    vault_lookup.py --global --obs proyecto:id   abre una observacion de otro proyecto

No hay que decirle de que tipo es lo que buscas:

    vault_lookup.py models/sale_order.py  ruta o nombre  -> ficha del fichero
    vault_lookup.py 29052                 solo digitos   -> abre esa observacion
    vault_lookup.py gastos_devolucion     concepto       -> busca en el contenido
    vault_lookup.py "efectos devueltos"   frase          -> busca en el contenido

Una busqueda por concepto devuelve las tres capas: las observaciones (que se
toco), las sesiones que ya trabajaron eso (que se pretendia) y los documentos que
lo mencionan.

Los flags siguen estando por si quieres forzar: --obs, --ses, --doc, --name,
--text, --stats.

El vault se resuelve solo desde el directorio actual, mirando la configuracion
de refresh_vaults. Pensado para dos pasos: primero el indice, que cuesta unas
decenas de tokens y lista los titulos, y solo despues las observaciones que de
verdad interesen. Volcar el vault entero cuesta miles de tokens y sobra.
"""
import json
import os
import re
import sys

CONFIG = os.path.expanduser(os.environ.get(
    "GRAFT_MEM_VAULT_CONFIG", "~/.config/graft-mem-vault/projects.json"))
TOPE_OBS = 15  # un fichero muy trabajado acumula cientos; las recientes bastan para orientarse
TOPE_TEXTO = 12  # aciertos por contenido que se muestran
DB = os.path.expanduser(os.environ.get("CLAUDE_MEM_DB", "~/.claude-mem/claude-mem.db"))
EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".md", ".json", ".yml", ".yaml",
       ".xml", ".sql", ".sh")


def cargar_cfg():
    if not os.path.exists(CONFIG):
        sys.exit(f"no existe {CONFIG} — ejecuta refresh_vaults.py primero")
    with open(CONFIG) as fh:
        return json.load(fh)


def resolver_vault(cwd=None):
    """Vault del proyecto al que pertenece el directorio actual.

    Se compara sobre rutas reales: en macOS /var es un enlace a /private/var y
    os.getcwd() devuelve la resuelta, asi que comparar contra la ruta escrita en
    la configuracion no encontraria nada. Lo mismo con ~/.claude -> ~/.claude-v47
    o cualquier hub que sea un enlace.
    """
    cwd = os.path.realpath(cwd or os.getcwd())
    cfg = cargar_cfg()
    base = os.path.expanduser(cfg.get("vault_dir", "~/vaults"))
    mejor, mejor_len = None, -1
    for nombre, conf in cfg["projects"].items():
        rutas = [conf["hub"]] + list(conf.get("roots", []))
        for r in rutas:
            r = os.path.realpath(os.path.expanduser(r))
            # la raiz mas especifica que contenga el cwd gana
            if (cwd == r or cwd.startswith(r + "/")) and len(r) > mejor_len:
                mejor, mejor_len = nombre, len(r)
    if not mejor:
        sys.exit(f"{cwd} no cae dentro de ningun proyecto de {CONFIG}")
    vault = os.path.join(base, f"{mejor}-graft-mem")
    if not os.path.isdir(vault):
        sys.exit(f"el proyecto es {mejor} pero no hay vault en {vault}")
    return mejor, vault


def parece_ruta(t):
    """El termino se refiere a un fichero concreto, no a un concepto."""
    return t.endswith(EXT) or "/" in t


def buscar_observaciones(proyecto, vault, termino, tope=TOPE_TEXTO, crudo=False):
    """Busca en el contenido usando el indice FTS5 que claude-mem ya mantiene.

    Es ~56 veces mas rapido que recorrer el vault con grep (4 ms frente a 225) y,
    lo que importa mas, viene ordenado por relevancia en vez de por orden de
    aparicion. Se descartan los ids que no estan en el vault: una observacion
    consolidada o archivada por memory-engineering sigue en el indice pero ya no
    tiene nota, y enlazarla seria mentir.
    """
    import sqlite3
    if not os.path.exists(DB):
        return []
    # crudo=True deja pasar una consulta FTS5 ya construida (p. ej. con OR);
    # por defecto se entrecomilla, que es lo que quiere quien busca una frase
    consulta = termino if crudo else '"' + termino.replace('"', ' ') + '"'
    proyectos = nombres_de_proyecto(proyecto)
    huecos = ",".join("?" * len(proyectos))
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        filas = con.execute(
            "SELECT o.id, o.title, o.created_at FROM observations_fts f"
            "  JOIN observations o ON o.rowid = f.rowid"
            f" WHERE observations_fts MATCH ? AND o.project IN ({huecos})"
            " ORDER BY rank LIMIT ?",
            (consulta, *proyectos, tope * 3),
        ).fetchall()
    except Exception:
        return []
    memoria = os.path.join(vault, "memory")
    out = []
    for oid, titulo, fecha in filas:
        if os.path.exists(os.path.join(memoria, f"obs-{oid}.md")):
            out.append((oid, titulo or "", (fecha or "")[:10]))
        if len(out) >= tope:
            break
    return out


def buscar_en_documentos(vault, termino, tope=TOPE_TEXTO):
    """Documentos del vault que mencionan el termino. FTS5 no los cubre."""
    import shutil
    import subprocess
    d = os.path.join(vault, "docs")
    if not os.path.isdir(d):
        return []
    if shutil.which("rg"):
        cmd = ["rg", "-l", "-i", "--fixed-strings", termino, d]
    else:
        cmd = ["grep", "-rli", "--fixed-strings", termino, d]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [os.path.basename(l)[:-3] for l in r.stdout.splitlines() if l][:tope]

def slug_ses(sid):
    """Nombre de nota de una sesion. Mismo criterio que el generador."""
    return "ses-" + re.sub(r"[^A-Za-z0-9._-]", "_", sid)


def buscar_sesiones(proyecto, vault, termino, tope=TOPE_TEXTO, crudo=False):
    """Sesiones cuyo resumen menciona el termino.

    Es la busqueda que responde «esto ya lo intentamos»: el resumen de sesion
    guarda la peticion y lo aprendido, que es donde vive la intencion, y eso no
    esta en el indice de observaciones. Como alli, se descartan las sesiones sin
    nota en el vault: enlazarlas seria mentir.
    """
    import sqlite3
    if not os.path.exists(DB):
        return []
    consulta = termino if crudo else '"' + termino.replace('"', ' ') + '"'
    proyectos = nombres_de_proyecto(proyecto)
    huecos = ",".join("?" * len(proyectos))
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        filas = con.execute(
            "SELECT s.memory_session_id, s.request, s.created_at"
            "  FROM session_summaries_fts f"
            "  JOIN session_summaries s ON s.rowid = f.rowid"
            f" WHERE session_summaries_fts MATCH ? AND s.project IN ({huecos})"
            " ORDER BY rank LIMIT ?",
            (consulta, *proyectos, tope * 5),
        ).fetchall()
    except Exception:
        return []
    d = os.path.join(vault, "sessions")
    out, vistos = [], set()
    for sid, req, fecha in filas:
        # un resumen por prompt: la misma sesion sale varias veces
        if not sid or sid in vistos:
            continue
        if not os.path.exists(os.path.join(d, slug_ses(sid) + ".md")):
            continue
        vistos.add(sid)
        titulo = (req or "").strip().splitlines()[0] if (req or "").strip() else ""
        out.append((sid, titulo, (fecha or "")[:10]))
        if len(out) >= tope:
            break
    return out


def nombres_de_proyecto(principal):
    """Nombres de claude-mem que alimentan el vault de este proyecto."""
    try:
        conf = cargar_cfg()["projects"].get(principal, {})
    except SystemExit:
        return [principal]
    return [principal] + list(conf.get("also", []))


TOPE_GLOBAL = 3  # por proyecto: mas ruido que ayuda si se ahoga con volumen


def todos_los_vaults(cfg):
    """Todos los vaults ya generados, con su proyecto. Solo para --global."""
    base = os.path.expanduser(cfg.get("vault_dir", "~/vaults"))
    out = []
    for nombre in cfg.get("projects", {}):
        vault = os.path.join(base, f"{nombre}-graft-mem")
        if os.path.isdir(vault):
            out.append((nombre, vault))
    return out


def buscar_global_observaciones(cfg, termino, tope_por_proyecto=TOPE_GLOBAL, crudo=False):
    """Como buscar_observaciones pero en todos los vaults, no solo el del repo actual."""
    out = []
    for proyecto, vault in todos_los_vaults(cfg):
        obs = buscar_observaciones(proyecto, vault, termino, tope_por_proyecto, crudo)
        out.extend((proyecto, oid, titulo, fecha) for oid, titulo, fecha in obs)
    return out


def buscar_global_documentos(cfg, termino, tope_por_proyecto=TOPE_GLOBAL):
    """Como buscar_en_documentos pero en todos los vaults."""
    out = []
    for proyecto, vault in todos_los_vaults(cfg):
        docs = buscar_en_documentos(vault, termino, tope_por_proyecto)
        out.extend((proyecto, d) for d in docs)
    return out

def buscar_global_sesiones(cfg, termino, tope_por_proyecto=TOPE_GLOBAL, crudo=False):
    """Como buscar_sesiones pero en todos los vaults."""
    out = []
    for proyecto, vault in todos_los_vaults(cfg):
        for sid, titulo, fecha in buscar_sesiones(
                proyecto, vault, termino, tope_por_proyecto, crudo):
            out.append((proyecto, sid, titulo, fecha))
    return out


def frontmatter(texto, clave):
    m = re.search(rf"^{clave}: (.+)$", texto, re.M)
    return m.group(1).strip() if m else ""


def seccion(texto, titulo):
    m = re.search(rf"^## {titulo}\n\n(.*?)(?=\n## |\n---\n|\Z)", texto, re.M | re.S)
    return m.group(1).strip() if m else ""


def buscar(vault, termino):
    """Notas de codigo y documento cuyo nombre encaja con el termino."""
    # los nombres de nota son la ruta con / convertido en _, asi que se compara
    # sobre una forma normalizada: da igual como escriba el separador quien busca
    hits = []
    t = re.sub(r"[^a-z0-9.]+", "_", termino.lower()).strip("_")
    for sub in ("code", "docs"):
        d = os.path.join(vault, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".md") and t in re.sub(r"[^a-z0-9.]+", "_", f.lower()):
                hits.append((sub, os.path.join(d, f)))
    return hits


def quien_cita(vault, clave_slug):
    """Documentos del vault que citan esta nota de codigo."""
    d = os.path.join(vault, "docs")
    out = []
    if not os.path.isdir(d):
        return out
    aguja = f"[[{clave_slug}]]"
    for f in sorted(os.listdir(d)):
        if not f.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, f), errors="replace") as fh:
                if aguja in fh.read():
                    out.append(f[:-3])
        except OSError:
            continue
    return out


def mostrar_indice(vault, hits, tope=TOPE_OBS):
    for sub, ruta in hits:
        texto = open(ruta, errors="replace").read()
        nombre = os.path.basename(ruta)[:-3]
        titulo = frontmatter(texto, "graft_path") or frontmatter(texto, "doc_path")
        print(f"\n=== {titulo or nombre}   [{sub}]")
        if sub == "code":
            variantes = frontmatter(texto, "variants")
            print(f"    {frontmatter(texto, 'symbols')} simbolos · "
                  f"{frontmatter(texto, 'observations')} observaciones"
                  + (f" · {variantes} versiones divergidas" if variantes not in ("1", "") else ""))
            citan = quien_cita(vault, nombre)
            if citan:
                print(f"    citado en {len(citan)} documentos: {', '.join(citan[:3])}")
        coms = [m for m in (re.match(r"- \[\[commit-(\w+)\]\] — (.*)", l.strip())
                            for l in seccion(texto, "Commits").splitlines()) if m]
        if coms:
            print(f"\n    Commits ({len(coms)}, mas reciente primero):")
            for m in coms[:5]:
                print(f"      {m.group(1)}  {m.group(2)[:88]}")
            if len(coms) > 5:
                print(f"      ... y {len(coms) - 5} mas")
        mem = seccion(texto, "Memoria")
        if mem:
            entradas = [m for m in (re.match(r"- \[\[obs-(\d+)\]\] — (.*)", l.strip())
                                    for l in mem.splitlines()) if m]
            print("\n    Memoria (mas reciente primero):")
            for m in entradas[:tope]:
                print(f"      {m.group(1):>7}  {m.group(2)[:96]}")
            if len(entradas) > tope:
                print(f"      ... y {len(entradas) - tope} mas — añade --all para verlas todas")
        else:
            print("    (sin memoria asociada — nadie ha trabajado esto con Claude)")
    print("\nAbrir una observacion:  vault_lookup.py --obs <id>")


def mostrar_obs(vault, ids):
    for i in ids:
        ruta = os.path.join(vault, "memory", f"obs-{i}.md")
        if not os.path.exists(ruta):
            print(f"[obs-{i} no existe en este vault]")
            continue
        print(open(ruta, errors="replace").read())
        print("\n" + "-" * 60)

def mostrar_ses(vault, ids):
    for i in ids:
        sid = i[4:] if i.startswith("ses-") else i
        ruta = os.path.join(vault, "sessions", slug_ses(sid) + ".md")
        if not os.path.exists(ruta):
            print(f"[la sesion {sid} no existe en este vault]")
            continue
        print(open(ruta, errors="replace").read())
        print("\n" + "-" * 60)


def mostrar_ses_global(cfg, refs):
    base = os.path.expanduser(cfg.get("vault_dir", "~/vaults"))
    for ref in refs:
        proyecto, sep, sid = ref.partition(":")
        if not sep:
            print(f"[{ref!r} necesita el formato proyecto:id con --global]")
            continue
        mostrar_ses(os.path.join(base, f"{proyecto}-graft-mem"), [sid])


def mostrar_stats(proyecto, vault):
    idx = os.path.join(vault, "INDEX.md")
    print(f"proyecto: {proyecto}\nvault: {vault}")
    if os.path.exists(idx):
        for linea in open(idx, errors="replace").read().splitlines():
            if linea.startswith("- ") and "**" in linea:
                print("  " + linea[2:])


def mostrar_contenido(proyecto, vault, termino, tope):
    obs = buscar_observaciones(proyecto, vault, termino, tope)
    ses = buscar_sesiones(proyecto, vault, termino, max(3, tope // 3))
    docs = buscar_en_documentos(vault, termino, tope)
    if obs:
        print(f"\n=== observaciones que mencionan {termino!r} (por relevancia)")
        for oid, titulo, fecha in obs:
            print(f"    {oid:>7}  {fecha}  {titulo[:88]}")
    if ses:
        print(f"\n=== sesiones que ya trabajaron esto ({len(ses)})")
        for sid, titulo, fecha in ses:
            print(f"    {fecha}  {titulo[:88]}")
            print(f"             --ses {sid}")
    if docs:
        print(f"\n=== documentos que lo mencionan ({len(docs)})")
        for d in docs:
            print(f"      {d}")
    return bool(obs or ses or docs)


def mostrar_global(termino, obs, docs, ses=()):
    if not obs and not docs and not ses:
        print(f"nada en ningun vault para {termino!r}")
        return
    if obs:
        print(f"\n=== observaciones en todos los proyectos para {termino!r} "
              f"(top {TOPE_GLOBAL} por proyecto)")
        for proyecto, oid, titulo, fecha in obs:
            print(f"    [{proyecto:<14}] {oid:>7}  {fecha}  {titulo[:80]}")
    if ses:
        print(f"\n=== sesiones en todos los proyectos ({len(ses)})")
        for proyecto, sid, titulo, fecha in ses:
            print(f"    [{proyecto:<14}] {fecha}  {titulo[:74]}")
            print(f"                     --global --ses {proyecto}:{sid}")
    if docs:
        print(f"\n=== documentos en todos los proyectos ({len(docs)})")
        for proyecto, d in docs:
            print(f"      [{proyecto}] {d}")
    print("\nAbrir una observacion:  vault_lookup.py --global --obs proyecto:id")


def mostrar_obs_global(cfg, refs):
    base = os.path.expanduser(cfg.get("vault_dir", "~/vaults"))
    for ref in refs:
        proyecto, sep, oid = ref.partition(":")
        if not sep:
            print(f"[{ref!r} necesita el formato proyecto:id con --global]")
            continue
        mostrar_obs(os.path.join(base, f"{proyecto}-graft-mem"), [oid])


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args and "--stats" not in flags and "--global" not in flags:
        sys.exit(__doc__)

    if "--global" in flags:
        cfg = cargar_cfg()
        if "--obs" in flags:
            return mostrar_obs_global(cfg, args)
        if "--ses" in flags:
            return mostrar_ses_global(cfg, args)
        if not args:
            sys.exit("vault_lookup.py --global <termino>   (o --global --obs proyecto:id)")
        termino = " ".join(args)
        tope = 10 ** 6 if "--all" in flags else TOPE_GLOBAL
        obs = buscar_global_observaciones(cfg, termino, tope)
        docs = buscar_global_documentos(cfg, termino, tope)
        ses = buscar_global_sesiones(cfg, termino, tope)
        return mostrar_global(termino, obs, docs, ses)

    proyecto, vault = resolver_vault()
    tope = 10 ** 6 if "--all" in flags else TOPE_OBS
    tope_txt = 10 ** 6 if "--all" in flags else TOPE_TEXTO

    if "--stats" in flags:
        return mostrar_stats(proyecto, vault)
    if "--obs" in flags:
        return mostrar_obs(vault, args)
    if "--ses" in flags:
        return mostrar_ses(vault, args)
    if "--doc" in flags:
        ruta = os.path.join(vault, "docs", re.sub(r"[^A-Za-z0-9._-]", "_", args[0]) + ".md")
        if not os.path.exists(ruta):
            sys.exit(f"no existe {ruta}")
        return print(open(ruta, errors="replace").read())

    termino = " ".join(args)

    # Clasificacion automatica: quien consulta no tiene por que saber de que tipo
    # es su pregunta. Solo digitos es un id; algo con barra o extension es un
    # fichero; lo demas es un concepto y se busca en el contenido.
    if termino.isdigit():
        return mostrar_obs(vault, [termino])

    if "--text" in flags:
        if not mostrar_contenido(proyecto, vault, termino, tope_txt):
            print(f"nada en el vault de {proyecto} para {termino!r}")
        return

    hits = buscar(vault, termino)
    if hits and "--name" not in flags and len(hits) > 12:
        print(f"{len(hits)} ficheros coinciden por nombre, se muestran 12:")
        hits = hits[:12]
    if hits:
        mostrar_indice(vault, hits, tope)
    if "--name" in flags:
        if not hits:
            print(f"ningun fichero del vault de {proyecto} se llama asi: {termino!r}")
        return

    # Si el termino no era una ruta, o no encontro fichero, se busca en el
    # contenido sin que nadie tenga que pedirlo con otro flag.
    algo = False
    if not hits or not parece_ruta(termino):
        algo = mostrar_contenido(proyecto, vault, termino, tope_txt)
        if algo:
            print("\nAbrir una observacion:  vault_lookup.py <id>")
    if not hits and not algo:
        print(f"nada en el vault de {proyecto} para {termino!r}")
        print("prueba otro termino, o --stats para ver que hay")


if __name__ == "__main__":
    main()
