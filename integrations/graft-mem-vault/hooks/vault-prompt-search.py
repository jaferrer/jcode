#!/usr/bin/env python3
"""UserPromptSubmit: busca la pregunta en el vault antes de que el modelo elija herramienta.

Es la pieza que hace la consulta de verdad automatica. El aviso de lectura solo
salta al abrir un fichero concreto, pero el primer movimiento ante una pregunta
rara vez es leer: es buscar, o razonar. Y una pregunta conceptual ("por que
falla la conciliacion") no toca ningun fichero, asi que sin esto el historico no
aparece nunca salvo que alguien se acuerde de pedirlo.

Cuesta unos milisegundos porque va contra el indice FTS5 que claude-mem ya
mantiene, y solo inyecta si hay aciertos de sobra: un resultado suelto es mas
ruido que ayuda.

Si el repo actual no tiene nada pero SI lo tienen otros proyectos con vault, se
avisa de eso en vez de callar del todo: es la señal de "esto puede estar fuera
del scope de este repo, prueba --global" sin volcar resultados ajenos.

Fail-open por construccion: cualquier error emite {} y el prompt sigue.
"""
import json
import os
import re
import sys

CONFIG_ROOT = os.path.expanduser(
    os.environ.get("GRAFT_MEM_VAULT_HOME")
    or os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
SCRIPTS = os.path.join(CONFIG_ROOT, "scripts")
LOOKUP = os.path.join(SCRIPTS, "vault_lookup.py")
MIN_TERMINOS = 1     # por debajo, la pregunta no tiene con que buscar
MIN_ACIERTOS = 2     # un acierto suelto suele ser casualidad
MIN_ACIERTOS_GLOBAL = 2  # igual de exigente fuera del repo: menos ruido
TOPE = 5
TOPE_SESIONES = 2    # la sesion ocupa dos lineas: con dos ya se ve el patron
TOPE_GLOBAL = 2      # por proyecto ajeno, para no ahogar el aviso con volumen
MIN_LARGO = 12       # "si", "vale", "sigue" no son consultas

# palabras que no discriminan nada; en ambos idiomas porque las sesiones mezclan
VACIAS = set("""
a al algo ahora antes aqui asi aunque bien cada como con contra cual cuando de del desde
donde dos el ella ellos en entre era eres es esa ese eso esta este esto estoy fue ha hace
hacer hacia hasta hay la las le lo los mas me mi mucho muy no nos o para pero poco por
porque que quien se ser si sin sobre solo son su sus tambien te tiene todo tu un una uno
ver y ya
ademas ahi aqui asi cual cuando cuanto donde esta estan este esto hacen hizo hago
mismo pasa paso puede queda quiero sale sea segun ser sido tal tras vale varias
varios
about after all also and any are as at be been but by can could do does for from get has
have how in into is it its just like make may more most no not of on one only or other
our out over should so some than that the their then there these they this those to up
use very was we were what when where which who why will with would you your
archivo fichero ficheros codigo file files code why donde esta cual como que
""".split())


def emitir(obj=None):
    sys.stdout.write(json.dumps(obj) if obj else "{}")
    sys.exit(0)


def terminos(prompt):
    """Palabras del prompt que sirven para buscar.

    El patron es unicode a proposito: con [A-Za-z_] las tildes parten la
    palabra por la mitad y en un repo en español eso destroza la consulta
    ("migracion" -> "migraci", "diseñada" -> "dise"), que era justo lo que
    hacia fallar la busqueda en las preguntas mas naturales.
    """
    brutas = re.findall(r"[^\W\d_][\w.]{2,}", prompt, re.UNICODE)
    out, vistas = [], set()
    for t in brutas:
        bajo = t.lower()
        if bajo in VACIAS or bajo in vistas or len(bajo) < 4:
            continue
        vistas.add(bajo)
        out.append(t)
    # los identificadores (con _ o .) discriminan mucho mas que una palabra suelta
    out.sort(key=lambda t: (("_" not in t and "." not in t), -len(t)))
    return out[:6]


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        emitir()
    prompt = (payload.get("prompt") or "").strip()
    if len(prompt) < MIN_LARGO:
        emitir()
    palabras = terminos(prompt)
    if len(palabras) < MIN_TERMINOS:
        emitir()

    sys.path.insert(0, SCRIPTS)
    try:
        import vault_lookup as L
        proyecto, vault = L.resolver_vault()
    except SystemExit:
        emitir()  # el repo no tiene vault
    except Exception:
        emitir()

    consulta = " OR ".join(f'"{p}"' for p in palabras)
    try:
        obs = L.buscar_observaciones(proyecto, vault, consulta, TOPE, crudo=True)
    except Exception:
        emitir()
    # la sesion contesta a otra pregunta: no "que fichero se toco" sino "esto ya se
    # intento, y esto es lo que se aprendio". Un acierto suelto aqui ya vale
    try:
        ses = L.buscar_sesiones(proyecto, vault, consulta, TOPE_SESIONES, crudo=True)
    except Exception:
        ses = []
    if len(obs) < MIN_ACIERTOS and not ses:
        # nada en este repo: antes de callar, se prueba si el resto de vaults
        # tiene algo, para avisar de que la pregunta puede estar fuera de scope
        try:
            cfg = L.cargar_cfg()
            propios = set(L.nombres_de_proyecto(proyecto))
            fuera = [(p, oid, t, f) for p, oid, t, f in
                     L.buscar_global_observaciones(cfg, consulta, TOPE_GLOBAL, crudo=True)
                     if p not in propios]
        except Exception:
            fuera = []
        if len(fuera) < MIN_ACIERTOS_GLOBAL:
            emitir()
        otros = ", ".join(sorted({p for p, *_ in fuera}))
        mensaje_fuera = (
            f"[vault] Nada en {proyecto} sobre esto, pero SI "
            f"hay historico en otros proyectos con vault "
            f"({otros}). Si la pregunta no es de este repo:\n"
            f"  python3 {LOOKUP} "
            "--global <termino>")
        emitir({"systemMessage": mensaje_fuera,
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                        "additionalContext": mensaje_fuera}})

    lineas = [
        f"[vault] El historico de {proyecto} tiene material sobre esto. "
        "Miralo antes de investigar desde cero:",
    ]
    lineas += [f"  {oid}  {fecha}  {titulo[:92]}" for oid, titulo, fecha in obs]
    if ses:
        lineas.append("  Sesiones que ya trabajaron esto:")
        for sid, titulo, fecha in ses:
            lineas.append(f"    {fecha}  {titulo[:76]}")
            lineas.append(f"      abrir: vault_lookup.py --ses {sid}")
    lineas.append(
        f"  Abrir: python3 {LOOKUP} --obs <id>"
        "   ·   Mas: vault_lookup.py <termino>")
    mensaje = "\n".join(lineas)
    emitir({"systemMessage": mensaje,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                    "additionalContext": mensaje}})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        emitir()
