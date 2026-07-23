#!/usr/bin/env python3
"""Pruebas de aceptación de Tachón.

Genera sus propios ficheros de prueba en pruebas/ y verifica los cinco
criterios de la especificación. Ejecutar con:  py probar_tachon.py
"""

import csv
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import fitz
from PIL import Image

from tachon import _configurar_tesseract, normalizar

RAIZ = Path(__file__).parent
PRUEBAS = RAIZ / "pruebas"
ENTRADA = PRUEBAS / "entrada"

DNI = "12345678Z"
TEL = "612345678"
EMAIL = "juan.perez@correo.com"
IBAN_REGEX = r"\bES\d{2}(?:[ -]?\d{4}){5}\b"
DATO_AUSENTE = "maria.lopez@no-esta.com"

LINEAS_DOC = [
    "Solicitud de ejemplo",
    "DNI: 12.345.678-Z",
    "Telefono fijo: 612 34 56 78",
    "Movil internacional: +34 612 345 678",
    "Otro contacto: (612345678)",
    "Correo: juan.perez@correo.com",
    "IBAN: ES12 3456 7890 1234 5678 9012",
]
# Variante "de escáner" con un 5 leído como S: solo la caza la pasada aproximada.
LINEA_CORROMPIDA = "Copia del DNI: 1234S678-Z"

VARIANTES_LITERALES = [
    "12.345.678-Z",
    "612 34 56 78",
    "+34 612 345 678",
    "(612345678)",
    "juan.perez@correo.com",
    "ES12 3456 7890 1234 5678 9012",
]

fallos: list[str] = []


def comprobar(nombre: str, condicion: bool, detalle: str = "") -> None:
    if condicion:
        print(f"  [OK]    {nombre}")
    else:
        print(f"  [FALLO] {nombre}" + (f" — {detalle}" if detalle else ""))
        fallos.append(nombre)


def pagina_con_texto(doc: fitz.Document, lineas: list[str]) -> fitz.Page:
    page = doc.new_page()
    y = 80
    for linea in lineas:
        page.insert_text((72, y), linea, fontsize=12)
        y += 26
    return page


def crear_ficheros() -> None:
    if PRUEBAS.exists():
        shutil.rmtree(PRUEBAS)
    ENTRADA.mkdir(parents=True)

    # 1) PDF con capa de texto.
    doc = fitz.open()
    pagina_con_texto(doc, LINEAS_DOC)
    doc.set_metadata({"author": "Autor de Prueba", "title": "Documento con datos"})
    doc.save(ENTRADA / "texto.pdf")
    doc.close()

    # 2) El mismo contenido (más la línea corrompida) rasterizado dentro de un PDF.
    src = fitz.open()
    page = pagina_con_texto(src, LINEAS_DOC + [LINEA_CORROMPIDA])
    pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))
    dst = fitz.open()
    p2 = dst.new_page(width=page.rect.width, height=page.rect.height)
    p2.insert_image(p2.rect, pixmap=pix)
    dst.save(ENTRADA / "escaneado.pdf")
    dst.close()

    # 3) Un PNG suelto con el mismo contenido.
    pix.save(ENTRADA / "suelto.png")
    src.close()

    # Fichero de datos: incluye un comentario y un dato que no aparece (criterio 4).
    (PRUEBAS / "datos.txt").write_text(
        "# datos de prueba\n"
        f"{DNI}\n{TEL}\n{EMAIL}\nre:{IBAN_REGEX}\n{DATO_AUSENTE}\n",
        encoding="utf-8",
    )


def ejecutar_tachon(entrada: Path, salida: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(RAIZ / "tachon.py"), str(entrada),
         "--datos", str(PRUEBAS / "datos.txt"), "--salida", str(salida)],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )


def texto_sin_datos(nombre: str, crudo: str) -> None:
    norm = normalizar(crudo)
    comprobar(f"{nombre}: sin DNI", normalizar(DNI) not in norm)
    comprobar(f"{nombre}: sin teléfono", normalizar(TEL) not in norm)
    comprobar(f"{nombre}: sin email", normalizar(EMAIL) not in norm)
    comprobar(f"{nombre}: sin IBAN", not re.search(IBAN_REGEX, crudo))


def ocr_de_pdf(ruta: Path) -> str:
    import pytesseract

    doc = fitz.open(ruta)
    trozos = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72))
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        trozos.append(pytesseract.image_to_string(img, lang="spa+eng"))
    doc.close()
    return "\n".join(trozos)


def caso_1_pdf_texto() -> None:
    print("\nCriterios 1, 4 y 5 — PDF con capa de texto")
    salida = PRUEBAS / "salida1"
    r = ejecutar_tachon(ENTRADA / "texto.pdf", salida)
    destino = salida / "texto_filtrado.pdf"
    comprobar("la ejecución termina sin error", r.returncode == 0,
              f"código {r.returncode}\n{r.stdout}\n{r.stderr}")
    comprobar("existe el fichero de salida", destino.exists())
    if not destino.exists():
        return

    doc = fitz.open(destino)
    crudo = "\n".join(page.get_text() for page in doc)
    meta = doc.metadata
    xml = doc.get_xml_metadata()
    doc.close()

    # Criterio 1: los cuatro datos fuera, en todas sus variantes.
    texto_sin_datos("texto_filtrado.pdf", crudo)
    for variante in VARIANTES_LITERALES:
        comprobar(f"variante literal ausente: «{variante}»", variante not in crudo)

    # Criterio 4: aviso de dato sin coincidencias, sin romper la ejecución.
    comprobar("aviso de datos sin coincidencias", "NO aparecieron" in r.stdout, r.stdout)
    comprobar("el aviso menciona el dato ausente", DATO_AUSENTE in r.stdout)

    # Criterio 5: metadatos vacíos.
    campos = ["author", "title", "subject", "keywords", "creator", "producer",
              "creationDate", "modDate"]
    sucios = [c for c in campos if meta.get(c)]
    comprobar("metadatos del PDF vacíos", not sucios, f"con valor: {sucios} → {meta}")
    comprobar("sin metadatos XML", not xml, xml[:200] if xml else "")

    comprobar("el informe.csv existe", (salida / "informe.csv").exists())


def caso_2_pdf_escaneado() -> None:
    print("\nCriterio 2 — PDF escaneado (OCR + pasada aproximada)")
    salida = PRUEBAS / "salida2"
    r = ejecutar_tachon(ENTRADA / "escaneado.pdf", salida)
    destino = salida / "escaneado_filtrado.pdf"
    comprobar("la ejecución termina sin error", r.returncode == 0,
              f"código {r.returncode}\n{r.stdout}\n{r.stderr}")
    comprobar("existe el fichero de salida", destino.exists())
    if not destino.exists():
        return

    comprobar("la página se detecta como escaneada (modo OCR)", "modo: OCR" in r.stdout,
              r.stdout)
    comprobar("se avisa de que no hay capa de texto que verificar",
              "SIN CAPA DE TEXTO" in r.stdout, r.stdout)

    # La pasada aproximada debe haberse activado (línea corrompida 1234S678-Z).
    aprox_total = 0
    with open(salida / "informe.csv", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            if fila["aproximadas"]:
                aprox_total += int(fila["aproximadas"])
    comprobar("la pasada aproximada se activó", aprox_total >= 1,
              f"aproximadas en informe.csv = {aprox_total}\n{r.stdout}")

    # Verificación real: OCR del resultado y comprobar que los datos ya no se leen.
    texto_sin_datos("escaneado_filtrado.pdf (OCR)", ocr_de_pdf(destino))

    # Criterio 5 también aplica aquí.
    doc = fitz.open(destino)
    meta = doc.metadata
    doc.close()
    campos = ["author", "title", "subject", "keywords", "creator", "producer",
              "creationDate", "modDate"]
    sucios = [c for c in campos if meta.get(c)]
    comprobar("metadatos del PDF escaneado vacíos", not sucios, str(meta))


def caso_3_png() -> None:
    import pytesseract

    print("\nCriterio 3 — PNG suelto")
    salida = PRUEBAS / "salida3"
    r = ejecutar_tachon(ENTRADA / "suelto.png", salida)
    destino = salida / "suelto_filtrado.png"
    comprobar("la ejecución termina sin error", r.returncode == 0,
              f"código {r.returncode}\n{r.stdout}\n{r.stderr}")
    comprobar("existe el fichero de salida", destino.exists())
    if not destino.exists():
        return

    img = Image.open(destino)
    texto_sin_datos("suelto_filtrado.png (OCR)", pytesseract.image_to_string(img, lang="spa+eng"))
    comprobar("la imagen de salida no lleva EXIF", len(img.getexif()) == 0)


def caso_deteccion() -> None:
    from app import detectar_datos

    print("\nDetección automática (patrones locales, sin IA)")
    texto = (
        "DNI: 12.345.678-Z y NIE X-1234567-L\n"
        "Tel: +34 612 345 678, correo juan.perez@correo.com\n"
        "Nacido el 3 de enero de 1990, alta el 12/05/2024\n"
        "CP 28013, Madrid\n"
        "Sello de operación: 2026010213015602521006663009"
        "1.245.204.95e4B-lapuu2mF6T45fSNKGEb_UxdEI8WaW2700\n"
        "Sello de firma: 20260102130825577302000003864966"
        "9202100666300MOINaGxWlHmDHykwN8=\n"
    )
    detectados = detectar_datos(texto)
    tipos = {d["tipo"] for d in detectados}
    for esperado in ["DNI", "NIE", "Teléfono", "Correo", "Fecha",
                     "Cód. postal", "Localidad", "Sello/hash"]:
        comprobar(f"detecta {esperado}", esperado in tipos, str(sorted(tipos)))
    sellos = [d["valor"] for d in detectados if d["tipo"] == "Sello/hash"]
    comprobar("captura los dos sellos enteros",
              len(sellos) == 2 and all(len(s) > 40 for s in sellos), str(sellos))
    fechas = [d["valor"] for d in detectados if d["tipo"] == "Fecha"]
    comprobar("captura ambas fechas (texto y numérica)", len(fechas) == 2, str(fechas))
    comprobar("lo dudoso sale desmarcado (fiable=False)",
              all(not d["fiable"] for d in detectados
                  if d["tipo"] in ("Fecha", "Cód. postal", "Localidad")))


def main() -> int:
    _configurar_tesseract()
    print("Generando ficheros de prueba en pruebas/ ...")
    crear_ficheros()

    caso_1_pdf_texto()
    caso_2_pdf_escaneado()
    caso_3_png()
    caso_deteccion()

    print()
    if fallos:
        print(f"RESULTADO: {len(fallos)} comprobación(es) fallida(s). "
              "Se conserva pruebas/ para inspección:")
        for f in fallos:
            print(f"  · {f}")
        return 1
    shutil.rmtree(PRUEBAS)  # todo en verde: no dejamos residuos
    print("RESULTADO: todas las comprobaciones pasan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
