#!/usr/bin/env python3
"""Consulta al vault que devuelve hechos anclados en vez de material crudo.

Recolecta candidatos con una sola consulta FTS5, los recorta de forma
determinista y, si hay motor disponible, deja que un modelo barato filtre y
ancle cada hecho a su fuente. Cada etapa degrada a la anterior.

Con el interruptor APAGADO (por defecto) es un passthrough a vault_lookup.py:
misma salida, mismo codigo de retorno. Encender: touch ~/.claude/.vault-scout-on

Uso:
  vault_scout.py "<pregunta>"          consulta
  vault_scout.py --dry-run "<preg>"    recoleccion y recorte, sin modelo
  vault_scout.py --check               estado del interruptor y del motor
"""
import os
import re
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
LOOKUP = os.path.join(AQUI, "vault_lookup.py")
INTERRUPTOR = os.path.expanduser(os.environ.get(
    "VAULT_SCOUT_INTERRUPTOR", "~/.claude/.vault-scout-on"))

RE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

TOPE_TERMINOS = 6   # mas terminos traen ruido, no cobertura
TOPE_OBS = 8
TOPE_SES = 3
TOPE_DOCS = 3

# Vacias de cuatro letras o mas: las cortas ya las descarta el propio tokenizador.
# Incluye los verbos de pedir (busca, dime, quiero): son la peticion, no el tema.
STOPWORDS = frozenset("""
ahora algo algun alguna algunas algunos antes aqui aunque arreglar busca buscar cada como
cual cuales cuando cuanto desde dime donde entre eran eras esa esas esos esta estas
este esto estos favor hace hacer hacia hasta hecho hizo mientras misma mismo
mucho muchos necesito nunca para pero podria porque puede pueden quien quiero
saber segun siempre sino sobre solo tambien tengo tiene tienen toda todas todo
todos
""".split())

RE_PALABRA = re.compile(r"[0-9a-záéíóúüñ]{4,}", re.I | re.U)


def expandir(pregunta):
    """Terminos candidatos de una pregunta en lenguaje natural.

    Ordena por longitud descendente porque la palabra mas larga suele ser la mas
    especifica, y solo las TOPE_TERMINOS primeras entran en la consulta.
    """
    vistos, out = set(), []
    for t in RE_PALABRA.findall(pregunta.lower()):
        if t in STOPWORDS or t in vistos:
            continue
        vistos.add(t)
        out.append(t)
    out.sort(key=len, reverse=True)
    return out[:TOPE_TERMINOS]


def consulta_fts(terminos):
    """Una sola consulta con OR: bm25 ya puntua mas alto lo que casa con mas terminos.

    Eso ahorra lanzar una busqueda por termino y fusionarlas a mano, que era el
    plan original: el ranking del propio FTS5 hace ese trabajo mejor.
    """
    return " OR ".join('"%s"' % t.replace('"', "") for t in terminos)


def recolectar(proyecto, vault, terminos):
    """Candidatos de las tres capas del vault: observaciones, sesiones y documentos."""
    sys.path.insert(0, AQUI)
    import vault_lookup as L
    q = consulta_fts(terminos)
    obs = L.buscar_observaciones(proyecto, vault, q, TOPE_OBS, crudo=True)
    ses = L.buscar_sesiones(proyecto, vault, q, TOPE_SES, crudo=True)
    # los documentos no estan en FTS5: se buscan por termino, con los mas
    # especificos primero, hasta llenar el cupo
    docs, vistos = [], set()
    for t in terminos[:3]:
        for d in L.buscar_en_documentos(vault, t, TOPE_DOCS):
            if d not in vistos:
                vistos.add(d)
                docs.append(d)
    return {"obs": obs, "ses": ses, "docs": docs[:TOPE_DOCS]}


PRESUPUESTO_CORPUS = 40000   # caracteres; ~10k tokens de techo antes de recortar

# Los resumenes de sesion van por prompt, con **Peticion** / **Investigado** /
# **Aprendido**. Solo la peticion responde a «esto ya lo intentamos».
RE_PETICION = re.compile(r"\*\*Peticion\*\*\n\n(.*?)(?=\n\*\*|\n## |\Z)", re.S)
RE_TITULO = re.compile(r"^# (.+)$", re.M)
RE_RESUMEN = re.compile(r"^\*(.+)\*$", re.M)


def recortar_obs(texto):
    """Titulo, resumen y Hechos. La prosa es redundante con esos dos y es el bulto."""
    sys.path.insert(0, AQUI)
    import vault_lookup as L
    oid = L.frontmatter(texto, "obs_id").strip('"')
    fecha = L.frontmatter(texto, "date").strip('"')[:10]
    tipo = L.frontmatter(texto, "type").strip('"')
    m = RE_TITULO.search(texto)
    titulo = m.group(1).strip() if m else ""
    m = RE_RESUMEN.search(texto)
    out = [f"[obs {oid}] {fecha} ({tipo}) {titulo}"]
    if m:
        out.append("  resumen: " + m.group(1).strip())
    for linea in L.seccion(texto, "Hechos").splitlines():
        linea = linea.strip()
        if linea.startswith("- "):
            out.append("  " + linea)
    return "\n".join(out)


def recortar_ses(texto):
    """De una sesion solo la peticion de cada prompt: la capa de intencion."""
    sys.path.insert(0, AQUI)
    import vault_lookup as L
    sid = L.frontmatter(texto, "session").strip('"')
    fecha = L.frontmatter(texto, "date").strip('"')[:10]
    m = RE_TITULO.search(texto)
    out = [f"[ses-{sid}] {fecha} {m.group(1).strip() if m else ''}"]
    for p in RE_PETICION.findall(texto):
        p = " ".join(p.split())
        if p:
            out.append("  pidio: " + p)
    return "\n".join(out)


DIRS_THOUGHTS = ("thoughts/shared/handoffs", "thoughts/ledgers")
TOPE_LINEAS_SUELTAS = 12
TOPE_SUELTOS = 3


def _rastreados(raiz):
    """Rutas de thoughts/ que git ya conoce. Una sola llamada, no una por fichero.

    Sin esto la etiqueta afirmaria 'sin commitear' sobre documentos que si lo
    estan, y ese texto acaba alimentando al modelo que debe anclar cada hecho a
    su fuente.
    """
    try:
        r = subprocess.run(["git", "-C", raiz, "ls-files", "--", *DIRS_THOUGHTS],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None          # sin git no se puede afirmar nada
    if r.returncode != 0:
        return None
    return {l.strip() for l in r.stdout.splitlines() if l.strip()}


def barrer_thoughts(raiz, terminos):
    """Handoffs y ledgers que el vault no puede ver porque no estan commiteados.

    graft_mem_vault indexa markdown con `git ls-files '*.md'` (linea 325), asi que
    un documento recien escrito es invisible. Se exigen dos terminos distintos para
    entrar: con uno solo, cualquier fichero largo coincidiria.
    """
    cuenta = {}
    for sub in DIRS_THOUGHTS:
        d = os.path.join(raiz, sub)
        if not os.path.isdir(d):
            continue
        for actual, _dirs, ficheros in os.walk(d):
            for f in ficheros:
                ruta = os.path.join(actual, f)
                try:
                    with open(ruta, errors="replace") as fh:
                        texto = fh.read().lower()
                except OSError:
                    continue
                casan = {t for t in terminos if t in texto}
                if len(casan) >= 2:
                    cuenta[ruta] = len(casan)

    rastreados = _rastreados(raiz)
    out = []
    for ruta in sorted(cuenta, key=lambda r: (-cuenta[r], r))[:TOPE_SUELTOS]:
        lineas = []
        with open(ruta, errors="replace") as fh:
            for linea in fh:
                if any(t in linea.lower() for t in terminos):
                    lineas.append(linea.rstrip())
                if len(lineas) >= TOPE_LINEAS_SUELTAS:
                    break
        relativa = os.path.relpath(ruta, raiz)
        rastreado = None if rastreados is None else relativa in rastreados
        out.append((relativa, "\n".join(lineas), rastreado))
    return out


def _entero(nombre, defecto):
    """Un valor invalido en el entorno no puede tumbar el passthrough.

    Se evalua al importar, antes de que main() mire el interruptor: reventar
    aqui rompe la garantia de que con el scout apagado todo sigue igual.
    """
    try:
        return int(os.environ.get(nombre, "") or defecto)
    except ValueError:
        return defecto


MODELO = os.environ.get("VAULT_SCOUT_MODEL", "qwen3.5:4b-nvfp4")
TIMEOUT_MOTOR = _entero("VAULT_SCOUT_TIMEOUT", 60)
FCC_URL = os.environ.get("FCC_URL", "http://127.0.0.1:8082")
FCC_TOKEN = os.environ.get("FCC_AUTH_TOKEN", "freecc")

RE_REF = re.compile(r"\[([^\]\n]{1,200})\]")

# El modelo SELECCIONA Y COPIA; no redacta. Un modelo pequeno redactando funde
# dos hechos ciertos de fuentes distintas en una frase falsa, y eso ninguna
# comprobacion posterior lo detecta. Una fuente por linea lo impide de raiz.
PLANTILLA = """Eres un extractor. Respondes SOLO con lo que este en el EXTRACTO.

PREGUNTA: {pregunta}

REGLAS
- Cada linea cita UNA sola fuente, entre corchetes, copiada literalmente de
  REFERENCIAS. No inventes referencias ni las abrevies.
- Prohibido escribir una frase que combine dos fuentes.
- Prohibido deducir causas, fechas o relaciones que no esten escritas.
- Descarta lo que no venga a cuento. No lo menciones.
- Si el extracto no responde, escribe solo:
  VEREDICTO · confianza: NADA-ENCONTRADO

FORMATO
VEREDICTO · confianza: ALTA|MEDIA|NADA-ENCONTRADO
<una linea por hecho, cada una terminada en [referencia]>

REFERENCIAS (las unicas citables, copia literal):
{refs}

EXTRACTO:
{texto}
"""


def prompt_extractivo(pregunta, texto, refs):
    return PLANTILLA.format(pregunta=pregunta, texto=texto,
                            refs="\n".join(f"[{r}]" for r in sorted(refs)))


RE_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _limpiar(salida):
    """ollama run emite escapes de terminal aunque se redirija la salida.

    Sin quitarlos, las rutas se parten por la mitad y la validacion las toma
    por referencias fabricadas.
    """
    return RE_ANSI.sub("", salida or "")


def motor_ollama(prompt):
    """Completacion directa. NO se usa local-task.sh: sus tres tiers arrancan una
    sesion entera de Claude Code (local-task.sh:185-250) y el prefill cuesta 60-70 s.
    """
    import shutil
    if not shutil.which("ollama"):
        return None
    try:
        r = subprocess.run(["ollama", "run", MODELO], input=prompt,
                           capture_output=True, text=True, timeout=TIMEOUT_MOTOR)
    except (OSError, subprocess.SubprocessError):
        return None
    salida = _limpiar(r.stdout).strip()
    return salida if r.returncode == 0 and salida else None


def motor_fcc(prompt):
    """fcc-server habla la API de Anthropic. urllib basta: no hace falta curl."""
    import json as _json
    import urllib.request
    try:
        urllib.request.urlopen(FCC_URL + "/v1/models", timeout=2).read()
    except Exception:
        return None
    cuerpo = _json.dumps({"model": "default", "max_tokens": 700,
                          "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(FCC_URL + "/v1/messages", data=cuerpo, headers={
        "content-type": "application/json", "x-api-key": FCC_TOKEN,
        "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_MOTOR) as r:
            datos = _json.loads(r.read())
    except Exception:
        return None
    t = "".join(b.get("text", "") for b in datos.get("content", [])
                if isinstance(b, dict))
    t = _limpiar(t).strip()
    return t or None


def sintetizar(prompt):
    """Motor mas barato disponible. Sin ninguno, None: el extracto ya vale solo."""
    if MODELO == "none":
        return None
    for motor in (motor_ollama, motor_fcc):
        salida = motor(prompt)
        if salida:
            return salida
    return None


def refs_no_respaldadas(veredicto, refs):
    """Referencias citadas que no viajaron en el corpus: eso es una invencion.

    Coge ids fabricados. NO coge la deriva de atribucion — fundir dos hechos
    ciertos de fuentes distintas — contra la que solo protege el formato.
    """
    return [r.strip() for r in RE_REF.findall(veredicto) if r.strip() not in refs]


def lineas_sin_ancla(veredicto):
    """Lineas de contenido que no llevan exactamente una referencia.

    El diseno se apoya en «una fuente por linea» para que el modelo no pueda
    fundir dos hechos ciertos en una frase falsa. Si nadie lo comprueba, esa
    garantia es solo una peticion en el prompt: una linea sin cita es
    invencion libre y sin ancla.
    """
    malas = []
    for linea in veredicto.splitlines():
        t = linea.strip()
        if not t or t.startswith("VEREDICTO") or t.startswith("SIN ABRIR"):
            continue
        if len(RE_REF.findall(t)) != 1:
            malas.append(t)
    return malas


RE_VEREDICTO = re.compile(r"^VEREDICTO\b", re.M)


def bloque_veredicto(salida):
    """El veredicto es lo que va desde la ULTIMA linea VEREDICTO en adelante.

    Un modelo pequeno narra su razonamiento antes de responder y puede nombrar
    la palabra por el camino. Anclar en la primera ocurrencia arrastraria ese
    preambulo al veredicto que se imprime.
    """
    limpia = _limpiar(salida)
    ultima = None
    for m in RE_VEREDICTO.finditer(limpia):
        ultima = m
    return limpia[ultima.start():].strip() if ultima else ""


def check():
    import shutil
    print(f"interruptor: {'ENCENDIDO' if encendido() else 'apagado'}  ({INTERRUPTOR})")
    print(f"modelo:      {MODELO}")
    print(f"ollama:      {'si' if shutil.which('ollama') else 'no'}")
    print(f"fcc:         {FCC_URL}")
    sys.path.insert(0, AQUI)
    import vault_lookup as L
    try:
        proyecto, vault = L.resolver_vault()
        print(f"vault:       {proyecto} — {vault}")
    except SystemExit as e:
        print(f"vault:       sin resolver ({e})")
    return 0


def extracto(vault, cand, sueltos=()):
    """Corpus recortado y el conjunto de referencias que se pueden citar.

    Devolver las referencias es lo que permite despues comprobar que el veredicto
    no cita nada que no viajara en el corpus.
    """
    partes, refs, gastado = [], set(), 0

    def cabe(t):
        return gastado + len(t) <= PRESUPUESTO_CORPUS

    for oid, _titulo, _fecha in cand["obs"]:
        ruta = os.path.join(vault, "memory", f"obs-{oid}.md")
        if not os.path.exists(ruta):
            continue
        t = recortar_obs(open(ruta, errors="replace").read())
        if not cabe(t):
            break
        partes.append(t)
        gastado += len(t)
        refs.add(f"obs {oid}")

    for sid, _titulo, _fecha in cand["ses"]:
        sys.path.insert(0, AQUI)
        import vault_lookup as L
        ruta = os.path.join(vault, "sessions", L.slug_ses(sid) + ".md")
        if not os.path.exists(ruta):
            continue
        t = recortar_ses(open(ruta, errors="replace").read())
        if not cabe(t):
            break
        partes.append(t)
        gastado += len(t)
        refs.add(f"ses-{sid}")

    # Los documentos entran como puntero: abrirlos enteros se comeria el
    # presupuesto y su valor aqui es decir «mira tambien aqui».
    if cand["docs"]:
        t = "documentos que lo mencionan:\n" + "\n".join(
            f"  [doc {d}]" for d in cand["docs"])
        if cabe(t):
            partes.append(t)
            gastado += len(t)
            refs.update(f"doc {d}" for d in cand["docs"])

    for ruta, lineas, rastreado in sueltos:
        if rastreado is False:
            marca = " (sin commitear, invisible para el vault)"
        elif rastreado is None:
            marca = " (de thoughts/, estado en git desconocido)"
        else:
            marca = " (de thoughts/)"
        t = f"[{ruta}]{marca}\n" + "\n".join(
            "  " + l for l in lineas.splitlines())
        if not cabe(t):
            break
        partes.append(t)
        gastado += len(t)
        refs.add(ruta)

    return "\n\n".join(partes), refs


def encendido():
    """El interruptor es un fichero o una variable de entorno. Apagado por defecto."""
    if os.environ.get("VAULT_SCOUT_ON", "").strip() in ("1", "true", "yes"):
        return True
    return os.path.exists(INTERRUPTOR)


def es_referencia_directa(pregunta):
    """Quien ya sabe que quiere abrir no debe pagar la latencia del scout."""
    t = pregunta.strip()
    if not t:
        return False
    return bool(t.isdigit() or RE_UUID.match(t) or _parece_ruta(t))


def _parece_ruta(t):
    # se importa perezosamente para que --check funcione sin projects.json
    sys.path.insert(0, AQUI)
    import vault_lookup as L
    return L.parece_ruta(t)


def passthrough(args):
    """Delega en vault_lookup sin capturar nada: la salida debe ser identica."""
    return subprocess.run([sys.executable, LOOKUP, *args]).returncode


def _raiz_repo():
    """thoughts/ cuelga de la raiz, y el scout se lanza desde cualquier subdirectorio."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return os.getcwd()
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else os.getcwd()


def main():
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    libres = [a for a in args if not a.startswith("--")]
    pregunta = " ".join(libres)

    if "--check" in flags:
        return check()

    # El interruptor primero: apagado, TODO delega en vault_lookup, incluido el
    # caso sin argumentos. Es la garantia de paridad de salida y codigo de retorno.
    if not encendido() or es_referencia_directa(pregunta):
        return passthrough(args)
    if not args:
        print(__doc__.strip())
        return 64

    sys.path.insert(0, AQUI)
    import vault_lookup as L
    try:
        proyecto, vault = L.resolver_vault()
    except SystemExit:
        # sin vault resuelto no hay nada que recolectar: que responda vault_lookup
        return passthrough(args)

    terminos = expandir(pregunta)
    if not terminos:
        return passthrough(args)
    cand = recolectar(proyecto, vault, terminos)
    sueltos = barrer_thoughts(_raiz_repo(), terminos)
    texto, refs = extracto(vault, cand, sueltos)
    if not texto.strip():
        return passthrough(args)

    if "--dry-run" in flags:
        print(f"terminos: {', '.join(terminos)}")
        print(f"referencias citables: {len(refs)}")
        print()
        print(texto)
        return 0

    veredicto = bloque_veredicto(sintetizar(prompt_extractivo(pregunta, texto, refs)))
    if not veredicto:
        print(texto)
        print("[scout: sin veredicto utilizable — extracto sin filtrar]", file=sys.stderr)
        return 0
    malas = refs_no_respaldadas(veredicto, refs)
    if malas:
        print(texto)
        print(f"[scout: el modelo cito {malas[:3]} fuera del corpus"
              " — extracto sin filtrar]", file=sys.stderr)
        return 0
    sin_ancla = lineas_sin_ancla(veredicto)
    if sin_ancla:
        print(texto)
        print(f"[scout: {len(sin_ancla)} linea(s) sin una referencia unica"
              " — extracto sin filtrar]", file=sys.stderr)
        return 0

    print(veredicto)
    sin_abrir = [f"obs {o[0]}" for o in cand["obs"] if f"obs {o[0]}" not in refs]
    if sin_abrir:
        print("\nSIN ABRIR (presupuesto): " + ", ".join(sin_abrir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
