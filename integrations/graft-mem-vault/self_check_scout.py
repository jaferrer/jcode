#!/usr/bin/env python3
"""Autocomprobacion de vault_scout: passthrough, recoleccion, recorte y validacion.

Lo que mas importa verificar es que con el interruptor apagado la salida sea
identica a la de vault_lookup.py: esa es la garantia de que nada de lo que ya
funciona se rompe.

Uso: python3 self_check_scout.py
"""
import os
import subprocess
import sys
import tempfile

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
SCOUT = os.path.join(AQUI, "vault_scout.py")
LOOKUP = os.path.join(AQUI, "vault_lookup.py")


def lanzar(script, args, env):
    return subprocess.run([sys.executable, script, *args], capture_output=True,
                          text=True, env=env, cwd=env["PRUEBA_CWD"], timeout=60)


def vault_sintetico(tmp):
    """Un vault minimo con tres observaciones y una sesion, mas su DB de claude-mem.

    Se escriben las notas a mano en vez de generarlas con graft_mem_vault: asi el
    test fija el formato exacto que el recortador debe saber leer.
    """
    import json
    from self_check import crear_db

    vault = os.path.join(tmp, "vaults", "PRUEBA-graft-mem")
    for sub in ("memory", "sessions", "docs", "code"):
        os.makedirs(os.path.join(vault, sub))

    notas = {
        1: ("El diario de cobro se elegia por tipo con limit=1",
            "Un limit=1 sin orden devolvia un diario arbitrario.",
            ["- El diario se resolvia con limit=1 y sin ORDER BY",
             "- Afectaba solo a companias con dos diarios de cobro"]),
        2: ("La orden llama a cobrar y no cambia",
            "Se confirmo que la orden no participa en la eleccion de diario.",
            ["- orden.py:lanzar llama a linea.py:cobrar",
             "- Ningun cambio necesario en la orden"]),
        3: ("Nada que ver con este asunto",
            "Observacion de relleno para comprobar el filtrado.",
            ["- Texto sin relacion con diarios ni cobros"]),
    }
    for oid, (titulo, resumen, hechos) in notas.items():
        with open(os.path.join(vault, "memory", f"obs-{oid}.md"), "w") as fh:
            fh.write(f'---\nobs_id: {oid}\ndate: "2026-08-07T10:00:00.000Z"\n'
                     f'type: "discovery"\nfiles: 1\nsession: "sesion-a"\ntags: [memory]\n---\n'
                     f"# {titulo}\n\n*{resumen}*\n\n"
                     "Parrafo de prosa que el recortador debe tirar entero porque es "
                     "redundante con el resumen y los hechos.\n\n"
                     "## Hechos\n\n" + "\n".join(hechos) + "\n\n"
                     "## Ficheros\n\n- [[pagos_linea.py]]\n\n"
                     "## Sesion\n\n- [[ses-sesion-a]]\n\n"
                     "## Conceptos\n\n#gotcha\n")

    with open(os.path.join(vault, "sessions", "ses-sesion-a.md"), "w") as fh:
        fh.write('---\nsession: "sesion-a"\ndate: "2026-08-07"\nobservations: 2\n'
                 "files: 1\nprompts: 2\ntags: [session]\n---\n"
                 "# Sesion 2026-08-07 — Arreglar el cobro que elige mal el diario\n\n"
                 "## Prompt 1\n\n**Peticion**\n\nArreglar el cobro que elige mal el diario\n\n"
                 "**Investigado**\n\nse leyo pagos/linea.py\n\n"
                 "**Aprendido**\n\nel diario se elegia por tipo con limit=1\n\n"
                 "## Prompt 2\n\n**Peticion**\n\nConfirmar que no rompe la orden\n\n"
                 "**Aprendido**\n\nla orden llama a cobrar y no cambia\n")

    with open(os.path.join(vault, "docs", "docs_cobros.md"), "w") as fh:
        fh.write("---\ndoc_path: docs/cobros.md\n---\n# Cobros\n\nEl diario de cobro.\n")

    db = os.path.join(tmp, "claude-mem.db")
    crear_db(db, [(oid, t, r, ["pagos/linea.py"], [])
                  for oid, (t, r, _) in notas.items()])

    cfg = os.path.join(tmp, "projects.json")
    raiz = os.path.join(tmp, "repo")
    os.makedirs(raiz)
    with open(cfg, "w") as fh:
        json.dump({"vault_dir": os.path.join(tmp, "vaults"),
                   "projects": {"PRUEBA": {"hub": raiz, "roots": []}}}, fh)
    return vault, db, cfg, raiz


def main():
    fallos = []
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ)
        env["PRUEBA_CWD"] = tmp
        env["VAULT_SCOUT_ON"] = ""          # interruptor apagado
        env["VAULT_SCOUT_INTERRUPTOR"] = os.path.join(tmp, "no-existe")

        # 1. Con el interruptor apagado la salida es identica a vault_lookup.
        #    Se prueba contra un termino que no resuelve vault: ambos deben
        #    fallar igual, con el mismo texto y el mismo codigo de salida.
        a = lanzar(SCOUT, ["un-concepto-cualquiera"], env)
        b = lanzar(LOOKUP, ["un-concepto-cualquiera"], env)
        if a.stdout != b.stdout:
            fallos.append("interruptor apagado: stdout distinto de vault_lookup")
        if a.returncode != b.returncode:
            fallos.append(f"interruptor apagado: rc {a.returncode} != {b.returncode}")

        # 2. Sin argumentos y con el interruptor apagado, tambien delega:
        #    mismo texto de uso y mismo codigo de retorno que vault_lookup.
        a = lanzar(SCOUT, [], env)
        b = lanzar(LOOKUP, [], env)
        if a.stdout != b.stdout:
            fallos.append("sin argumentos: stdout distinto de vault_lookup")
        if a.returncode != b.returncode:
            fallos.append(f"sin argumentos: rc {a.returncode} != {b.returncode}")

        # 3. La expansion tira las palabras vacias y se queda con el contenido.
        os.environ["GRAFT_MEM_VAULT_CONFIG"] = os.path.join(tmp, "projects.json")
        os.environ["CLAUDE_MEM_DB"] = os.path.join(tmp, "claude-mem.db")
        vault, db, cfg, raiz = vault_sintetico(tmp)
        import vault_lookup as L
        import vault_scout as S
        L.CONFIG, L.DB = cfg, db

        # 1b. La garantia nº1, de verdad: con el interruptor APAGADO y un vault
        #     que SI resuelve, la salida real debe ser identica a la de
        #     vault_lookup. Los chequeos 1 y 2 corren donde el vault no resuelve
        #     y solo comparan dos mensajes de error, asi que no protegen nada:
        #     borrar el gobierno del interruptor en main() no los hacia fallar.
        envoff = dict(env)
        envoff["PRUEBA_CWD"] = raiz
        envoff["GRAFT_MEM_VAULT_CONFIG"] = cfg
        envoff["CLAUDE_MEM_DB"] = db
        envoff["VAULT_SCOUT_ON"] = ""
        envoff["VAULT_SCOUT_INTERRUPTOR"] = os.path.join(tmp, "no-existe")
        a = lanzar(SCOUT, ["diario de cobro"], envoff)
        b = lanzar(LOOKUP, ["diario de cobro"], envoff)
        if a.stdout != b.stdout:
            fallos.append("interruptor apagado con vault resoluble: stdout distinto")
        if a.stderr != b.stderr:
            fallos.append("interruptor apagado con vault resoluble: stderr distinto")
        if a.returncode != b.returncode:
            fallos.append(f"interruptor apagado con vault resoluble: rc {a.returncode} != {b.returncode}")
        if not a.stdout.strip():
            fallos.append("el caso de paridad no produce salida real: no prueba nada")

        terminos = S.expandir("¿que se hizo para arreglar el diario de cobro?")
        if "diario" not in terminos or "cobro" not in terminos:
            fallos.append(f"expandir pierde contenido: {terminos}")
        if any(t in terminos for t in ("para", "hizo", "arreglar")):
            fallos.append(f"expandir deja pasar palabras vacias: {terminos}")

        # 3b. Las palabras acentuadas sobreviven enteras. Un patron que las
        #     trocea produce fragmentos que no casan con nada en FTS5.
        #     El literal DEBE llevar tildes y enia: son el dato bajo prueba.
        acentos = S.expandir("¿que paso con la sesión de la compañía?")
        if "sesión" not in acentos or "compañía" not in acentos:
            fallos.append(f"expandir mutila las palabras acentuadas: {acentos}")

        # 4. La consulta FTS5 entrecomilla cada termino y los une con OR.
        q = S.consulta_fts(["diario", "cobro"])
        if q != '"diario" OR "cobro"':
            fallos.append(f"consulta_fts mal formada: {q!r}")

        # 5. La recoleccion encuentra las dos observaciones que hablan del tema
        #    y no la de relleno, y encuentra la sesion.
        cand = S.recolectar("PRUEBA", vault, ["diario", "cobro"])
        ids = {o[0] for o in cand["obs"]}
        if 1 not in ids:
            fallos.append(f"recolectar no encuentra la obs 1: {ids}")
        if 3 in ids:
            fallos.append("recolectar trae la observacion de relleno")
        if not cand["ses"]:
            fallos.append("recolectar no encuentra ninguna sesion")

        # 6. El recorte se queda con titulo, resumen y Hechos, y tira la prosa.
        nota = open(os.path.join(vault, "memory", "obs-1.md")).read()
        corto = S.recortar_obs(nota)
        if "[obs 1]" not in corto:
            fallos.append("recortar_obs no ancla la referencia")
        if "limit=1 y sin ORDER BY" not in corto:
            fallos.append("recortar_obs pierde los Hechos")
        if "Parrafo de prosa" in corto:
            fallos.append("recortar_obs no tira la prosa")
        if "#gotcha" in corto or "[[pagos_linea.py]]" in corto:
            fallos.append("recortar_obs arrastra secciones que no aportan")
        if len(corto) >= len(nota):
            fallos.append("recortar_obs no comprime")

        # 7. De una sesion solo interesa que se pidio: es la capa de intencion.
        notas = open(os.path.join(vault, "sessions", "ses-sesion-a.md")).read()
        cortas = S.recortar_ses(notas)
        if "Arreglar el cobro que elige mal el diario" not in cortas:
            fallos.append("recortar_ses pierde la peticion")
        if "Confirmar que no rompe la orden" not in cortas:
            fallos.append("recortar_ses solo coge el primer prompt")
        if "se leyo pagos/linea.py" in cortas:
            fallos.append("recortar_ses arrastra lo investigado")

        # 8. El extracto junta las capas y declara que referencias son citables.
        texto, refs = S.extracto(vault, cand)
        if "obs 1" not in refs:
            fallos.append(f"extracto no declara la obs 1 como citable: {refs}")
        if "[obs 1]" not in texto:
            fallos.append("extracto no incluye la observacion principal")
        if len(texto) > S.PRESUPUESTO_CORPUS:
            fallos.append("extracto se pasa del presupuesto")

        # 9. --dry-run imprime el extracto sin llamar a ningun modelo.
        env2 = dict(env)
        env2["VAULT_SCOUT_ON"] = "1"
        env2["PRUEBA_CWD"] = raiz
        env2["GRAFT_MEM_VAULT_CONFIG"] = cfg
        env2["CLAUDE_MEM_DB"] = db
        env2["VAULT_SCOUT_MODEL"] = "none"
        r = lanzar(SCOUT, ["--dry-run", "arreglar el diario de cobro"], env2)
        if "[obs 1]" not in r.stdout:
            fallos.append(f"--dry-run no imprime el extracto: {r.stdout[:200]} {r.stderr[:200]}")
        if r.returncode != 0:
            fallos.append(f"--dry-run rc={r.returncode}")

        # 10. Un handoff SIN COMMITEAR es invisible para el vault (git ls-files),
        #     asi que el barrido de thoughts/ es lo unico que puede encontrarlo.
        hoja = os.path.join(raiz, "thoughts", "shared", "handoffs", "cobros")
        os.makedirs(hoja)
        with open(os.path.join(hoja, "2026-08-08_sin-commitear.yaml"), "w") as fh:
            fh.write("goal: arreglado el diario de cobro que se elegia con limit=1\n"
                     "irrelevante: una linea que no menciona el tema\n")
        sueltos = S.barrer_thoughts(raiz, ["diario", "cobro"])
        rutas = [r for r, _, _ in sueltos]
        if not any("sin-commitear" in r for r in rutas):
            fallos.append(f"barrer_thoughts no ve el handoff sin commitear: {rutas}")
        cuerpo = "".join(c for _, c, _ in sueltos)
        if "limit=1" not in cuerpo:
            fallos.append("barrer_thoughts no trae la linea que coincide")
        if "una linea que no menciona el tema" in cuerpo:
            fallos.append("barrer_thoughts trae lineas que no coinciden")

        # 11. Un fichero que solo casa con UN termino no entra: seria ruido.
        with open(os.path.join(hoja, "ruido.yaml"), "w") as fh:
            fh.write("nota: aqui solo aparece la palabra diario y nada mas\n")
        rutas = [r for r, _, _ in S.barrer_thoughts(raiz, ["diario", "cobro"])]
        if any("ruido" in r for r in rutas):
            fallos.append("barrer_thoughts acepta ficheros con un solo termino")

        # 12. Lo suelto llega al extracto y es citable por su ruta.
        texto, refs = S.extracto(vault, cand, sueltos)
        if not any(r.startswith("thoughts/") for r in refs):
            fallos.append(f"el extracto no declara citable lo suelto: {refs}")

        # 12b. Un fichero commiteado NO puede etiquetarse como sin commitear:
        #      esa etiqueta acaba alimentando al modelo como si fuera un hecho.
        texto12b, _ = S.extracto(vault, cand, [("thoughts/ledgers/x.md", "linea", True)])
        if "sin commitear" in texto12b:
            fallos.append("etiqueta un fichero rastreado como sin commitear")
        texto12c, _ = S.extracto(vault, cand, [("thoughts/ledgers/y.md", "linea", False)])
        if "sin commitear" not in texto12c:
            fallos.append("no marca como sin commitear un fichero que no lo esta")

        # 13. Un veredicto que cita una referencia que no viajo en el corpus
        #     es una invencion y debe detectarse.
        refs_ok = {"obs 1", "ses-sesion-a"}
        malas = S.refs_no_respaldadas(
            "VEREDICTO · confianza: ALTA\nel diario usaba limit=1 [obs 1]\n", refs_ok)
        if malas:
            fallos.append(f"refs_no_respaldadas rechaza una cita legitima: {malas}")
        malas = S.refs_no_respaldadas(
            "VEREDICTO · confianza: ALTA\nalgo que nadie dijo [obs 999]\n", refs_ok)
        if malas != ["obs 999"]:
            fallos.append(f"refs_no_respaldadas no detecta la invencion: {malas}")

        # 13b. El razonamiento previo del modelo no contamina la validacion:
        #      sus corchetes no son citas.
        ruido = ("Thinking...\nCada linea cita [reference] copiada de REFERENCIAS.\n"
                 "Formato: [ALTA|MEDIA|NADA-ENCONTRADO]\n\n"
                 "VEREDICTO · confianza: ALTA\nel diario usaba limit=1 [obs 1]\n")
        bloque = S.bloque_veredicto(ruido)
        if bloque.startswith("Thinking") or "[reference]" in bloque:
            fallos.append(f"bloque_veredicto no descarta el razonamiento: {bloque[:60]!r}")
        if S.refs_no_respaldadas(bloque, {"obs 1"}):
            fallos.append("el razonamiento previo hace fallar la validacion de referencias")

        # 13c. Los escapes de terminal que emite ollama no parten las rutas.
        sucio = "VEREDICTO · confianza: ALTA\nalgo [obs \x1b[3D\x1b[K1]\n"
        if S.refs_no_respaldadas(S.bloque_veredicto(sucio), {"obs 1"}):
            fallos.append("los escapes ANSI rompen la validacion de referencias")

        # 13d. Si el modelo nombra VEREDICTO durante su razonamiento, el bloque
        #      arranca en la ULTIMA aparicion, no en la primera.
        doble = ("Pensando...\nVEREDICTO se cita asi segun las reglas [x]\n"
                 "Ahora la respuesta:\n\nVEREDICTO · confianza: ALTA\nhecho real [obs 1]\n")
        b = S.bloque_veredicto(doble)
        if "[x]" in b or "Pensando" in b:
            fallos.append(f"bloque_veredicto ancla en la primera aparicion: {b[:60]!r}")
        if S.refs_no_respaldadas(b, {"obs 1"}):
            fallos.append("el preambulo con VEREDICTO contamina la validacion")

        # 13e. Una linea sin cita es invencion sin ancla: no puede pasar.
        if not S.lineas_sin_ancla("VEREDICTO · confianza: ALTA\nel sistema borra la base de datos\n"):
            fallos.append("una linea sin referencia pasa la validacion")
        if S.lineas_sin_ancla("VEREDICTO · confianza: ALTA\nhecho [obs 1]\n\nSIN ABRIR: obs 2\n"):
            fallos.append("la validacion rechaza un veredicto bien formado")
        if not S.lineas_sin_ancla("VEREDICTO · confianza: ALTA\ndos fuentes [obs 1] [obs 2]\n"):
            fallos.append("una linea con dos referencias pasa la validacion")

        # 14. Con VAULT_SCOUT_MODEL=none no se llama a ningun motor.
        os.environ["VAULT_SCOUT_MODEL"] = "none"
        import importlib
        importlib.reload(S)
        if S.sintetizar("lo que sea") is not None:
            fallos.append("con modelo none se sigue llamando al motor")

        # 15. El prompt lleva las referencias citables y prohibe cruzar fuentes.
        p = S.prompt_extractivo("y el diario?", "[obs 1] algo", {"obs 1"})
        if "obs 1" not in p:
            fallos.append("el prompt no declara las referencias citables")
        if "combine dos fuentes" not in p:
            fallos.append("el prompt no prohibe cruzar fuentes")

        # 16. --check informa sin necesitar vault ni motor.
        r = lanzar(SCOUT, ["--check"], env2)
        if r.returncode != 0 or "interruptor" not in r.stdout:
            fallos.append(f"--check no informa: rc={r.returncode} {r.stdout[:120]}")

        # 17. --quitar-scout borra el script y el bloque de la regla, y es
        #     idempotente. Sale antes de comprobar dependencias, asi que el
        #     test es hermetico.
        cdir = os.path.join(tmp, "claude")
        os.makedirs(os.path.join(cdir, "scripts"))
        os.makedirs(os.path.join(cdir, "rules"))
        with open(os.path.join(cdir, "scripts", "vault_scout.py"), "w") as fh:
            fh.write("# postizo\n")
        regla = os.path.join(cdir, "rules", "vault-lookup.md")
        with open(regla, "w") as fh:
            fh.write("# Regla\n\ncontenido previo\n\n<!-- vault-scout -->\n"
                     "bloque del scout\n<!-- /vault-scout -->\n")
        envi = dict(os.environ)
        envi["CLAUDE_CONFIG_DIR"] = cdir
        for intento in (1, 2):
            r = subprocess.run(["bash", os.path.join(AQUI, "install.sh"),
                                "--quitar-scout"], capture_output=True, text=True,
                               env=envi, timeout=60)
            if r.returncode != 0:
                fallos.append(f"--quitar-scout intento {intento} rc={r.returncode}"
                              f" {r.stderr[:160]}")
        if os.path.exists(os.path.join(cdir, "scripts", "vault_scout.py")):
            fallos.append("--quitar-scout no borra el script")
        texto_regla = open(regla).read()
        if "vault-scout" in texto_regla:
            fallos.append("--quitar-scout no revierte el bloque de la regla")
        if "contenido previo" not in texto_regla:
            fallos.append("--quitar-scout se lleva por delante el resto de la regla")
        if os.path.exists(regla + ".bak"):
            fallos.append("--quitar-scout deja un .bak")

        # 17b. Con una marca de apertura huerfana, el descableado NO toca la
        #      regla: un sed sin cierre borraria hasta el final del fichero.
        with open(regla, "w") as fh:
            fh.write("# Regla\n\ncontenido previo\n\n<!-- vault-scout -->\n"
                     "bloque a medio escribir\n\n## Seccion del usuario\n"
                     "texto que no se puede perder\n")
        r = subprocess.run(["bash", os.path.join(AQUI, "install.sh"), "--quitar-scout"],
                           capture_output=True, text=True, env=envi, timeout=60)
        texto_regla = open(regla).read()
        if "texto que no se puede perder" not in texto_regla:
            fallos.append("--quitar-scout borra contenido ajeno con marcas desparejadas")
        if "marcas de apertura" not in r.stderr:
            fallos.append("--quitar-scout no avisa de las marcas desparejadas")

        # 18. Residuo de --quitar-scout: no debe sobrevivir ni el interruptor
        #     encendido ni la cache de bytecode de vault_lookup, o una
        #     reinstalacion posterior arrancaria encendida pese al apagado
        #     por defecto.
        interruptor = os.path.join(cdir, ".vault-scout-on")
        open(interruptor, "w").close()
        cache_dir = os.path.join(cdir, "scripts", "__pycache__")
        os.makedirs(cache_dir, exist_ok=True)
        cache_pyc = os.path.join(cache_dir, "vault_lookup.cpython-99.pyc")
        open(cache_pyc, "w").close()
        r = subprocess.run(["bash", os.path.join(AQUI, "install.sh"), "--quitar-scout"],
                           capture_output=True, text=True, env=envi, timeout=60)
        if os.path.exists(interruptor):
            fallos.append("--quitar-scout deja el interruptor encendido")
        if os.path.exists(cache_pyc):
            fallos.append("--quitar-scout deja la cache __pycache__ de vault_lookup")

    if fallos:
        print("FALLOS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("OK — passthrough, recoleccion, recorte, thoughts, motor, instalacion")
    return 0


if __name__ == "__main__":
    sys.exit(main())
