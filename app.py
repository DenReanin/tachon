#!/usr/bin/env python3
"""Interfaz web local de Tachón.

Servidor Flask en 127.0.0.1: arrastras un PDF o imagen, indicas (o detectas)
los datos a borrar y descargas el fichero tachado. Todo se procesa en local,
sin red: la "detección automática" son patrones con validación (letra del
DNI/NIE, IBAN módulo 97, Luhn para tarjetas), no un modelo de IA.

Uso:  py app.py   →  http://127.0.0.1:5000
"""

import os
import re
import tempfile
import uuid
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
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

OPCIONES = SimpleNamespace(
    umbral_texto=40, dpi=300, idioma="spa+eng",
    corte_fuzzy=88, forzar_ocr=False, sin_ocr=False,
)

# token → (ruta del resultado, nombre de descarga)
DESCARGAS: dict[str, tuple[Path, str]] = {}


# ---------------------------------------------------------------- detección

LETRAS_DNI = "TRWAGMYFPDXBNJZSQVHLCKE"


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


def _sello_valido(cand: str) -> bool:
    # Un sello/hash de verdad arrastra muchos dígitos; una palabra larga, no.
    return sum(ch.isdigit() for ch in cand) >= 8


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
    ("Teléfono", re.compile(r"(?<!\d)(?:\+34[ .-]?|0034[ .-]?)?[6789](?:[ .-]?\d){8}(?!\d)"), _telefono_valido),
    ("Tarjeta", re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"), _luhn_valido),
    ("Sello/hash", re.compile(f"(?<![{_SELLO}])[A-Za-z0-9][{_SELLO}]{{18,}}[A-Za-z0-9=](?![{_SELLO}])"), _sello_valido),
    ("Fecha", re.compile(r"\b\d{1,2}\s+de\s+(?:" + MESES + r")(?:\s+del?)?\s+\d{4}\b", re.IGNORECASE), _fecha_valida),
    ("Fecha", re.compile(r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}-\d{1,2}-\d{2,4}|\d{1,2}\.\d{1,2}\.\d{4}|\d{4}-\d{1,2}-\d{1,2})\b"), _fecha_valida),
    ("Cód. postal", re.compile(r"(?<!\d)\d{5}(?!\d)"), _cp_valido),
    ("Localidad", _patron_localidades(), None),
]

# Tipos con validación fuerte: salen marcados de serie en la interfaz.
# El resto (fechas, códigos postales, localidades…) se ofrece desmarcado,
# porque no todo lo que cuadra en formato es un dato personal tuyo.
FIABLES = {"IBAN", "Correo", "DNI", "NIE", "Teléfono", "Tarjeta", "Sello/hash"}


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
            valor = m.group().strip()
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
    return resultado[:100]


def extraer_texto_rapido(ruta: Path) -> str:
    """Texto del documento para la detección: capa de texto, y OCR si es escaneado."""
    import pytesseract

    if ruta.suffix.lower() in tachon.EXT_PDF:
        doc = fitz.open(ruta)
        partes = []
        for page in doc:
            texto = page.get_text()
            if len(texto.strip()) >= OPCIONES.umbral_texto:
                partes.append(texto)
            else:
                pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                partes.append(pytesseract.image_to_string(img, lang=OPCIONES.idioma))
        doc.close()
        return "\n".join(partes)
    img = Image.open(ruta).convert("RGB")
    return pytesseract.image_to_string(img, lang=OPCIONES.idioma)


# ------------------------------------------------------------------- rutas

def _guardar_subida() -> tuple[Path, Path] | tuple[None, str]:
    """Guarda el fichero subido en un directorio temporal propio."""
    f = request.files.get("fichero")
    if f is None or not f.filename:
        return None, "No ha llegado ningún fichero."
    nombre = Path(f.filename).name
    sufijo = Path(nombre).suffix.lower()
    if sufijo not in tachon.EXT_PDF | tachon.EXT_IMAGEN:
        return None, f"Extensión no admitida: {sufijo or '(ninguna)'}. Vale PDF o imagen."
    tmp = Path(tempfile.mkdtemp(prefix="tachon_"))
    ruta = tmp / re.sub(r"[^\w.\-]", "_", nombre)
    f.save(ruta)
    return ruta, tmp


@app.get("/")
def portada():
    return PAGINA


@app.post("/detectar")
def detectar():
    ruta, extra = _guardar_subida()
    if ruta is None:
        return jsonify({"error": extra}), 400
    try:
        detectados = detectar_datos(extraer_texto_rapido(ruta))
    except Exception as e:
        return jsonify({"error": f"No se pudo leer el documento: {e}"}), 500
    return jsonify({"detectados": detectados})


@app.post("/procesar")
def procesar():
    ruta, extra = _guardar_subida()
    if ruta is None:
        return jsonify({"error": extra}), 400

    avisos: list[str] = []
    datos = tachon.datos_desde_lineas(
        request.form.get("datos", "").splitlines(),
        avisar=lambda m: avisos.append(m.strip()),
    )
    if not datos:
        return jsonify({"error": "La lista de datos a borrar está vacía."}), 400

    destino = ruta.parent / f"{ruta.stem}_filtrado{ruta.suffix}"
    resumen: list[str] = list(avisos)
    grave = False
    try:
        if ruta.suffix.lower() in tachon.EXT_PDF:
            info = tachon.procesar_pdf(ruta, destino, datos, OPCIONES)
            alertas = tachon.verificar_pdf(destino, datos)
        else:
            info = tachon.procesar_imagen(ruta, destino, datos, OPCIONES)
            alertas = ["NOTA: en imágenes no hay capa de texto que verificar; "
                       "revisa el resultado a ojo."]
    except Exception as e:
        return jsonify({"error": f"Fallo al procesar: {e}"}), 500

    resumen.append(f"Modo: {info['modo']}")
    for d in datos:
        extra_txt = f" ({d.aproximadas} aproximada(s) por OCR, revísalas)" if d.aproximadas else ""
        resumen.append(f"«{d.etiqueta}»: {d.coincidencias} coincidencia(s){extra_txt}")
    for d in datos:
        if d.coincidencias == 0:
            resumen.append(f"AVISO: «{d.etiqueta}» no apareció ni una vez — "
                           "puede estar en un formato sin cubrir.")
    for alerta in alertas:
        resumen.append(alerta)
        if alerta.startswith("ALERTA"):
            grave = True

    token = uuid.uuid4().hex
    nombre_descarga = f"{Path(request.files['fichero'].filename).stem}_filtrado{ruta.suffix}"
    DESCARGAS[token] = (destino, nombre_descarga)
    return jsonify({"token": token, "resumen": resumen, "grave": grave})


@app.get("/descargar/<token>")
def descargar(token):
    par = DESCARGAS.get(token)
    if par is None:
        return "Descarga no encontrada o caducada.", 404
    ruta, nombre = par
    return send_file(ruta, as_attachment=True, download_name=nombre)


# ------------------------------------------------------------------ página

PAGINA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tachón</title>
<link rel="icon" href="data:,">
<style>
  * { box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; background: #f3f4f6; color: #1f2937;
         margin: 0; padding: 2rem 1rem; }
  main { max-width: 700px; margin: 0 auto; display: grid; gap: 1rem; }
  h1 { margin: 0; font-size: 1.6rem; }
  h1 span { background: #111; color: #fff; padding: 0 .4rem; }
  p.sub { margin: .2rem 0 0; color: #6b7280; font-size: .9rem; }
  .tarjeta { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
             padding: 1.1rem; }
  #zona { border: 2px dashed #9ca3af; border-radius: 10px; padding: 2.2rem 1rem;
          text-align: center; color: #6b7280; cursor: pointer; background: #fff;
          transition: border-color .15s, background .15s; }
  #zona.encima { border-color: #111; background: #eef2ff; }
  #zona.conFichero { border-style: solid; border-color: #111; color: #111; }
  h2 { font-size: 1rem; margin: 0 0 .7rem; }
  .campos { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }
  .campos input, textarea { width: 100%; padding: .5rem .6rem; border: 1px solid #d1d5db;
          border-radius: 6px; font: inherit; }
  textarea { margin-top: .5rem; resize: vertical; }
  button { font: inherit; border: 0; border-radius: 8px; padding: .6rem 1rem;
           cursor: pointer; }
  button:disabled { opacity: .5; cursor: wait; }
  #btnDetectar { background: #e5e7eb; }
  #btnProcesar { background: #111; color: #fff; width: 100%; padding: .8rem;
                 font-size: 1.05rem; }
  #detectados { display: grid; gap: .3rem; margin-top: .7rem; }
  #detectados label { display: flex; align-items: center; gap: .5rem;
          padding: .35rem .5rem; background: #f9fafb; border-radius: 6px;
          font-size: .92rem; }
  .tipo { background: #111; color: #fff; border-radius: 4px; padding: .05rem .4rem;
          font-size: .75rem; white-space: nowrap; }
  #resumen { display: none; }
  #resumen ul { margin: .4rem 0 0; padding-left: 1.2rem; }
  #resumen li { margin: .15rem 0; font-size: .92rem; }
  li.aviso { color: #b45309; }
  li.alerta { color: #b91c1c; font-weight: 600; }
  .nota { color: #6b7280; font-size: .82rem; margin: .5rem 0 0; }
</style>
</head>
<body>
<main>
  <div>
    <h1><span>Tachón</span></h1>
    <p class="sub">Borra tus datos personales de PDFs e imágenes. Todo se procesa
    en tu equipo, sin salir a internet.</p>
  </div>

  <div id="zona">Arrastra aquí tu PDF o imagen<br>(o haz clic para elegirlo)</div>
  <input type="file" id="fichero" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp" hidden>

  <div class="tarjeta">
    <h2>¿Qué hay que borrar?</h2>
    <div class="campos">
      <input placeholder="DNI o NIE (12345678Z)">
      <input placeholder="Teléfono (612345678)">
      <input placeholder="Correo electrónico">
      <input placeholder="Nombre y apellidos">
      <input placeholder="Dirección (Calle Mayor 3)">
      <input placeholder="IBAN">
    </div>
    <textarea rows="2" id="otros"
      placeholder="Otros datos: uno por línea (re: para expresiones regulares)"></textarea>
    <p class="nota">No hace falta escribir cada formato: «612345678» también borra
    «612 34 56 78» o «+34 612 345 678».</p>
    <button id="btnDetectar">Detectar datos automáticamente</button>
    <div id="detectados"></div>
    <p class="nota">La detección automática es local y por patrones: DNI, NIE,
    teléfonos, correos, IBAN, tarjetas, sellos y hashes, fechas, códigos postales
    y localidades. Lo dudoso aparece desmarcado para que decidas tú. Nombres y
    direcciones escríbelos a mano.</p>
  </div>

  <button id="btnProcesar">Tachar y descargar</button>

  <div class="tarjeta" id="resumen"><h2>Resultado</h2><ul></ul></div>
</main>

<script>
"use strict";
let fichero = null;

const zona = document.getElementById("zona");
const inputFichero = document.getElementById("fichero");
const btnDetectar = document.getElementById("btnDetectar");
const btnProcesar = document.getElementById("btnProcesar");
const cajaDetectados = document.getElementById("detectados");
const cajaResumen = document.getElementById("resumen");

function ponFichero(f) {
  if (!f) return;
  fichero = f;
  zona.classList.add("conFichero");
  zona.textContent = "\\u{1F4C4} " + f.name;
  cajaDetectados.replaceChildren();
  cajaResumen.style.display = "none";
}

zona.addEventListener("click", () => inputFichero.click());
inputFichero.addEventListener("change", () => ponFichero(inputFichero.files[0]));
zona.addEventListener("dragover", e => { e.preventDefault(); zona.classList.add("encima"); });
zona.addEventListener("dragleave", () => zona.classList.remove("encima"));
zona.addEventListener("drop", e => {
  e.preventDefault();
  zona.classList.remove("encima");
  ponFichero(e.dataTransfer.files[0]);
});

btnDetectar.addEventListener("click", async () => {
  if (!fichero) { alert("Primero elige un fichero."); return; }
  btnDetectar.disabled = true;
  btnDetectar.textContent = "Leyendo el documento…";
  try {
    const fd = new FormData();
    fd.append("fichero", fichero);
    const r = await fetch("/detectar", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    cajaDetectados.replaceChildren();
    if (j.detectados.length === 0) {
      const p = document.createElement("p");
      p.className = "nota";
      p.textContent = "No se ha detectado ningún dato con formato conocido.";
      cajaDetectados.append(p);
    }
    for (const d of j.detectados) {
      const label = document.createElement("label");
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

btnProcesar.addEventListener("click", async () => {
  if (!fichero) { alert("Primero elige un fichero."); return; }
  const lineas = recogerDatos();
  if (lineas.length === 0) {
    alert("Indica al menos un dato a borrar (o usa la detección automática).");
    return;
  }
  btnProcesar.disabled = true;
  btnProcesar.textContent = "Tachando…";
  try {
    const fd = new FormData();
    fd.append("fichero", fichero);
    fd.append("datos", lineas.join("\\n"));
    const r = await fetch("/procesar", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    const lista = cajaResumen.querySelector("ul");
    lista.replaceChildren();
    for (const linea of j.resumen) {
      const li = document.createElement("li");
      li.textContent = linea;
      if (linea.startsWith("AVISO") || linea.startsWith("NOTA") ||
          linea.startsWith("SIN CAPA")) li.className = "aviso";
      if (linea.startsWith("ALERTA")) li.className = "alerta";
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
