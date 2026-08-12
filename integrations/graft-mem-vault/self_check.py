#!/usr/bin/env python3
"""Autocomprobación de graft_mem_vault: HUB+SPOKE, join, enlaces y escapado.

Construye un hub, dos spokes (uno legítimo y otro con índice desmesurado) y una
base de claude-mem sintéticos en un directorio temporal, genera el vault y
verifica el resultado. No toca ~/.claude-mem ni ningún repo real.

Uso: python3 self_check.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graft_mem_vault as G
import vault_lookup as L


def nodo_fichero(rel, h):
    return {"id": rel, "kind": "file", "path": rel, "name": os.path.basename(rel), "body_hash": h}


def git(raiz, *args):
    import subprocess
    return subprocess.run(["git", "-C", raiz, *args], capture_output=True, text=True)


def escribir_repo(raiz, nodos, aristas, real=False):
    """Crea un repo con su wiring.json. Con real=True, un repo git de verdad."""
    if real:
        os.makedirs(raiz, exist_ok=True)
        git(raiz, "init", "-q")
        git(raiz, "config", "user.email", "check@example.invalid")
        git(raiz, "config", "user.name", "self check")
    else:
        os.makedirs(os.path.join(raiz, ".git"))
    os.makedirs(os.path.join(raiz, "graft/.graph"))
    with open(os.path.join(raiz, "graft/.graph/wiring.json"), "w") as fh:
        json.dump({"nodes": nodos, "edges": aristas}, fh)


def crear_db(ruta, obs):
    con = sqlite3.connect(ruta)
    con.execute(
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, project TEXT, title TEXT,"
        " subtitle TEXT, text TEXT, facts TEXT, narrative TEXT, concepts TEXT, type TEXT,"
        " created_at TEXT, created_at_epoch INTEGER, files_modified TEXT, files_read TEXT,"
        " memory_session_id TEXT)"
    )
    for i, (oid, titulo, narrativa, fm, fr) in enumerate(obs):
        con.execute(
            "INSERT INTO observations VALUES (?,'PRUEBA',?,NULL,NULL,'[]',?,'[]','discovery',"
            "'2026-08-07',?,?,?,?)",
            (oid, titulo, narrativa, 1786000000 + i, json.dumps(fm), json.dumps(fr),
             "sesion-a" if oid in (1, 3) else "sesion-b"),
        )
    # una observacion archivada bajo otro nombre de proyecto: pasa al trabajar
    # desde un spoke, porque claude-mem archiva por directorio de trabajo
    con.execute(
        "INSERT INTO observations VALUES (999,'OTRO','Trabajo archivado con otro nombre',"
        "NULL,NULL,'[]','se trabajo desde el spoke','[]','discovery','2026-08-07',"
        "1786000999,?,'[]','sesion-c')",
        (json.dumps(["pagos/orden.py"]),),
    )
    # Resumenes de sesion, uno por prompt, como los escribe claude-mem. La sesion B
    # se queda sin ninguno a proposito: debe generar nota igual, avisando de que no
    # hay resumen, porque la agrupacion de sus observaciones ya vale por si sola.
    con.execute(
        "CREATE TABLE session_summaries (id INTEGER PRIMARY KEY, memory_session_id TEXT,"
        " project TEXT, request TEXT, investigated TEXT, learned TEXT, completed TEXT,"
        " next_steps TEXT, notes TEXT, prompt_number INTEGER, created_at TEXT,"
        " created_at_epoch INTEGER)"
    )
    for i, (req, inv, apr) in enumerate((
        ("Arreglar el cobro que elige mal el diario", "se leyo pagos/linea.py",
         "el diario se elegia por tipo con limit=1"),
        ("Confirmar que no rompe la orden", "se reviso [[esto-no]] en el dominio",
         "la orden llama a cobrar y no cambia"),
    ), 1):
        con.execute(
            "INSERT INTO session_summaries VALUES (?,'sesion-a','PRUEBA',?,?,?,"
            "'hecho','nada pendiente',NULL,?,'2026-08-07',?)",
            (i, req, inv, apr, i, 1786000000 + i),
        )
    con.execute("CREATE VIRTUAL TABLE session_summaries_fts USING fts5(request,"
                " investigated, learned, completed, next_steps, notes,"
                " content='session_summaries', content_rowid='id')")
    con.execute("INSERT INTO session_summaries_fts(rowid, request, investigated, learned,"
                " completed, next_steps, notes) SELECT id, request, investigated, learned,"
                " completed, next_steps, notes FROM session_summaries")
    con.execute("CREATE VIRTUAL TABLE observations_fts USING fts5(title, subtitle,"
                " narrative, text, content='observations', content_rowid='id')")
    con.execute("INSERT INTO observations_fts(rowid, title, subtitle, narrative, text)"
                " SELECT id, title, subtitle, narrative, text FROM observations")
    con.commit()
    con.close()


def main():
    fallos = []
    with tempfile.TemporaryDirectory() as tmp:
        hub = os.path.join(tmp, "HUB", "PRUEBA")
        spoke = os.path.join(tmp, "spokes", "modulo")
        gordo = os.path.join(tmp, "spokes", "vendorizado")

        # El hub y el spoke tienen linea.py con distinto body_hash: dos checkouts
        # divergidos del mismo fichero logico. Debe fusionarse igual, avisando.
        escribir_repo(hub, [
            nodo_fichero("pagos/linea.py", "AAA"),
            nodo_fichero("pagos/huerfano.py", "BBB"),
            {"id": "pagos/linea.py#cobrar", "kind": "method", "path": "pagos/linea.py",
             "name": "cobrar", "span": "L10-L20", "signature": "def cobrar(self)"},
        ], [], real=True)

        # commits reales: uno con sustancia sobre un fichero del vault, uno trivial
        # que debe filtrarse, y uno que solo toca ficheros ajenos al vault
        os.makedirs(os.path.join(hub, "pagos"), exist_ok=True)
        for nombre, mensaje in (
            ("pagos/linea.py", "fix(cobros): el diario se elegia por tipo con limit=1\n\n"
                               "Detalle en el cuerpo del commit."),
            ("pagos/linea.py", "wip"),
            ("ajeno.txt", "docs: algo que no toca codigo del vault"),
        ):
            ruta = os.path.join(hub, nombre)
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
            with open(ruta, "a") as fh:
                fh.write("x\n")
            git(hub, "add", nombre)
            git(hub, "commit", "-q", "-m", mensaje)

        escribir_repo(spoke, [
            nodo_fichero("pagos/linea.py", "ZZZ"),  # checkout divergido del mismo fichero
            nodo_fichero("pagos/orden.py", "CCC"),
            {"id": "pagos/orden.py#lanzar", "kind": "method", "path": "pagos/orden.py",
             "name": "lanzar", "span": "L5-L9", "signature": "def lanzar(self)"},
        ], [
            {"source": "pagos/orden.py", "target": "pagos/linea.py#cobrar", "relation": "calls"},
        ])
        # documentos markdown: no los indexa graft, los recoge load_docs
        os.makedirs(os.path.join(hub, "thoughts", "handoffs"))
        with open(os.path.join(hub, "thoughts", "handoffs", "traspaso.md"), "w") as fh:
            fh.write("# Traspaso\n\nEl bug vive en pagos/linea.py y hay que revisar\n"
                     "tambien pagos/orden.py antes de cerrar. El perfil afectado es\n"
                     "perfiles/developer.yaml. Ojo con [[esto]].\n")
        with open(os.path.join(hub, "CLAUDE.md"), "w") as fh:
            fh.write("# CLAUDE.md\n\nInstrucciones del proyecto.\n")
        # dos rutas que solo difieren en mayusculas: en macOS y Windows el
        # sistema de ficheros no las distingue y una nota pisaria a la otra
        os.makedirs(os.path.join(hub, ".github"))
        with open(os.path.join(hub, ".github", "TEMPLATE.md"), "w") as fh:
            fh.write("# plantilla en mayusculas\n")

        os.makedirs(os.path.join(spoke, ".github"), exist_ok=True)
        with open(os.path.join(spoke, ".github", "template.md"), "w") as fh:
            fh.write("# plantilla en minusculas\n")

        # configuracion: la capa de decision. Entra el yaml y el json de la config
        # de agentes; el lockfile, el secreto y el json de datos se quedan fuera.
        os.makedirs(os.path.join(hub, "perfiles"))
        with open(os.path.join(hub, "perfiles", "developer.yaml"), "w") as fh:
            fh.write("perfil: developer\nrules:\n  - tldr-over-grep\n"
                     "nota: |\n  ```no debe romper el bloque```\n"
                     "matriz: [[1, 2], [3, 4]]\nregex: '[[:space:]]+'\n")
        os.makedirs(os.path.join(hub, ".claude"))
        with open(os.path.join(hub, ".claude", "settings.json"), "w") as fh:
            fh.write('{"hooks": {"PreToolUse": []}}\n')
        with open(os.path.join(hub, ".claude", "settings.local.json"), "w") as fh:
            fh.write('{"env": {"UN_TOKEN": "no-debe-entrar"}}\n')
        with open(os.path.join(hub, "package-lock.json"), "w") as fh:
            fh.write('{"lockfileVersion": 3}\n')
        with open(os.path.join(hub, "secrets.yaml"), "w") as fh:
            fh.write("clave: no-debe-entrar\n")
        os.makedirs(os.path.join(hub, "datos"))
        with open(os.path.join(hub, "datos", "tabla.json"), "w") as fh:
            fh.write('{"filas": 3}\n')

        escribir_repo(gordo, [nodo_fichero(f"vendor/dep{i}/mod.py", f"H{i}") for i in range(60)], [])

        # Nueve referencias al spoke gordo: supera MIN_REFS, asi que entra como raiz
        # y solo la guarda de tamaño debe frenarlo.
        obs = [
            (1, "Bug en el cobro", "corregido el diario",
             [os.path.join(hub, "pagos/linea.py")], []),
            (2, "Orden revisada", "usa [[campo_x]] en el dominio",
             [os.path.join(spoke, "pagos/orden.py")], []),
            (3, "Misma linea desde el spoke", "sin cambios",
             [], [os.path.join(spoke, "pagos/linea.py")]),
            (4, "Ruta relativa suelta", "claude-mem no siempre guarda la raiz",
             ["pagos/orden.py"], []),
            (5, "Fuera del proyecto", "toca un repo ajeno", ["/tmp/ajeno/README.md"], []),
            (6, "Handoff actualizado", "se dejo el traspaso escrito",
             [os.path.join(hub, "thoughts/handoffs/traspaso.md")], []),
        ] + [
            # el spoke necesita superar MIN_REFS para ser reconocido como tal
            (20 + i, f"Trabajo en el spoke {i}", "iteracion",
             [os.path.join(spoke, "pagos/orden.py")], [])
            for i in range(5)
        ] + [
            (10 + i, f"Vendor {i}", "ruido", [os.path.join(gordo, f"vendor/dep{i}/mod.py")], [])
            for i in range(9)
        ]

        db = os.path.join(tmp, "mem.db")
        crear_db(db, obs)
        G.DB = db  # nunca la base real

        vault = os.path.join(tmp, "vault")
        sys.argv = ["self_check", hub, "PRUEBA", vault, "--max-files=50"]
        G.main()

        code, memory = os.path.join(vault, "code"), os.path.join(vault, "memory")
        idx = open(os.path.join(vault, "INDEX.md")).read()

        # 1. El spoke se descubre solo, sin estar declarado en ninguna parte.
        if spoke not in idx:
            fallos.append("el spoke no se descubrio a partir de las observaciones")
        if "/tmp/ajeno" in idx:
            fallos.append("un repo ajeno de una sola referencia entro como raiz")

        # 2. El indice desmesurado se descarta y se avisa, no se cuela en el vault.
        if "descartado" not in idx:
            fallos.append("el indice desmesurado no se marco como descartado")
        if any("mod.py" in f for f in os.listdir(code)):
            fallos.append("ficheros del arbol vendorizado colados en el vault")

        # 3. Hub y spoke tienen linea.py con contenido divergido: se fusiona igual,
        #    porque es el mismo fichero logico, y se avisa de la divergencia.
        notas_linea = [f for f in os.listdir(code) if f.endswith("linea.py.md")]
        if len(notas_linea) != 1:
            fallos.append(f"linea.py deberia fusionarse en 1 nota, hay {len(notas_linea)}")
        else:
            linea = open(os.path.join(code, notas_linea[0])).read()
            for esperado in ("[[obs-1]]", "[[obs-3]]", "cobrar", "L10-L20", "## Llamado por"):
                if esperado not in linea:
                    fallos.append(f"falta {esperado!r} en la nota fusionada de linea.py")
            if linea.count("**cobrar**") != 1:
                fallos.append("simbolo duplicado al fusionar hub y spoke")
            if "han divergido" not in linea:
                fallos.append("no se avisa de que los checkouts divergen")
            if "variants: 2" not in linea:
                fallos.append("no se registra el numero de variantes")

        # 4. Observacion del spoke y ruta relativa suelta enganchan con orden.py.
        orden = open(os.path.join(code, G.slug("pagos/orden.py") + ".md")).read()
        for esperado in ("[[obs-2]]", "[[obs-4]]", "## Llama a"):
            if esperado not in orden:
                fallos.append(f"falta {esperado!r} en la nota de orden.py")

        # 5. La ruta de un repo ajeno no genera nota.
        if os.path.exists(os.path.join(memory, "obs-5.md")):
            fallos.append("una ruta de repo ajeno genero nota de memoria")

        # 6. La prosa con [[...]] queda neutralizada.
        if "[[campo_x]]" in open(os.path.join(memory, "obs-2.md")).read():
            fallos.append("prosa con [[...]] no escapada: genera enlaces fantasma")

        # 7. El fichero sin memoria sale como punto ciego, no desaparece.
        if G.slug("pagos/huerfano.py") not in idx:
            fallos.append("el fichero sin observaciones no figura como punto ciego")

        # 8. Una segunda pasada limpia las notas huerfanas de la anterior.
        huerfana = os.path.join(code, "obsoleta_de_una_pasada_previa.md")
        open(huerfana, "w").write("# resto de una generacion anterior")
        G.main()
        if os.path.exists(huerfana):
            fallos.append("la regeneracion no borro las notas obsoletas")

        # 9. Un destino ajeno (sin nuestra firma) no se toca.
        ajeno = os.path.join(tmp, "vault-ajeno")
        os.makedirs(os.path.join(ajeno, "code"))
        propia = os.path.join(ajeno, "code", "notas-mias.md")
        open(propia, "w").write("# notas escritas a mano")
        sys.argv = ["self_check", hub, "PRUEBA", ajeno, "--max-files=50"]
        try:
            G.main()
            fallos.append("se escribio sobre un directorio sin la firma del generador")
        except SystemExit:
            pass
        if not os.path.exists(propia):
            fallos.append("se borraron notas de un directorio ajeno")

        # 10. Los documentos markdown entran como notas propias, con su contenido.
        docs = os.path.join(vault, "docs")
        nota_doc = os.path.join(docs, G.slug("thoughts/handoffs/traspaso.md") + ".md")
        if not os.path.exists(nota_doc):
            fallos.append("el handoff no genero nota de documento")
        else:
            d = open(nota_doc).read()
            if "El bug vive en" not in d:
                fallos.append("la nota de documento no lleva el contenido completo")
            if "[[esto]]" in d:
                fallos.append("prosa del documento con [[...]] no escapada")
            if "[[obs-6]]" not in d:
                fallos.append("la observacion que toca el handoff no esta enlazada")
            # el triangulo: el documento cita codigo y debe enlazar con su nota
            for esperado in (G.slug("pagos/linea.py"), G.slug("pagos/orden.py")):
                if f"[[{esperado}]]" not in d:
                    fallos.append(f"el documento no enlaza el codigo que cita: {esperado}")
        if not os.path.exists(os.path.join(docs, G.slug("CLAUDE.md") + ".md")):
            fallos.append("CLAUDE.md no genero nota de documento")

        # 11. vault_lookup encuentra la nota escriba como escriba el separador,
        #     y resuelve el proyecto desde la raiz mas especifica que contenga el cwd.
        for termino in ("pagos/linea.py", "pagos_linea.py", "linea.py"):
            if not L.buscar(vault, termino):
                fallos.append(f"vault_lookup no encuentra {termino!r}")
        if L.buscar(vault, "no_existe_este_fichero.py"):
            fallos.append("vault_lookup inventa coincidencias")
        cfg = {"vault_dir": os.path.dirname(vault),
               "projects": {"PRUEBA": {"hub": hub, "roots": [spoke]}}}
        cfg_path = os.path.join(tmp, "projects.json")
        json.dump(cfg, open(cfg_path, "w"))
        L.CONFIG = cfg_path
        os.rename(vault, os.path.join(os.path.dirname(vault), "PRUEBA-graft-mem"))
        try:
            proy, _ = L.resolver_vault(os.path.join(spoke, "pagos"))
            if proy != "PRUEBA":
                fallos.append(f"vault_lookup resuelve mal el proyecto: {proy}")
        except SystemExit as e:
            fallos.append(f"vault_lookup no resuelve el vault desde el spoke: {e}")
        try:
            L.resolver_vault("/tmp")
            fallos.append("vault_lookup resuelve un directorio fuera de todo proyecto")
        except SystemExit:
            pass

        # 12. La clasificacion automatica: no hay que decirle de que tipo es la consulta.
        L.DB = db
        import io, contextlib

        def salida(argv):
            """Salida de vault_lookup, incluido el mensaje de un sys.exit().

            sys.exit("mensaje") no escribe en stdout al capturarse la excepcion,
            asi que sin esto un fallo real se veria como salida vacia.
            """
            buf = io.StringIO()
            sys.argv = ["vault_lookup"] + argv
            try:
                with contextlib.redirect_stdout(buf):
                    L.main()
            except SystemExit as e:
                if e.code and not isinstance(e.code, int):
                    return buf.getvalue() + str(e.code)
            return buf.getvalue()

        os.chdir(hub)  # resolver_vault mira el cwd
        # ruta -> ficha del fichero
        if "simbolos" not in salida(["pagos/linea.py"]):
            fallos.append("una ruta no se clasifica como consulta de fichero")
        # solo digitos -> abre esa observacion
        if "Bug en el cobro" not in salida(["1"]):
            fallos.append("un id numerico no abre la observacion")
        # concepto -> busca en el contenido via FTS5, sin pedir flag
        cont = salida(["diario"])
        if "corregido el diario" not in cont and "Bug en el cobro" not in cont:
            fallos.append("un concepto no dispara la busqueda por contenido")
        # termino inexistente -> lo dice, no revienta
        if "nada en el vault" not in salida(["zzz_inexistente_xyz"]):
            fallos.append("un termino sin resultados no avisa")
        # sin FTS disponible la busqueda por contenido devuelve vacio, no explota
        L.DB = "/no/existe/mem.db"
        if L.buscar_observaciones("PRUEBA", vault, "diario"):
            fallos.append("buscar_observaciones inventa datos sin base")
        # 13. Rutas con enlace simbolico: en macOS /var -> /private/var y
        #     ~/.claude -> ~/.claude-v47. Comparar sin resolver no encuentra nada.
        enlace = os.path.join(tmp, "enlace-al-hub")
        try:
            os.symlink(hub, enlace)
            proy, _ = L.resolver_vault(enlace)
            if proy != "PRUEBA":
                fallos.append("resolver_vault falla si el hub llega por un enlace")
        except SystemExit:
            fallos.append("resolver_vault no resuelve un hub alcanzado por enlace")
        except OSError:
            pass  # sin permiso para crear enlaces: se omite

        L.DB = db
        os.chdir(tmp)
        os.rename(os.path.join(os.path.dirname(vault), "PRUEBA-graft-mem"), vault)

        # 14. Los commits entran como cuarta categoria, atados por la ruta exacta
        #     que registra git. Los triviales y los que no tocan el vault se caen.
        comdir = os.path.join(vault, "commits")
        notas_com = [f for f in os.listdir(comdir)] if os.path.isdir(comdir) else []
        if not notas_com:
            fallos.append("no se genero ninguna nota de commit")
        else:
            todo = "\n".join(open(os.path.join(comdir, f), errors="replace").read()
                              for f in notas_com)
            if "el diario se elegia por tipo" not in todo:
                fallos.append("el commit con sustancia no genero nota")
            if "Detalle en el cuerpo" not in todo:
                fallos.append("el cuerpo del commit no se incluye")
            if "wip" in todo.lower().split("---")[0]:
                fallos.append("un commit trivial (wip) no se filtro")
            if "algo que no toca codigo" in todo:
                fallos.append("un commit que no toca el vault genero nota")
            linea_nota = open(os.path.join(code, notas_linea[0])).read()
            if "## Commits" not in linea_nota:
                fallos.append("la nota de codigo no lista sus commits")

        # 15. Nombres de nota unicos aunque el sistema de ficheros no distinga
        #     mayusculas: .github/TEMPLATE.md y .github/template.md coexisten.
        nombres = [f for f in os.listdir(os.path.join(vault, "docs"))
                   if "emplate" in f.lower()]
        if len(nombres) != 2:
            fallos.append(f"colision de mayusculas: se esperaban 2 notas, hay {nombres}")

        # 16. Varios nombres de proyecto en un mismo vault: claude-mem archiva por
        #     directorio de trabajo, asi que un proyecto se reparte al usar spokes.
        vault2 = os.path.join(tmp, "vault-multi")
        sys.argv = ["self_check", hub, "PRUEBA,OTRO", vault2, "--max-files=50"]
        G.main()
        multi = open(os.path.join(vault2, "code",
                                  G.slug("pagos/orden.py") + ".md"), errors="replace").read()
        if "[[obs-999]]" not in multi:
            fallos.append("las observaciones del segundo proyecto no se incorporan")
        if "PRUEBA, OTRO" not in open(os.path.join(vault2, "INDEX.md"),
                                      errors="replace").read():
            fallos.append("el INDEX no refleja los dos nombres de proyecto")
        # y con un solo nombre no deben colarse
        if "[[obs-999]]" in open(os.path.join(code, G.slug("pagos/orden.py") + ".md"),
                                 errors="replace").read():
            fallos.append("una observacion de otro proyecto se colo sin pedirlo")

        # 18. --global: busca en todos los vaults, no solo el del repo actual.
        with tempfile.TemporaryDirectory() as gtmp:
            vdir = os.path.join(gtmp, "vaults")
            os.makedirs(vdir)
            gdb = os.path.join(gtmp, "mem.db")
            crear_db(gdb, [(1, "Bug en el cobro", "corregido el diario", [], [])])
            # crear_db siempre siembra 'PRUEBA' (obs 1) y, aparte, 'OTRO' (obs-999):
            # dos proyectos listos para probar que --global cruza ambos
            for proyecto, oid in (("PRUEBA", 1), ("OTRO", 999)):
                mem = os.path.join(vdir, f"{proyecto}-graft-mem", "memory")
                os.makedirs(mem)
                open(os.path.join(vdir, f"{proyecto}-graft-mem", "INDEX.md"), "w").write("# x")
                open(os.path.join(mem, f"obs-{oid}.md"), "w").write("nota")

            gcfg = {"vault_dir": vdir, "projects": {
                "PRUEBA": {"hub": "/no/importa"}, "OTRO": {"hub": "/no/importa"}}}
            gcfg_path = os.path.join(gtmp, "projects.json")
            json.dump(gcfg, open(gcfg_path, "w"))
            L.CONFIG, L.DB = gcfg_path, gdb

            cfg = L.cargar_cfg()
            vaults = {p for p, _ in L.todos_los_vaults(cfg)}
            if vaults != {"PRUEBA", "OTRO"}:
                fallos.append(f"todos_los_vaults no encuentra los dos proyectos: {vaults}")

            globales = L.buscar_global_observaciones(cfg, "diario OR trabajo", crudo=True)
            proyectos_con_hit = {p for p, *_ in globales}
            if proyectos_con_hit != {"PRUEBA", "OTRO"}:
                fallos.append(f"--global no alcanza los dos proyectos: {proyectos_con_hit}")
            # cada proyecto solo debe traer lo suyo, no lo del otro
            for proyecto, oid, *_ in globales:
                if proyecto == "PRUEBA" and oid != 1:
                    fallos.append(f"--global mezcla observaciones entre proyectos: {globales}")
                if proyecto == "OTRO" and oid != 999:
                    fallos.append(f"--global mezcla observaciones entre proyectos: {globales}")

        # 19. La sesion es la quinta categoria: agrupa las observaciones de una
        #     tanda y trae el resumen que claude-mem ya escribia y nadie leia.
        sesdir = os.path.join(vault, "sessions")
        notas_ses = sorted(os.listdir(sesdir)) if os.path.isdir(sesdir) else []
        if notas_ses != ["ses-sesion-a.md", "ses-sesion-b.md"]:
            fallos.append(f"notas de sesion inesperadas: {notas_ses}")
        else:
            sa = open(os.path.join(sesdir, "ses-sesion-a.md"), errors="replace").read()
            for esperado in ("Arreglar el cobro", "el diario se elegia por tipo",
                             "[[obs-1]]", "[[obs-3]]", "prompts: 2", "## Prompt 2"):
                if esperado not in sa:
                    fallos.append(f"falta {esperado!r} en la nota de sesion")
            if "[[esto-no]]" in sa:
                fallos.append("prosa del resumen de sesion no escapada")
            sb = open(os.path.join(sesdir, "ses-sesion-b.md"), errors="replace").read()
            if "no guardo resumen" not in sb:
                fallos.append("una sesion sin resumen no se anota como tal")
            if "[[obs-2]]" not in sb:
                fallos.append("la sesion sin resumen no agrupa sus observaciones")
        # y la observacion enlaza de vuelta con su sesion: el salto en los dos sentidos
        if "[[ses-sesion-a]]" not in open(os.path.join(memory, "obs-1.md"),
                                          errors="replace").read():
            fallos.append("la observacion no enlaza con su sesion")
        if "Sesiones recientes" not in idx:
            fallos.append("el INDEX no lista las sesiones")
        # el lookup y el generador tienen que estar de acuerdo en el nombre de nota
        if L.slug_ses("sesion-a") != "ses-sesion-a":
            fallos.append("vault_lookup nombra la nota de sesion distinto que el generador")
        L.DB = db  # el bloque --global la dejo apuntando a otra base
        encontradas = L.buscar_sesiones("PRUEBA", vault, "diario")
        if not encontradas:
            fallos.append("vault_lookup no encuentra la sesion por su resumen")
        elif encontradas[0][0] != "sesion-a":
            fallos.append(f"vault_lookup devuelve la sesion equivocada: {encontradas}")

        # 20. La configuracion entra como documento; el lockfile, el secreto y el
        #     json de datos se quedan fuera. El contenido va en bloque, no crudo.
        nota_yaml = os.path.join(docs, G.slug("perfiles/developer.yaml") + ".md")
        if not os.path.exists(nota_yaml):
            fallos.append("un yaml del proyecto no genero nota de documento")
        else:
            y = open(nota_yaml, errors="replace").read()
            if "```yaml" not in y:
                fallos.append("el yaml no se encierra en bloque: Obsidian lo interpretaria")
            if 'doc_kind: "yaml"' not in y:
                fallos.append("la nota de configuracion no declara doc_kind")
            if "```no debe romper" in y:
                fallos.append("las comillas del contenido cierran el bloque antes de tiempo")
            if "[[1, 2], [3, 4]]" not in y or "[[:space:]]" not in y:
                fallos.append("el contenido de la config se altero al embeberlo")
        if not os.path.exists(os.path.join(docs, G.slug(".claude/settings.json") + ".md")):
            fallos.append("el json de la config de agentes no entro")
        for prohibido in ("package-lock.json", "secrets.yaml", "datos/tabla.json",
                          ".claude/settings.local.json"):
            if os.path.exists(os.path.join(docs, G.slug(prohibido) + ".md")):
                fallos.append(f"{prohibido} no deberia tener nota en el vault")
        if f"[[{G.slug('perfiles/developer.yaml')}]]" not in open(nota_doc,
                                                                 errors="replace").read():
            fallos.append("el documento no enlaza la configuracion que cita")

        # 17. Ningun wikilink apunta a una nota inexistente.
        notas = {f[:-3] for r, _, fs in os.walk(vault) for f in fs if f.endswith(".md")}
        for r, _, fs in os.walk(vault):
            for f in fs:
                if not f.endswith(".md"):
                    continue
                texto = open(os.path.join(r, f)).read()
                # fuera los bloques cercados: dentro, Obsidian no interpreta los
                # [[...]], y una config puede llevarlos ([[bin]] de un Cargo.toml)
                fuera = re.sub(r"```.*?```", "", texto, flags=re.S)
                for enlace in re.findall(r"\[\[([^\]]+)\]\]", fuera):
                    if enlace not in notas:
                        fallos.append(f"enlace roto {enlace!r} en {f}")

    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("OK — spokes, fusion, guarda, join, docs, config, commits, sesiones,"
          " multiproyecto, global, lookup, enlaces")
    return 0


if __name__ == "__main__":
    sys.exit(main())
