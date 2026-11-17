#!/usr/bin/env python3
"""Tachón — borra datos personales concretos de PDFs e imágenes.

No detecta nada por su cuenta: recibe la lista de valores a eliminar
(un dato por línea, `#` comenta, prefijo `re:` para expresiones regulares)
y los busca y borra de verdad (redacción real, no una caja encima).

Uso:
    tachon <fichero-o-carpeta> --datos mis_datos.txt --salida limpio/
"""

import argparse
import csv
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # pymupdf
from PIL import Image, ImageDraw
from rapidfuzz import fuzz

EXT_IMAGEN = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
EXT_PDF = {".pdf"}

# Separadores que se eliminan al normalizar: espacio (también duro y finos),
# tabulador, punto, guion, guion bajo, barra, barra invertida, paréntesis,
# corchetes, llaves, dos puntos, coma, punto y coma, guiones largos y el «+»
# de los prefijos telefónicos.
_SEPARADORES = " \t\u00a0\u2007\u202f.-_/\\()[]{}:,;\u2013\u2014+"
_TABLA_SEP = str.maketrans("", "", _SEPARADORES)


def normalizar(texto: str) -> str:
    """Minúsculas, sin acentos (NFKD descartando combinantes) y sin separadores."""
    texto = texto.lower()
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.translate(_TABLA_SEP)


def _configurar_tesseract() -> None:
    """En Windows, Tesseract no siempre está en el PATH: se busca a mano."""
    import pytesseract

    if shutil.which("tesseract"):
        return
    for candidata in (
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        Path.home() / r"AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
    ):
        if candidata.exists():
            pytesseract.pytesseract.tesseract_cmd = str(candidata)
            return
    # Si tampoco está ahí, pytesseract dará su propio error al usarse.


@dataclass
class Dato:
    """Un valor a eliminar: literal (normalizado) o expresión regular."""

    original: str
    es_regex: bool
    normalizado: str = ""
    patron: "re.Pattern | None" = None
    coincidencias: int = 0
    aproximadas: int = 0

    @property
    def etiqueta(self) -> str:
        return ("re:" + self.original) if self.es_regex else self.original

    def coincide_en_texto(self, texto: str) -> bool:
        if not texto:
            return False
        if self.es_regex:
            return bool(self.patron.search(texto))
        return self.normalizado in normalizar(texto)


def datos_desde_lineas(lineas, avisar=print) -> list[Dato]:
    """Convierte líneas de texto en Datos. Avisa y descarta los demasiado cortos."""
    datos: list[Dato] = []
    for n_linea, linea in enumerate(lineas, 1):
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if linea.startswith("re:"):
            expr = linea[3:]
            try:
                patron = re.compile(expr, re.IGNORECASE)
            except re.error as e:
                avisar(f"  AVISO: línea {n_linea}: regex inválida ({e}). Se descarta: {expr}")
                continue
            datos.append(Dato(original=expr, es_regex=True, patron=patron))
        else:
            norm = normalizar(linea)
            if len(norm) < 4:
                avisar(
                    f"  AVISO: línea {n_linea}: «{linea}» queda en menos de 4 caracteres "
                    "tras normalizar; borrar cadenas tan cortas produce destrozos. Se descarta."
                )
                continue
            datos.append(Dato(original=linea, es_regex=False, normalizado=norm))
    return datos


def cargar_datos(ruta: Path) -> list[Dato]:
    """Lee el fichero de datos."""
    return datos_desde_lineas(ruta.read_text(encoding="utf-8").splitlines())


@dataclass
class Linea:
    """Una línea de palabras con coordenadas y sus cadenas indexadas.

    `crudo`/`mapa_crudo` sirven para las regex (texto tal cual, palabras unidas
    por un espacio); `norm`/`mapa_norm` para la búsqueda literal normalizada.
    Cada mapa lleva, por posición de carácter, el índice de la palabra de la
    que procede ese carácter (-1 en los espacios de unión del texto crudo).
    """

    bloque: tuple = ()
    es_compuesta: bool = False
    corte_palabras: int = 0
    palabras: list = field(default_factory=list)
    rects: list = field(default_factory=list)
    crudo: str = ""
    mapa_crudo: list = field(default_factory=list)
    norm: str = ""
    mapa_norm: list = field(default_factory=list)

    def anadir_palabra(self, texto: str, rect: fitz.Rect) -> None:
        idx = len(self.rects)
        self.palabras.append((texto, rect))
        self.rects.append(rect)
        if self.crudo:
            self.crudo += " "
            self.mapa_crudo.append(-1)
        self.crudo += texto
        self.mapa_crudo.extend([idx] * len(texto))
        norm_pal = normalizar(texto)
        self.norm += norm_pal
        self.mapa_norm.extend([idx] * len(norm_pal))

    def indices_entre(self, mapa: list, ini: int, fin: int) -> list[int]:
        return sorted(
            {mapa[i] for i in range(ini, fin) if 0 <= i < len(mapa) and mapa[i] >= 0}
        )

    def rects_para_indices(self, indices: list[int]) -> list:
        if self.es_compuesta:
            # Solo aceptar coincidencias que crucen de la primera línea a la segunda.
            tiene_primera = any(i < self.corte_palabras for i in indices)
            tiene_segunda = any(i >= self.corte_palabras for i in indices)
            if not (tiene_primera and tiene_segunda):
                return []
        return [self.rects[i] for i in indices]

    def rects_entre(self, mapa: list, ini: int, fin: int) -> list:
        return self.rects_para_indices(self.indices_entre(mapa, ini, fin))


def _anadir_pares_multilinea(lineas_base: list[Linea]) -> list[Linea]:
    """Añade pares de líneas consecutivas dentro del mismo bloque de texto."""
    resultado = list(lineas_base)
    for l1, l2 in zip(lineas_base, lineas_base[1:]):
        if l1.bloque != l2.bloque:
            continue
        par = Linea(
            bloque=l1.bloque,
            es_compuesta=True,
            corte_palabras=len(l1.rects),
        )
        for texto, rect in l1.palabras:
            par.anadir_palabra(texto, rect)
        for texto, rect in l2.palabras:
            par.anadir_palabra(texto, rect)
        resultado.append(par)
    return resultado


def lineas_desde_words(words: list) -> list[Linea]:
    """Agrupa la salida de page.get_text('words') en líneas por (block, line)."""
    grupos: dict[tuple, Linea] = {}
    for x0, y0, x1, y1, texto, n_block, n_line, _n_word in words:
        clave = (n_block, n_line)
        linea = grupos.setdefault(clave, Linea(bloque=(n_block,)))
        linea.anadir_palabra(texto, fitz.Rect(x0, y0, x1, y1))
    return _anadir_pares_multilinea(list(grupos.values()))


def lineas_desde_ocr(datos_ocr: dict, zoom: float) -> list[Linea]:
    """Agrupa la salida de pytesseract.image_to_data en líneas.

    Ojo: Tesseract reinicia line_num en cada párrafo, así que la clave de
    agrupación DEBE incluir par_num o se mezclan líneas sin relación alguna.
    Las coordenadas de píxel se convierten a puntos PDF dividiendo por el zoom.
    """
    grupos: dict[tuple, Linea] = {}
    for i in range(len(datos_ocr["text"])):
        texto = datos_ocr["text"][i].strip()
        if not texto or int(datos_ocr["conf"][i]) < 0:
            continue
        clave = (datos_ocr["block_num"][i], datos_ocr["par_num"][i], datos_ocr["line_num"][i])
        bloque = (datos_ocr["block_num"][i], datos_ocr["par_num"][i])
        x, y = datos_ocr["left"][i], datos_ocr["top"][i]
        w, h = datos_ocr["width"][i], datos_ocr["height"][i]
        rect = fitz.Rect(x / zoom, y / zoom, (x + w) / zoom, (y + h) / zoom)
        linea = grupos.setdefault(clave, Linea(bloque=bloque))
        linea.anadir_palabra(texto, rect)
    return _anadir_pares_multilinea(list(grupos.values()))


@dataclass
class Coincidencia:
    rects: list
    dato: Dato
    aproximada: bool = False


def buscar_en_lineas(
    lineas: list[Linea],
    datos: list[Dato],
    con_fuzzy: bool,
    corte_fuzzy: float,
) -> list[Coincidencia]:
    """Busca todos los datos en las líneas. Fuzzy solo sobre líneas simples con OCR."""
    resultado: list[Coincidencia] = []
    for linea in lineas:
        for dato in datos:
            if dato.es_regex:
                for m in dato.patron.finditer(linea.crudo):
                    rects = linea.rects_entre(linea.mapa_crudo, m.start(), m.end())
                    if rects:
                        dato.coincidencias += 1
                        resultado.append(Coincidencia(rects, dato))
                continue

            encontrado_exacto = False
            inicio = 0
            while True:
                pos = linea.norm.find(dato.normalizado, inicio)
                if pos < 0:
                    break
                encontrado_exacto = True
                rects = linea.rects_entre(linea.mapa_norm, pos, pos + len(dato.normalizado))
                if rects:
                    dato.coincidencias += 1
                    resultado.append(Coincidencia(rects, dato))
                inicio = pos + len(dato.normalizado)

            # Pasada aproximada: solo sobre líneas individuales (no compuestas) y
            # cuando la línea es al menos tan larga como el dato.
            if (
                con_fuzzy
                and not linea.es_compuesta
                and not encontrado_exacto
                and len(linea.norm) >= len(dato.normalizado)
            ):
                al = fuzz.partial_ratio_alignment(
                    dato.normalizado, linea.norm, score_cutoff=corte_fuzzy
                )
                if al is not None and al.dest_end > al.dest_start:
                    rects = linea.rects_entre(linea.mapa_norm, al.dest_start, al.dest_end)
                    if rects:
                        dato.coincidencias += 1
                        dato.aproximadas += 1
                        resultado.append(Coincidencia(rects, dato, aproximada=True))
    return resultado


def ocr_pagina(page: fitz.Page, dpi: int, idioma: str) -> list[Linea]:
    import pytesseract

    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    datos_ocr = pytesseract.image_to_data(img, lang=idioma, output_type=pytesseract.Output.DICT)
    return lineas_desde_ocr(datos_ocr, zoom)


def _limpiar_enlaces_pagina(
    page: fitz.Page,
    rectangulos_tachados: list[fitz.Rect],
    datos: list[Dato],
) -> int:
    """Elimina enlaces (mailto:, tel:, URIs) que solapen o contengan datos sensibles."""
    eliminados = 0
    for link in page.get_links():
        uri = link.get("uri") or ""
        link_rect = link.get("from")
        solapa = bool(link_rect and any(r.intersects(link_rect) for r in rectangulos_tachados))
        datos_en_uri = [d for d in datos if d.coincide_en_texto(uri)]
        if solapa or datos_en_uri:
            page.delete_link(link)
            eliminados += 1
            if not solapa:
                for d in datos_en_uri:
                    d.coincidencias += 1
    return eliminados


def _limpiar_toc(doc: fitz.Document, datos: list[Dato]) -> int:
    """Redacta entradas de la tabla de contenidos (marcadores) con datos sensibles."""
    toc = doc.get_toc(simple=False)
    if not toc:
        return 0
    modificados = 0
    nuevo_toc = []
    for entrada in toc:
        titulo = str(entrada[1]) if len(entrada) > 1 else ""
        coinciden = [d for d in datos if d.coincide_en_texto(titulo)]
        if coinciden:
            entrada = list(entrada)
            entrada[1] = "[REDACTADO]"
            modificados += 1
            for d in coinciden:
                d.coincidencias += 1
        nuevo_toc.append(entrada)
    if modificados:
        doc.set_toc(nuevo_toc)
    return modificados


def procesar_pdf(entrada: Path, salida: Path, datos: list[Dato], args) -> dict:
    doc = fitz.open(entrada)
    modos = set()
    n_coincidencias = 0
    n_aproximadas = 0
    n_enlaces_borrados = 0

    for page in doc:
        words = page.get_text("words")
        n_chars = sum(len(w[4]) for w in words)
        escaneada = n_chars < args.umbral_texto

        lineas: list[Linea] = []
        usa_ocr = False
        if not escaneada:
            lineas.extend(lineas_desde_words(words))
            modos.add("texto")
        elif args.sin_ocr:
            print(f"    página {page.number + 1}: parece escaneada, saltada (--sin-ocr)")
        if (escaneada and not args.sin_ocr) or args.forzar_ocr:
            lineas.extend(ocr_pagina(page, args.dpi, args.idioma))
            modos.add("OCR")
            usa_ocr = True

        coincidencias = buscar_en_lineas(lineas, datos, usa_ocr, args.corte_fuzzy)
        rectangulos_pagina: list[fitz.Rect] = []
        if coincidencias:
            for c in coincidencias:
                n_coincidencias += 1
                if c.aproximada:
                    n_aproximadas += 1
                for rect in c.rects:
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    rectangulos_pagina.append(rect)
            # Redacción real: elimina el texto y los píxeles de imagen debajo.
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

        n_enlaces_borrados += _limpiar_enlaces_pagina(page, rectangulos_pagina, datos)

    _limpiar_toc(doc, datos)
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.save(salida, garbage=4, deflate=True, clean=True)
    doc.close()

    return {
        "modo": "+".join(sorted(modos)) if modos else "vacío",
        "coincidencias": n_coincidencias,
        "aproximadas": n_aproximadas,
        "enlaces_borrados": n_enlaces_borrados,
    }


def procesar_imagen(entrada: Path, salida: Path, datos: list[Dato], args) -> dict:
    import pytesseract

    img = Image.open(entrada).convert("RGB")
    datos_ocr = pytesseract.image_to_data(img, lang=args.idioma, output_type=pytesseract.Output.DICT)
    lineas = lineas_desde_ocr(datos_ocr, zoom=1.0)  # coordenadas ya en píxeles
    coincidencias = buscar_en_lineas(lineas, datos, True, args.corte_fuzzy)

    dibujo = ImageDraw.Draw(img)
    n_aprox = 0
    for c in coincidencias:
        if c.aproximada:
            n_aprox += 1
        for r in c.rects:
            dibujo.rectangle([r.x0, r.y0, r.x1, r.y1], fill=(0, 0, 0))

    # Imagen recreada desde cero: los píxeles tapados no existen y no hay EXIF.
    limpia = Image.new("RGB", img.size)
    limpia.paste(img)
    limpia.save(salida)
    return {"modo": "OCR", "coincidencias": len(coincidencias), "aproximadas": n_aprox}


def verificar_pdf(salida: Path, datos: list[Dato]) -> list[str]:
    """Reabre el PDF y comprueba que ningún dato sigue en texto, enlaces ni TOC."""
    alertas: list[str] = []
    doc = fitz.open(salida)
    texto_total = "\n".join(page.get_text() for page in doc)
    uris_total = [link.get("uri") or "" for page in doc for link in page.get_links()]
    toc_titulos = [str(e[1]) for e in doc.get_toc(simple=True) if len(e) > 1]
    doc.close()

    for uri in uris_total:
        for dato in datos:
            if dato.coincide_en_texto(uri):
                alertas.append(f"ALERTA: «{dato.etiqueta}» sigue en un enlace del PDF ({uri})")

    for titulo in toc_titulos:
        for dato in datos:
            if dato.coincide_en_texto(titulo):
                alertas.append(f"ALERTA: «{dato.etiqueta}» sigue en un marcador del PDF")

    if not texto_total.strip():
        alertas.append(
            "SIN CAPA DE TEXTO: la verificación no puede confirmar nada sobre este "
            "fichero (probablemente era escaneado). Revísalo a ojo."
        )
        return alertas

    norm_total = normalizar(texto_total)
    for dato in datos:
        if dato.es_regex:
            if dato.patron.search(texto_total):
                alertas.append(
                    f"ALERTA: la regex «{dato.original}» sigue encontrando texto en la salida"
                )
        elif dato.normalizado in norm_total:
            alertas.append(f"ALERTA: «{dato.original}» sigue apareciendo en la salida")
    return alertas


def recopilar_ficheros(ruta: Path) -> list[Path]:
    if ruta.is_file():
        return [ruta]
    exts = EXT_PDF | EXT_IMAGEN
    return sorted(p for p in ruta.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tachon",
        description="Borra datos personales concretos de PDFs e imágenes (redacción real).",
    )
    parser.add_argument("entrada", type=Path, help="fichero o carpeta a procesar")
    parser.add_argument("--datos", type=Path, required=True,
                        help="fichero de texto con un dato por línea (re: para regex, # comenta)")
    parser.add_argument("--salida", type=Path, default=Path("limpio"),
                        help="carpeta de salida (por defecto: limpio/)")
    parser.add_argument("--umbral-texto", type=int, default=40,
                        help="caracteres mínimos para no tratar la página como escaneada (40)")
    parser.add_argument("--dpi", type=int, default=300, help="resolución del OCR (300)")
    parser.add_argument("--idioma", default="spa+eng", help="idiomas de Tesseract (spa+eng)")
    parser.add_argument("--corte-fuzzy", type=float, default=88,
                        help="score_cutoff de la pasada aproximada sobre OCR (88)")
    grupo_ocr = parser.add_mutually_exclusive_group()
    grupo_ocr.add_argument("--forzar-ocr", action="store_true",
                           help="pasar OCR también a páginas con texto (datos dentro de imágenes)")
    grupo_ocr.add_argument("--sin-ocr", action="store_true",
                           help="saltarse las páginas escaneadas")
    args = parser.parse_args(argv)

    if not args.entrada.exists():
        print(f"ERROR: no existe {args.entrada}")
        return 2
    if not args.datos.exists():
        print(f"ERROR: no existe el fichero de datos {args.datos}")
        return 2

    _configurar_tesseract()

    print(f"Leyendo datos de {args.datos}:")
    datos = cargar_datos(args.datos)
    if not datos:
        print("ERROR: la lista de datos quedó vacía; nada que hacer.")
        return 2
    print(f"  {len(datos)} dato(s) a eliminar.")

    ficheros = recopilar_ficheros(args.entrada)
    if not ficheros:
        print("ERROR: no se encontraron PDFs ni imágenes en la entrada.")
        return 2

    args.salida.mkdir(parents=True, exist_ok=True)
    filas_csv = []
    hubo_alerta = False

    for fichero in ficheros:
        destino = args.salida / f"{fichero.stem}_filtrado{fichero.suffix}"
        if destino.resolve() == fichero.resolve():
            print(f"\n{fichero.name}: saltado para no sobrescribir la entrada.")
            continue

        # Contadores por fichero.
        for d in datos:
            d.coincidencias = 0
            d.aproximadas = 0

        print(f"\n{fichero.name}")
        try:
            if fichero.suffix.lower() in EXT_PDF:
                info = procesar_pdf(fichero, destino, datos, args)
                alertas = verificar_pdf(destino, datos)
            else:
                info = procesar_imagen(fichero, destino, datos, args)
                alertas = ["NOTA: en imágenes no hay capa de texto que verificar; "
                           "revisa el resultado a ojo."]
        except Exception as e:
            print(f"  ERROR procesando el fichero: {e}")
            filas_csv.append([fichero.name, "ERROR", "", "", "", str(e)])
            hubo_alerta = True
            continue

        print(f"  modo: {info['modo']}")
        for d in datos:
            extra = f" ({d.aproximadas} aproximada(s), revísalas)" if d.aproximadas else ""
            print(f"  «{d.etiqueta}»: {d.coincidencias} coincidencia(s){extra}")
            filas_csv.append([fichero.name, info["modo"], d.etiqueta,
                              d.coincidencias, d.aproximadas, ""])

        sin_uso = [d for d in datos if d.coincidencias == 0]
        if sin_uso:
            print("  AVISO — estos datos NO aparecieron ni una vez (¿formato sin cubrir?):")
            for d in sin_uso:
                print(f"    · {d.etiqueta}")

        for alerta in alertas:
            print(f"  {alerta}")
            filas_csv.append([fichero.name, info["modo"], "", "", "", alerta])
            if alerta.startswith("ALERTA"):
                hubo_alerta = True

        print(f"  guardado en {destino}")

    ruta_csv = args.salida / "informe.csv"
    with open(ruta_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["fichero", "modo", "dato", "coincidencias", "aproximadas", "nota"])
        w.writerows(filas_csv)
    print(f"\nInforme escrito en {ruta_csv}")

    return 1 if hubo_alerta else 0


if __name__ == "__main__":
    sys.exit(main())
