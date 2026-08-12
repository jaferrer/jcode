#!/usr/bin/env python3
"""Refresca todos los vaults de golpe y los da de alta en Obsidian.

    python3 refresh_vaults.py [--only PROY] [--no-register] [--no-build] [--dry-run]
    python3 refresh_vaults.py --register-only     # solo alta en Obsidian

La configuracion vive en ~/.config/graft-mem-vault/projects.json. Si no existe,
se genera por descubrimiento (proyectos de claude-mem con hub en ~/ai/HUB y un
minimo de observaciones) y se deja escrita para que la edites: ahi es donde se
declaran las raices acotadas de un proyecto, que si no habria que repetir en cada
invocacion.

Antes de generar cada vault se refrescan con `graft build` los indices que ya
existen, para que el vault no se construya sobre un grafo de codigo viejo. Se
desactiva con --no-build.

El alta en Obsidian se hace escribiendo en su registro de vaults. Solo añade
entradas, nunca borra, y guarda copia previa. Obsidian reescribe ese fichero al
cerrarse, asi que conviene tenerlo cerrado al refrescar.
"""
import json
import os
import secrets
import shutil
import subprocess
import sqlite3
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
GENERADOR = os.path.join(AQUI, "graft_mem_vault.py")
CONFIG = os.path.expanduser(os.environ.get(
    "GRAFT_MEM_VAULT_CONFIG", "~/.config/graft-mem-vault/projects.json"))
DB = os.path.expanduser(os.environ.get("CLAUDE_MEM_DB", "~/.claude-mem/claude-mem.db"))
# donde viven los hubs de proyecto; en otra maquina puede no ser ~/ai/HUB
HUBS = os.path.expanduser(os.environ.get("GRAFT_MEM_VAULT_HUBS", "~/ai/HUB"))
MIN_OBS = int(os.environ.get("GRAFT_MEM_VAULT_MIN_OBS", "80"))


def ruta_obsidian():
    """Registro de vaults de Obsidian, segun plataforma."""
    if os.environ.get("OBSIDIAN_CONFIG"):
        return os.path.expanduser(os.environ["OBSIDIAN_CONFIG"])
    candidatos = [
        "~/Library/Application Support/obsidian/obsidian.json",   # macOS
        "~/.config/obsidian/obsidian.json",                       # Linux
        os.path.join(os.environ.get("APPDATA", "~"), "obsidian", "obsidian.json"),
    ]
    for c in candidatos:
        r = os.path.expanduser(c)
        if os.path.exists(r):
            return r
    return os.path.expanduser(candidatos[0])


OBSIDIAN = ruta_obsidian()


def descubrir():
    """Proyectos de claude-mem que tienen hub y observaciones suficientes."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    filas = con.execute(
        "SELECT project, count(*) FROM observations"
        " WHERE files_modified NOT IN ('', '[]') OR files_read NOT IN ('', '[]')"
        " GROUP BY project HAVING count(*) >= ? ORDER BY count(*) DESC",
        (MIN_OBS,),
    ).fetchall()
    proyectos = {}
    for nombre, _ in filas:
        hub = os.path.join(HUBS, nombre)
        if os.path.isdir(hub):
            proyectos[nombre] = {"hub": hub, "roots": []}
    return proyectos


def cargar_config():
    if os.path.exists(CONFIG):
        with open(CONFIG) as fh:
            return json.load(fh)
    cfg = {"vault_dir": os.path.expanduser("~/vaults"), "projects": descubrir()}
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    print(f"configuracion generada en {CONFIG} ({len(cfg['projects'])} proyectos)")
    print("editala para declarar raices acotadas (--root) de un proyecto concreto\n")
    return cfg


def obsidian_abierto():
    """El binario se llama Obsidian en macOS y obsidian en Linux."""
    for nombre in ("Obsidian", "obsidian"):
        try:
            if subprocess.run(["pgrep", "-x", nombre],
                              capture_output=True).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            return False  # sin pgrep no podemos saberlo; mejor no bloquear
    return False


def registrar_en_obsidian(rutas, dry_run=False):
    """Da de alta los vaults en el registro de Obsidian. Solo añade.

    Obsidian reescribe su registro al cerrarse, asi que escribir con la app
    abierta se pierde sin avisar. Se aborta el alta y se dice que hacer.
    """
    if not os.path.exists(OBSIDIAN):
        print(f"aviso: no encuentro {OBSIDIAN}; ¿Obsidian instalado?")
        return 0
    if obsidian_abierto() and not dry_run:
        print("\nObsidian esta abierto: no doy de alta los vaults porque al cerrarse")
        print("reescribiria su registro y se perderia. Cierralo y ejecuta:")
        print(f"    python3 {os.path.basename(__file__)} --register-only")
        return 0
    with open(OBSIDIAN) as fh:
        datos = json.load(fh)
    vaults = datos.setdefault("vaults", {})
    ya = {v.get("path") for v in vaults.values()}
    nuevos = [r for r in rutas if r not in ya]
    if not nuevos:
        return 0
    if dry_run:
        print(f"[dry-run] daria de alta {len(nuevos)} vaults en Obsidian")
        return len(nuevos)
    shutil.copy2(OBSIDIAN, OBSIDIAN + ".bak")
    for ruta in nuevos:
        vaults[secrets.token_hex(8)] = {"path": ruta, "ts": int(time.time() * 1000)}
    with open(OBSIDIAN, "w") as fh:
        json.dump(datos, fh, separators=(",", ":"))
    return len(nuevos)


def main():
    solo = None
    registrar = True
    dry_run = False
    solo_registrar = False
    build = True
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--only" and i + 1 < len(args):
            solo = args[i + 1]
        elif a == "--no-register":
            registrar = False
        elif a == "--dry-run":
            dry_run = True
        elif a == "--register-only":
            solo_registrar = True
        elif a == "--no-build":
            build = False

    cfg = cargar_config()
    if solo_registrar:
        base = os.path.expanduser(cfg.get("vault_dir", "~/vaults"))
        rutas = [os.path.join(base, d) for d in sorted(os.listdir(base))
                 if os.path.isfile(os.path.join(base, d, "INDEX.md"))]
        n = registrar_en_obsidian(rutas, dry_run)
        print(f"Obsidian: {n} vaults dados de alta de {len(rutas)} encontrados")
        if n:
            print("abre Obsidian y los tendras en el selector de vaults")
        return 0
    vault_dir = os.path.expanduser(cfg.get("vault_dir", "~/vaults"))
    proyectos = cfg["projects"]
    if solo:
        if solo not in proyectos:
            sys.exit(f"{solo!r} no esta en {CONFIG}")
        proyectos = {solo: proyectos[solo]}

    generados = []
    fallos = []
    for nombre, conf in proyectos.items():
        destino = os.path.join(vault_dir, f"{nombre}-graft-mem")
        # claude-mem archiva por directorio de trabajo, asi que un proyecto puede
        # quedar repartido en varios nombres; "also" los une en un unico vault
        nombres = ",".join([nombre] + list(conf.get("also", [])))
        cmd = [sys.executable, GENERADOR, os.path.expanduser(conf["hub"]), nombres, destino]
        for r in conf.get("roots", []):
            cmd.append(f"--root={os.path.expanduser(r)}")
        for clave, flag in (("min_refs", "--min-refs"), ("max_files", "--max-files")):
            if clave in conf:
                cmd.append(f"{flag}={conf[clave]}")
        if conf.get("no_docs"):
            cmd.append("--no-docs")
        if conf.get("no_commits"):
            cmd.append("--no-commits")
        if conf.get("no_sessions"):
            cmd.append("--no-sessions")
        if build and not conf.get("no_build"):
            cmd.append("--build")
        if dry_run:
            print("[dry-run] " + " ".join(cmd))
            continue
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            fallos.append((nombre, r.stderr.strip().splitlines()[:1]))
            print(f"  ✗ {nombre}: {r.stderr.strip().splitlines()[:1]}")
            continue
        resumen = {}
        for linea in r.stdout.splitlines():
            if ":" in linea and linea.startswith("  "):
                k, _, v = linea.strip().partition(":")
                resumen[k.strip()] = v.strip()
        print(f"  ✓ {nombre:<18} cod={resumen.get('notas de codigo', '?'):>5}"
              f" docs={resumen.get('notas de documento', '?').split(' ')[0]:>5}"
              f" ses={resumen.get('notas de sesion', '?').split(' ')[0]:>4}"
              f" commits={resumen.get('notas de commit', '?').split(' ')[0]:>5}"
              f" historia={resumen.get('con historia (memoria o commits)', '?')}")
        generados.append(destino)

    if registrar and generados:
        n = registrar_en_obsidian(generados, dry_run)
        print(f"\nObsidian: {n} vaults dados de alta"
              f"{' (copia previa en obsidian.json.bak)' if n and not dry_run else ''}")
        if n:
            print("reinicia Obsidian para verlos en el selector de vaults")
    if fallos:
        print(f"\n{len(fallos)} proyectos fallaron: {', '.join(n for n, _ in fallos)}")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
