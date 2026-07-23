# Tachón

Herramienta de línea de comandos que **borra datos personales concretos** de PDFs e imágenes. No detecta nada por su cuenta: tú le pasas la lista de tus datos (DNI, teléfono, email, dirección…) y ella los busca y los elimina de verdad — redacción real con PyMuPDF, no una caja negra encima del texto.

Funciona **100 % sin red**: nada se descarga ni se envía a ninguna parte.

## Requisitos

- Python 3.10+ con: `pip install pymupdf pytesseract pillow rapidfuzz`
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) instalado como binario del sistema (solo necesario para escaneados e imágenes). En Windows, si no está en el PATH, Tachón lo busca en las rutas de instalación habituales.

## Interfaz web (recomendada)

```
py app.py
```

Abre `http://127.0.0.1:5000` en el navegador:

1. **Arrastra** tu PDF o imagen a la zona de subida (o haz clic para elegirlo).
2. Rellena los campos de lo que hay que borrar (DNI, teléfono, correo…), o pulsa **«Detectar datos automáticamente»**: la herramienta lee el documento en local y te propone los datos con formato conocido que encuentre — DNI y NIE (validando la letra), teléfonos, correos, IBAN (módulo 97), tarjetas (Luhn), **sellos de operación/firma y hashes** (cadenas largas con muchos dígitos), **fechas** (numéricas o «3 de enero de 1990»), **códigos postales** y **localidades** habituales. Los tipos con validación fuerte salen marcados; los dudosos (fechas, CP, localidades) salen desmarcados para que decidas tú. Nombres y direcciones no se pueden detectar por patrón: escríbelos a mano.
3. Pulsa **«Tachar y descargar»**: se procesa y el fichero limpio se descarga solo.

El servidor solo escucha en tu propio equipo (`127.0.0.1`) y no sale a internet: la detección es por patrones, sin IA ni servicios externos.

Para instalar la interfaz hace falta además: `pip install flask`

## Uso por terminal

```
py tachon.py <fichero-o-carpeta> --datos mis_datos.txt --salida limpio/
```

Los resultados se guardan como `<nombre>_filtrado.<extensión>` en la carpeta de salida, junto a un `informe.csv`. La entrada nunca se sobrescribe.

### Fichero de datos

Un dato por línea. `#` comenta. El prefijo `re:` marca la línea como expresión regular (se aplica sobre el texto crudo).

```
12345678Z
612345678
juan.perez@correo.com
Calle Mayor 3
re:\bES\d{2}(?:[ -]?\d{4}){5}\b
```

No hace falta poner cada formato del mismo dato: `612345678` también borra `612 34 56 78`, `+34 612 345 678` o `612-345-678`, porque ambos lados se normalizan (minúsculas, sin acentos, sin separadores) antes de comparar.

### Opciones

| Opción | Por defecto | Qué hace |
|---|---|---|
| `--salida` | `limpio/` | carpeta de resultados |
| `--umbral-texto` | 40 | caracteres mínimos para no tratar la página como escaneada |
| `--dpi` | 300 | resolución al rasterizar para OCR |
| `--idioma` | `spa+eng` | idiomas de Tesseract |
| `--corte-fuzzy` | 88 | umbral de la pasada aproximada sobre páginas OCR |
| `--forzar-ocr` | — | OCR también en páginas con texto (datos dentro de imágenes incrustadas) |
| `--sin-ocr` | — | saltarse las páginas escaneadas |

## Qué leer en la salida

- **AVISO de datos sin coincidencias**: el dato está en un formato que no se cubrió (o no aparece). Es la línea más importante del informe.
- **Coincidencias aproximadas**: encontradas por tolerancia a errores de OCR; revísalas a ojo.
- **ALERTA**: un dato sigue en el fichero de salida tras la verificación. Fallo grave.
- **SIN CAPA DE TEXTO**: la verificación automática no puede confirmar nada (escaneados e imágenes); revisa el resultado a ojo.

Además del texto, se limpian siempre los metadatos del PDF y el EXIF de las imágenes.

## Pruebas

```
py probar_tachon.py
```

Genera sus propios ficheros en `pruebas/` y verifica los cinco criterios de aceptación (PDF con texto, PDF escaneado con pasada aproximada, PNG suelto, aviso de dato ausente y metadatos vacíos).
