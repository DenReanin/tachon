#!/usr/bin/env python3
"""Interfaz web local de Tachón.

Servidor Flask en 127.0.0.1: arrastras uno o varios PDFs o imágenes, indicas
(o detectas) los datos a borrar y descargas el fichero tachado (o un ZIP con
lote + informe.csv). Todo se procesa en local, sin red: la "detección
automática" son patrones con validación matemática (letra del DNI/NIE, CIF,
NSS mod 97, IBAN mod 97, Luhn para tarjetas), no un modelo de IA.

Uso:  py app.py   →  http://127.0.0.1:5000
"""

import csv
import io
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

try:
    import pymupdf as fitz
except ImportError:
    import fitz
from flask import Flask, jsonify, request, send_file
from PIL import Image

import tachon

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

OPCIONES = SimpleNamespace(
    umbral_texto=40, dpi=300, idioma="spa+eng",
    corte_fuzzy=88, forzar_ocr=False, sin_ocr=False,
)

# token → (ruta del resultado, nombre de descarga, carpeta temporal, timestamp)
DESCARGAS: dict[str, tuple[Path, str, Path, float]] = {}
TTL_DESCARGA_SEG = 900  # 15 minutos


def _limpiar_descargas_antiguas(max_age_sec: int = TTL_DESCARGA_SEG) -> None:
    """Elimina tokens caducados y sus directorios temporales en /tmp."""
    ahora = time.time()
    for token in list(DESCARGAS.keys()):
        ruta, _nombre, tmp_dir, creado = DESCARGAS[token]
        if ahora - creado > max_age_sec or not ruta.exists():
            DESCARGAS.pop(token, None)
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------- detección

LETRAS_DNI = "TRWAGMYFPDXBNJZSQVHLCKE"
_LETRAS_CIF_CTRL = "JABCDEFGHI"
_CONSONANTES_MATRICULA = frozenset("BCDFGHJKLMNPRSTVWXYZ")


def _limpio(cand: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", cand).upper()


def _dni_valido(cand: str) -> bool:
    c = _limpio(cand)
    return len(c) == 9 and c[:8].isdigit() and LETRAS_DNI[int(c[:8]) % 23] == c[8]


def _nie_valido(cand: str) -> bool:
    c = _limpio(cand)
    if len(c) != 9 or c[0] not in "XYZ" or not c[1:8].isdigit():
        return False
    numero = str("XYZ".index(c[0])) + c[1:8]
    return LETRAS_DNI[int(numero) % 23] == c[8]


def _cif_valido(cand: str) -> bool:
    c = _limpio(cand)
    if len(c) != 9 or c[0] not in "ABCDEFGHJNPQRSUVW" or not c[1:8].isdigit():
        return False
    digitos = c[1:8]
    suma_pares = sum(int(digitos[i]) for i in (1, 3, 5))
    suma_impares = 0
    for i in (0, 2, 4, 6):
        doble = int(digitos[i]) * 2
        suma_impares += (doble // 10) + (doble % 10)
    ctrl_num = (10 - ((suma_pares + suma_impares) % 10)) % 10
    ctrl_letra = _LETRAS_CIF_CTRL[ctrl_num]
    if c[0] in "PQRSNW":
        return c[8] == ctrl_letra
    if c[0] in "ABEH":
        return c[8] == str(ctrl_num)
    return c[8] in (str(ctrl_num), ctrl_letra)


def _nss_valido(cand: str) -> bool:
    d = re.sub(r"\D", "", cand)
    if len(d) not in (11, 12):
        return False
    prov = int(d[:2])
    if not (1 <= prov <= 53 or prov == 66):
        return False
    num = int(d[2:-2])
    ctrl = int(d[-2:])
    base = (prov * 10_000_000 + num) if num < 10_000_000 else int(f"{prov}{num}")
    return base % 97 == ctrl


def _iban_valido(cand: str) -> bool:
    c = _limpio(cand)
    if not (15 <= len(c) <= 34 and c[:2].isalpha() and c[2:4].isdigit()):
        return False
    return int("".join(str(int(x, 36)) for x in c[4:] + c[:4])) % 97 == 1


def _telefono_valido(cand: str) -> bool:
    d = re.sub(r"\D", "", cand)
    if d.startswith("0034"):
        d = d[4:]
    elif d.startswith("34") and len(d) == 11:
        d = d[2:]
    return len(d) == 9 and d[0] in "6789"


def _luhn_valido(cand: str) -> bool:
    d = re.sub(r"\D", "", cand)
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _matricula_valida(cand: str) -> bool:
    c = _limpio(cand)
    return (
        len(c) == 7
        and c[:4].isdigit()
        and all(ch in _CONSONANTES_MATRICULA for ch in c[4:])
    )


def _sello_valido(cand: str) -> bool:
    # Un sello/hash de verdad arrastra muchos dígitos; una palabra larga, no.
    return sum(ch.isdigit() for ch in cand) >= 8


def _direccion_valida(cand: str) -> bool:
    limpio = cand.strip(" .,;")
    return len(limpio) >= 8


MESES = ("enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
         "septiembre|setiembre|octubre|noviembre|diciembre")


def _fecha_valida(cand: str) -> bool:
    partes = re.findall(r"\d+", cand)
    if len(partes) == 2:  # «3 de enero de 1990» → día y año
        return 1 <= int(partes[0]) <= 31 and 1900 <= int(partes[1]) <= 2099
    if len(partes) != 3:
        return False
    if len(partes[0]) == 4:  # aaaa-mm-dd
        anio, mes, dia = (int(p) for p in partes)
    else:  # dd/mm/aaaa (o aa)
        dia, mes, anio = (int(p) for p in partes)
        if anio < 100:
            anio += 2000 if anio < 50 else 1900
    return 1 <= dia <= 31 and 1 <= mes <= 12 and 1900 <= anio <= 2099


def _cp_valido(cand: str) -> bool:
    return 1000 <= int(cand) <= 52999  # 01000–52999: provincias españolas


# Capitales de provincia y ciudades grandes. Amplía la lista si te hace falta.
LOCALIDADES = [
    "A Coruña", "Albacete", "Alcalá de Henares", "Alcorcón", "Alicante",
    "Almería", "Ávila", "Badajoz", "Badalona", "Barcelona", "Bilbao", "Burgos",
    "Cáceres", "Cádiz", "Cartagena", "Castellón", "Ceuta", "Ciudad Real",
    "Córdoba", "Cuenca", "Elche", "Fuenlabrada", "Getafe", "Gijón", "Girona",
    "Granada", "Guadalajara", "Huelva", "Huesca", "Jaén", "Jerez de la Frontera",
    "Las Palmas de Gran Canaria", "Leganés", "León", "Lleida", "Logroño", "Lugo",
    "Madrid", "Málaga", "Marbella", "Melilla", "Móstoles", "Murcia", "Ourense",
    "Oviedo", "Palencia", "Palma de Mallorca", "Pamplona", "Parla", "Pontevedra",
    "Sabadell", "Salamanca", "San Sebastián", "Santa Cruz de Tenerife",
    "Santander", "Santiago de Compostela", "Segovia", "Sevilla", "Soria",
    "Tarragona", "Terrassa", "Teruel", "Toledo", "Valencia", "Valladolid",
    "Vigo", "Vitoria", "Zamora", "Zaragoza",
]

# El OCR pierde acentos a menudo: cada vocal acentuable admite ambas formas.
_ACENTO_FLEX = {"a": "[aá]", "e": "[eé]", "i": "[ií]", "o": "[oó]", "u": "[uúü]",
                "á": "[aá]", "é": "[eé]", "í": "[ií]", "ó": "[oó]", "ú": "[uú]",
                "ñ": "[nñ]", "Á": "[AÁ]", "É": "[EÉ]", "Í": "[IÍ]", "Ó": "[OÓ]",
                "Ú": "[UÚ]"}


def _patron_localidades() -> re.Pattern:
    trozos = []
    for nombre in sorted(LOCALIDADES, key=len, reverse=True):
        trozos.append("".join(
            r"\s+" if c == " " else _ACENTO_FLEX.get(c, re.escape(c)) for c in nombre
        ))
    letra = "A-Za-zÁÉÍÓÚÜÑáéíóúüñ"
    return re.compile(f"(?<![{letra}])(?:{'|'.join(trozos)})(?![{letra}])")


# Caracteres que puede llevar un sello de operación/firma o un hash.
_SELLO = r"A-Za-z0-9+=_.\-"

# El orden importa: lo ya consumido (p. ej. un IBAN) no se reinterpreta como
# teléfono, tarjeta o fecha por sus dígitos.
DETECTORES = [
    ("IBAN", re.compile(r"\b[A-Za-z]{2}\d{2}(?:[ .-]?[A-Za-z0-9]{4}){2,7}(?:[ .-]?[A-Za-z0-9]{1,3})?\b"), _iban_valido),
    ("Correo", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None),
    ("DNI", re.compile(r"\b\d{2}[ .]?\d{3}[ .]?\d{3}[ -]?[A-Za-z]\b"), _dni_valido),
    ("NIE", re.compile(r"\b[XYZxyz][ .-]?\d{3}[ .]?\d{4}[ -]?[A-Za-z]\b"), _nie_valido),
    ("CIF", re.compile(r"\b[ABCDEFGHJNPQRSUVWabcdefghjnpqrsuvw][ -]?\d{7}[ -]?[0-9A-Ja-j]\b"), _cif_valido),
    ("Tarjeta", re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"), _luhn_valido),
    ("NSS", re.compile(r"(?<!\d)\d{2}[ /.-]?\d{7,8}[ /.-]?\d{2}(?!\d)"), _nss_valido),
    ("Teléfono", re.compile(r"(?<!\d)(?:\+34[ .-]?|0034[ .-]?)?[6789](?:[ .-]?\d){8}(?!\d)"), _telefono_valido),
    ("Matrícula", re.compile(r"\b\d{4}[ -]?[BCDFGHJKLMNPRSTVWXYZ]{3}\b"), _matricula_valida),
    ("Sello/hash", re.compile(f"(?<![{_SELLO}])[A-Za-z0-9][{_SELLO}]{{18,}}[A-Za-z0-9=](?![{_SELLO}])"), _sello_valido),
    ("Fecha", re.compile(r"\b\d{1,2}\s+de\s+(?:" + MESES + r")(?:\s+del?)?\s+\d{4}\b", re.IGNORECASE), _fecha_valida),
    ("Fecha", re.compile(r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}|\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{1,2}-\d{1,2})\b"), _fecha_valida),
    ("Cód. postal", re.compile(r"(?<!\d)\d{5}(?!\d)"), _cp_valido),
    ("Dirección", re.compile(r"\b(?:C/|Calle|Avda\.?|Avenida|Paseo|Plaza|Pza\.?|Camino|Carretera|Ctra\.?|Ronda|Traves[ií]a|Urbanizaci[oó]n|Urb\.?)\s+[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9 ,.ºª\-/#]{4,60}"), _direccion_valida),
    ("Localidad", _patron_localidades(), None),
]

# Tipos con validación fuerte: salen marcados de serie en la interfaz.
# El resto (fechas, códigos postales, direcciones, localidades…) se ofrece desmarcado,
# porque no todo lo que cuadra en formato es un dato personal tuyo.
FIABLES = {"IBAN", "Correo", "DNI", "NIE", "CIF", "NSS", "Teléfono", "Tarjeta", "Matrícula", "Sello/hash"}


# Tipos que, si falla su validación, se ofrecen igualmente rebajados a "posible":
# para borrar es mejor sugerir de más (el usuario desmarca) que callar de menos.
DEGRADADOS = {"IBAN": "Posible IBAN"}


def detectar_datos(texto: str) -> list[dict]:
    """Busca datos con formato conocido en el texto. Local, sin IA."""
    consumidos: list[tuple[int, int]] = []
    vistos: set[tuple[str, str]] = set()
    resultado: list[dict] = []
    for tipo, patron, valida in DETECTORES:
        for m in patron.finditer(texto):
            if any(m.start() < fin and ini < m.end() for ini, fin in consumidos):
                continue
            valor = m.group().strip(" \t\r\n.,;") if tipo == "Dirección" else m.group().strip()
            if valida and not valida(valor):
                tipo_final = DEGRADADOS.get(tipo)
                if tipo_final is None:
                    continue
            else:
                tipo_final = tipo
            consumidos.append((m.start(), m.end()))
            clave = (tipo_final, tachon.normalizar(valor))
            if clave in vistos:
                continue
            vistos.add(clave)
            resultado.append({"tipo": tipo_final, "valor": valor,
                              "fiable": tipo_final in FIABLES})
    return resultado[:200]


def extraer_texto_rapido(ruta: Path, opciones=OPCIONES) -> str:
    """Texto del documento para la detección: capa de texto, y OCR si es escaneado."""
    import pytesseract

    if ruta.suffix.lower() in tachon.EXT_PDF:
        doc = fitz.open(ruta)
        partes = []
        for page in doc:
            texto = page.get_text()
            if len(texto.strip()) >= opciones.umbral_texto and not opciones.forzar_ocr:
                partes.append(texto)
            elif not opciones.sin_ocr:
                pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                partes.append(pytesseract.image_to_string(img, lang=opciones.idioma))
        doc.close()
        return "\n".join(partes)
    if opciones.sin_ocr:
        return ""
    img = Image.open(ruta).convert("RGB")
    return pytesseract.image_to_string(img, lang=opciones.idioma)


# ------------------------------------------------------------------- rutas

def _leer_opciones_form() -> SimpleNamespace:
    def _as_bool(val: str | None, default: bool) -> bool:
        if val is None:
            return default
        return val.strip().lower() in ("1", "true", "si", "sí", "on")

    try:
        dpi = max(72, min(600, int(request.form.get("dpi") or OPCIONES.dpi)))
    except ValueError:
        dpi = OPCIONES.dpi
    try:
        corte = max(50.0, min(100.0, float(request.form.get("corte_fuzzy") or OPCIONES.corte_fuzzy)))
    except ValueError:
        corte = OPCIONES.corte_fuzzy

    return SimpleNamespace(
        umbral_texto=OPCIONES.umbral_texto,
        dpi=dpi,
        idioma=OPCIONES.idioma,
        corte_fuzzy=corte,
        forzar_ocr=_as_bool(request.form.get("forzar_ocr"), OPCIONES.forzar_ocr),
        sin_ocr=_as_bool(request.form.get("sin_ocr"), OPCIONES.sin_ocr),
    )


def _guardar_subidas() -> tuple[list[tuple[Path, str]], Path] | tuple[None, str]:
    """Guarda uno o varios ficheros subidos en un único directorio temporal."""
    archivos = [f for f in request.files.getlist("ficheros") if f and f.filename]
    if not archivos:
        unico = request.files.get("fichero")
        if unico and unico.filename:
            archivos = [unico]

    if not archivos:
        return None, "No ha llegado ningún fichero."

    exts_validas = tachon.EXT_PDF | tachon.EXT_IMAGEN
    for f in archivos:
        sufijo = Path(f.filename).suffix.lower()
        if sufijo not in exts_validas:
            return None, f"Extensión no admitida en «{f.filename}»: {sufijo or '(ninguna)'}. Vale PDF o imagen."

    tmp = Path(tempfile.mkdtemp(prefix="tachon_"))
    guardados: list[tuple[Path, str]] = []
    for idx, f in enumerate(archivos):
        nombre_orig = Path(f.filename).name
        seguro = re.sub(r"[^\w.\-]", "_", nombre_orig)
        ruta = tmp / f"{idx:03d}_{seguro}" if len(archivos) > 1 else tmp / seguro
        f.save(ruta)
        guardados.append((ruta, nombre_orig))
    return guardados, tmp


@app.get("/")
def portada():
    return PAGINA


@app.post("/detectar")
def detectar():
    tachon._configurar_tesseract()
    subidos, extra = _guardar_subidas()
    if subidos is None:
        return jsonify({"error": extra}), 400
    tmp_dir = extra
    opciones = _leer_opciones_form()
    try:
        textos = [extraer_texto_rapido(ruta, opciones) for ruta, _ in subidos]
        detectados = detectar_datos("\n".join(textos))
        return jsonify({"detectados": detectados})
    except Exception as e:
        return jsonify({"error": f"No se pudo leer el documento: {e}"}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.post("/procesar")
def procesar():
    _limpiar_descargas_antiguas()
    tachon._configurar_tesseract()
    subidos, extra = _guardar_subidas()
    if subidos is None:
        return jsonify({"error": extra}), 400
    tmp_dir = extra

    avisos: list[str] = []
    datos = tachon.datos_desde_lineas(
        request.form.get("datos", "").splitlines(),
        avisar=lambda m: avisos.append(m.strip()),
    )
    if not datos:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": "La lista de datos a borrar está vacía."}), 400

    opciones = _leer_opciones_form()
    resumen: list[str] = list(avisos)
    grave = False
    filas_csv: list[list] = []
    salidas: list[tuple[Path, str]] = []

    try:
        for ruta, nombre_orig in subidos:
            stem_orig = Path(nombre_orig).stem
            sufijo = ruta.suffix.lower()
            nombre_out = f"{stem_orig}_filtrado{ruta.suffix}"
            destino = ruta.with_name(f"out_{ruta.stem}_filtrado{ruta.suffix}")

            for d in datos:
                d.coincidencias = 0
                d.aproximadas = 0

            try:
                if sufijo in tachon.EXT_PDF:
                    info = tachon.procesar_pdf(ruta, destino, datos, opciones)
                    alertas = tachon.verificar_pdf(destino, datos)
                else:
                    info = tachon.procesar_imagen(ruta, destino, datos, opciones)
                    alertas = ["NOTA: en imágenes no hay capa de texto que verificar; "
                               "revisa el resultado a ojo."]
            finally:
                # Cero fugas: eliminamos el fichero original sin tachar inmediatamente.
                ruta.unlink(missing_ok=True)

            salidas.append((destino, nombre_out))
            prefijo = f"[{nombre_orig}] " if len(subidos) > 1 else ""
            resumen.append(f"{prefijo}Modo: {info['modo']}")
            for d in datos:
                extra_txt = f" ({d.aproximadas} aproximada(s) por OCR, revísalas)" if d.aproximadas else ""
                resumen.append(f"{prefijo}«{d.etiqueta}»: {d.coincidencias} coincidencia(s){extra_txt}")
                filas_csv.append([nombre_orig, info["modo"], d.etiqueta,
                                  d.coincidencias, d.aproximadas, ""])
            for d in datos:
                if d.coincidencias == 0:
                    resumen.append(f"{prefijo}AVISO: «{d.etiqueta}» no apareció ni una vez — "
                                   "puede estar en un formato sin cubrir.")
            for alerta in alertas:
                resumen.append(f"{prefijo}{alerta}")
                filas_csv.append([nombre_orig, info["modo"], "", "", "", alerta])
                if alerta.startswith("ALERTA"):
                    grave = True
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return jsonify({"error": f"Fallo al procesar: {e}"}), 500

    token = uuid.uuid4().hex
    if len(salidas) == 1:
        ruta_final, nombre_descarga = salidas[0]
    else:
        ruta_final = tmp_dir / "tachon_lote_filtrado.zip"
        nombre_descarga = "tachon_lote_filtrado.zip"
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(["fichero", "modo", "dato", "coincidencias", "aproximadas", "nota"])
        writer.writerows(filas_csv)

        with zipfile.ZipFile(ruta_final, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path_out, arc_name in salidas:
                zf.write(path_out, arcname=arc_name)
                path_out.unlink(missing_ok=True)
            zf.writestr("informe.csv", csv_buf.getvalue().encode("utf-8"))

    DESCARGAS[token] = (ruta_final, nombre_descarga, tmp_dir, time.time())
    return jsonify({"token": token, "resumen": resumen, "grave": grave})


@app.get("/descargar/<token>")
def descargar(token):
    _limpiar_descargas_antiguas()
    par = DESCARGAS.get(token)
    if par is None:
        return "Descarga no encontrada o caducada.", 404
    ruta, nombre, _tmp_dir, _ts = par
    return send_file(ruta, as_attachment=True, download_name=nombre)


# ------------------------------------------------------------------ página

PAGINA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tachón // Redacción Real Local</title>
<link rel="icon" href="data:,">
<style>
  :root {
    --bg: #121215;
    --panel: #18181b;
    --canvas: #0c0c0e;
    --sunken: #060607;
    --border: #27272a;
    --border-strong: #3f3f46;
    --ink: #f4f4f5;
    --text-sec: #a1a1aa;
    --text-ter: #71717a;
    --accent: #ef4444;
    --accent-hover: #dc2626;
    --accent-on: #ffffff;
    --warn: #f59e0b;
    --danger: #ef4444;
    --ok: #22c55e;
  }
  html.light {
    --bg: #ffffff;
    --panel: #ffffff;
    --canvas: #f5f5f5;
    --sunken: #ededef;
    --border: #e6e6e6;
    --border-strong: #d4d4d8;
    --ink: #111113;
    --text-sec: #52525b;
    --text-ter: #71717a;
    --accent: #111113;
    --accent-hover: #27272a;
    --accent-on: #ffffff;
    --warn: #b45309;
    --danger: #b91c1c;
    --ok: #15803d;
  }
  * { box-sizing: border-box; border-radius: 0 !important; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: var(--canvas);
    color: var(--ink);
    margin: 0;
    padding: 1.5rem 1rem 3rem;
  }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  main { max-width: 780px; margin: 0 auto; display: grid; gap: 1rem; }
  header.top {
    display: flex; align-items: center; justify-content: space-between;
    gap: 1rem; border: 1px solid var(--border); background: var(--panel);
    padding: .85rem 1rem;
  }
  h1 { margin: 0; font-size: 1.25rem; letter-spacing: -.02em; display: flex; align-items: center; gap: .6rem; }
  h1 span.badge {
    background: var(--accent); color: var(--accent-on);
    padding: .15rem .45rem; font-size: .85rem; text-transform: uppercase;
  }
  p.sub { margin: .25rem 0 0; color: var(--text-sec); font-size: .84rem; }
  .tarjeta { background: var(--bg); border: 1px solid var(--border); padding: 1rem; }
  #zona {
    border: 1px dashed var(--border-strong); padding: 1.7rem 1rem;
    text-align: center; color: var(--text-sec); cursor: pointer;
    background: var(--panel); transition: border-color .15s, background .15s;
    font-size: .92rem;
  }
  #zona.encima { border-color: var(--accent); background: var(--sunken); color: var(--ink); }
  #zona.conFichero { border-style: solid; border-color: var(--accent); color: var(--ink); }
  h2 {
    font-size: .78rem; margin: 0 0 .75rem; text-transform: uppercase;
    letter-spacing: .08em; color: var(--text-ter);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .campos { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }
  @media (max-width: 600px) { .campos { grid-template-columns: 1fr; } }
  .campos input, textarea, .input-inline {
    width: 100%; padding: .5rem .6rem; border: 1px solid var(--border);
    background: var(--sunken); color: var(--ink); font: inherit; font-size: .88rem;
  }
  .campos input:focus, textarea:focus, .input-inline:focus {
    outline: none; border-color: var(--border-strong);
  }
  textarea { margin-top: .5rem; resize: vertical; }
  .fila-acciones { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin-top: .65rem; }
  button {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .75rem; font-weight: 600; text-transform: uppercase; letter-spacing: .05em;
    border: 1px solid var(--border-strong); background: var(--panel); color: var(--ink);
    padding: .5rem .8rem; cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--ink); }
  button:disabled { opacity: .45; cursor: wait; }
  #btnProcesar {
    background: var(--accent); border-color: var(--accent); color: var(--accent-on);
    width: 100%; padding: .85rem; font-size: .9rem;
  }
  #btnProcesar:hover:not(:disabled) { background: var(--accent-hover); }
  .panel-detectados { margin-top: .75rem; display: none; border-top: 1px solid var(--border); padding-top: .75rem; }
  .controles-det { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; margin-bottom: .5rem; }
  .controles-det input { flex: 1; min-width: 150px; padding: .38rem .5rem; font-size: .8rem; }
  #detectados { display: grid; gap: .25rem; max-height: 280px; overflow-y: auto; }
  #detectados label {
    display: flex; align-items: center; gap: .5rem;
    padding: .35rem .5rem; background: var(--sunken); border: 1px solid var(--border);
    font-size: .86rem; cursor: pointer;
  }
  .tipo {
    background: var(--panel); border: 1px solid var(--border-strong); color: var(--ink);
    padding: .05rem .38rem; font-size: .7rem; white-space: nowrap;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; text-transform: uppercase;
  }
  details.opciones { margin-top: .75rem; border: 1px solid var(--border); background: var(--sunken); padding: .55rem .75rem; }
  details.opciones summary {
    cursor: pointer; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .75rem; text-transform: uppercase; color: var(--text-sec);
  }
  .grid-opts { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .6rem; margin-top: .65rem; font-size: .82rem; }
  .grid-opts label { display: flex; align-items: center; gap: .4rem; color: var(--text-sec); }
  .grid-opts input[type="number"] {
    width: 72px; padding: .25rem .4rem; border: 1px solid var(--border);
    background: var(--panel); color: var(--ink); font-family: ui-monospace, monospace;
  }
  #resumen { display: none; }
  #resumen ul { margin: .4rem 0 0; padding-left: 1.2rem; }
  #resumen li { margin: .2rem 0; font-size: .86rem; }
  li.aviso { color: var(--warn); }
  li.alerta { color: var(--danger); font-weight: 700; }
  .nota { color: var(--text-ter); font-size: .78rem; margin: .5rem 0 0; line-height: 1.4; }
</style>
</head>
<body>
<main>
  <header class="top">
    <div>
      <h1><span class="badge mono">Tachón</span> <span class="mono" style="font-size:.8rem;color:var(--text-sec);">REDACCIÓN REAL</span></h1>
      <p class="sub">Borra datos personales de PDFs e imágenes (1 o varios a la vez). 100 % local, sin red.</p>
    </div>
    <button type="button" id="btnTema">TEMA</button>
  </header>

  <div id="zona">Arrastra aquí tus PDFs o imágenes (admite lote múltiple)<br>(o haz clic para elegirlos)</div>
  <input type="file" id="fichero" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" multiple hidden>

  <div class="tarjeta">
    <h2>1. Datos a eliminar</h2>
    <div class="campos">
      <input placeholder="DNI / NIE / CIF (12345678Z)">
      <input placeholder="Teléfono (612345678)">
      <input placeholder="Correo electrónico">
      <input placeholder="Nombre y apellidos">
      <input placeholder="Dirección (Calle Mayor 3)">
      <input placeholder="IBAN o NSS">
    </div>
    <textarea rows="3" id="otros"
      placeholder="Otros datos: uno por línea (re: para expresiones regulares, # para comentarios)"></textarea>

    <div class="fila-acciones">
      <button type="button" id="btnDetectar">Detectar datos automáticamente</button>
      <button type="button" id="btnImportarTxt">Importar datos.txt</button>
      <button type="button" id="btnExportarTxt">Exportar datos.txt</button>
      <input type="file" id="inputTxt" accept=".txt" hidden>
    </div>

    <p class="nota">No hace falta escribir cada formato: «612345678» también borra «612 34 56 78» o «+34 612 345 678», incluidos saltos de línea y enlaces <code>mailto:</code>/<code>tel:</code>.</p>

    <div class="panel-detectados" id="panelDetectados">
      <div class="controles-det">
        <button type="button" id="btnMarcarTodos">Marcar todos</button>
        <button type="button" id="btnSoloFiables">Solo fiables</button>
        <button type="button" id="btnDesmarcarTodos">Desmarcar todos</button>
        <input type="text" id="filtroDetectados" class="input-inline" placeholder="Filtrar detectados...">
      </div>
      <div id="detectados"></div>
    </div>

    <details class="opciones">
      <summary>Opciones avanzadas del motor (OCR y tolerancia)</summary>
      <div class="grid-opts">
        <label><input type="checkbox" id="optForzarOcr"> Forzar OCR en todas las páginas</label>
        <label><input type="checkbox" id="optSinOcr"> Saltar páginas escaneadas (--sin-ocr)</label>
        <label>DPI OCR: <input type="number" id="optDpi" min="72" max="600" value="300"></label>
        <label>Fuzzy (%): <input type="number" id="optFuzzy" min="50" max="100" value="88"></label>
      </div>
    </details>

    <p class="nota">Detección local sin IA: IBAN, Correo, DNI, NIE, CIF, NSS, Teléfono, Tarjeta, Matrícula, Sello/hash (fiables) + Fecha, Cód. postal, Dirección y Localidad (desmarcados por defecto).</p>
  </div>

  <button type="button" id="btnProcesar">Tachar y descargar</button>

  <div class="tarjeta" id="resumen"><h2>Resultado</h2><ul></ul></div>
</main>

<script>
"use strict";
let ficheros = [];

const zona = document.getElementById("zona");
const inputFichero = document.getElementById("fichero");
const btnDetectar = document.getElementById("btnDetectar");
const btnProcesar = document.getElementById("btnProcesar");
const panelDetectados = document.getElementById("panelDetectados");
const cajaDetectados = document.getElementById("detectados");
const cajaResumen = document.getElementById("resumen");
const filtroDetectados = document.getElementById("filtroDetectados");
const btnTema = document.getElementById("btnTema");

function aplicarTema(modo) {
  document.documentElement.classList.toggle("light", modo === "light");
  localStorage.setItem("tachon_tema", modo);
}
aplicarTema(localStorage.getItem("tachon_tema") || "dark");
btnTema.addEventListener("click", () => {
  const esClaro = document.documentElement.classList.contains("light");
  aplicarTema(esClaro ? "dark" : "light");
});

function ponFicheros(lista) {
  const arr = Array.from(lista || []);
  if (arr.length === 0) return;
  ficheros = arr;
  zona.classList.add("conFichero");
  if (arr.length === 1) {
    zona.textContent = "ARCHIVO LISTO: " + arr[0].name;
  } else {
    zona.textContent = arr.length + " ARCHIVOS LISTOS (SE DESCARGARÁ EN ZIP): " + arr.map(f => f.name).join(", ");
  }
  cajaDetectados.replaceChildren();
  panelDetectados.style.display = "none";
  cajaResumen.style.display = "none";
}

zona.addEventListener("click", () => inputFichero.click());
inputFichero.addEventListener("change", () => ponFicheros(inputFichero.files));
zona.addEventListener("dragover", e => { e.preventDefault(); zona.classList.add("encima"); });
zona.addEventListener("dragleave", () => zona.classList.remove("encima"));
zona.addEventListener("drop", e => {
  e.preventDefault();
  zona.classList.remove("encima");
  ponFicheros(e.dataTransfer.files);
});

function anadirOpciones(fd) {
  fd.append("forzar_ocr", document.getElementById("optForzarOcr").checked ? "true" : "false");
  fd.append("sin_ocr", document.getElementById("optSinOcr").checked ? "true" : "false");
  fd.append("dpi", document.getElementById("optDpi").value || "300");
  fd.append("corte_fuzzy", document.getElementById("optFuzzy").value || "88");
}

btnDetectar.addEventListener("click", async () => {
  if (ficheros.length === 0) { alert("Primero elige uno o varios ficheros."); return; }
  btnDetectar.disabled = true;
  btnDetectar.textContent = "Leyendo documento(s)…";
  try {
    const fd = new FormData();
    for (const f of ficheros) fd.append("ficheros", f);
    anadirOpciones(fd);
    const r = await fetch("/detectar", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    cajaDetectados.replaceChildren();
    panelDetectados.style.display = "block";
    if (j.detectados.length === 0) {
      const p = document.createElement("p");
      p.className = "nota";
      p.textContent = "No se ha detectado ningún dato con formato conocido.";
      cajaDetectados.append(p);
    }
    for (const d of j.detectados) {
      const label = document.createElement("label");
      label.dataset.fiable = d.fiable !== false ? "1" : "0";
      label.dataset.texto = (d.tipo + " " + d.valor).toLowerCase();
      const caja = document.createElement("input");
      caja.type = "checkbox";
      caja.checked = d.fiable !== false;
      caja.value = d.valor;
      const tipo = document.createElement("span");
      tipo.className = "tipo";
      tipo.textContent = d.tipo;
      label.append(caja, tipo, document.createTextNode(d.valor));
      cajaDetectados.append(label);
    }
  } catch (err) {
    alert("Error al detectar: " + err.message);
  } finally {
    btnDetectar.disabled = false;
    btnDetectar.textContent = "Detectar datos automáticamente";
  }
});

document.getElementById("btnMarcarTodos").addEventListener("click", () => {
  for (const c of cajaDetectados.querySelectorAll("input[type=checkbox]")) c.checked = true;
});
document.getElementById("btnDesmarcarTodos").addEventListener("click", () => {
  for (const c of cajaDetectados.querySelectorAll("input[type=checkbox]")) c.checked = false;
});
document.getElementById("btnSoloFiables").addEventListener("click", () => {
  for (const lbl of cajaDetectados.querySelectorAll("label")) {
    const c = lbl.querySelector("input[type=checkbox]");
    if (c) c.checked = lbl.dataset.fiable === "1";
  }
});
filtroDetectados.addEventListener("input", () => {
  const q = filtroDetectados.value.trim().toLowerCase();
  for (const lbl of cajaDetectados.querySelectorAll("label")) {
    lbl.style.display = !q || lbl.dataset.texto.includes(q) ? "flex" : "none";
  }
});

function recogerDatos() {
  const lineas = [];
  for (const el of document.querySelectorAll(".campos input"))
    if (el.value.trim()) lineas.push(el.value.trim());
  for (const linea of document.getElementById("otros").value.split("\\n"))
    if (linea.trim()) lineas.push(linea.trim());
  for (const caja of cajaDetectados.querySelectorAll("input:checked"))
    lineas.push(caja.value);
  return [...new Set(lineas)];
}

document.getElementById("btnImportarTxt").addEventListener("click", () => {
  document.getElementById("inputTxt").click();
});
document.getElementById("inputTxt").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const txt = await file.text();
  const otros = document.getElementById("otros");
  otros.value = (otros.value.trim() ? otros.value.trim() + "\\n" : "") + txt.trim();
});
document.getElementById("btnExportarTxt").addEventListener("click", () => {
  const lineas = recogerDatos();
  if (lineas.length === 0) { alert("No hay datos escritos o marcados para exportar."); return; }
  const blob = new Blob([lineas.join("\\n") + "\\n"], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "mis_datos.txt";
  a.click();
  URL.revokeObjectURL(a.href);
});

btnProcesar.addEventListener("click", async () => {
  if (ficheros.length === 0) { alert("Primero elige uno o varios ficheros."); return; }
  const lineas = recogerDatos();
  if (lineas.length === 0) {
    alert("Indica al menos un dato a borrar (o usa la detección automática).");
    return;
  }
  btnProcesar.disabled = true;
  btnProcesar.textContent = "Tachando…";
  try {
    const fd = new FormData();
    for (const f of ficheros) fd.append("ficheros", f);
    fd.append("datos", lineas.join("\\n"));
    anadirOpciones(fd);
    const r = await fetch("/procesar", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    const lista = cajaResumen.querySelector("ul");
    lista.replaceChildren();
    for (const linea of j.resumen) {
      const li = document.createElement("li");
      li.textContent = linea;
      if (linea.startsWith("AVISO") || linea.startsWith("NOTA") ||
          linea.startsWith("SIN CAPA") || linea.includes("] AVISO:") ||
          linea.includes("] NOTA:") || linea.includes("] SIN CAPA")) li.className = "aviso";
      if (linea.startsWith("ALERTA") || linea.includes("] ALERTA")) li.className = "alerta";
      lista.append(li);
    }
    cajaResumen.style.display = "block";
    window.location.href = "/descargar/" + j.token;
  } catch (err) {
    alert("Error al procesar: " + err.message);
  } finally {
    btnProcesar.disabled = false;
    btnProcesar.textContent = "Tachar y descargar";
  }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    tachon._configurar_tesseract()
    port = int(os.environ.get("TACHON_PORT", "5000"))
    print(f"Tachón en marcha: abre http://127.0.0.1:{port} en el navegador (Ctrl+C para parar).")
    app.run(host="127.0.0.1", port=port, debug=False)
