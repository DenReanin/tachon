#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

TACHON_PORT="${TACHON_PORT:-5000}"
export TACHON_PORT

if [ ! -x ".venv/bin/python" ]; then
  echo "[Tachón] Creando entorno virtual .venv..."
  python3 -m venv .venv
  echo "[Tachón] Instalando dependencias desde requirements.txt..."
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt
fi

if [ "${1:-}" = "--setup-only" ]; then
  echo "[Tachón] Entorno .venv listo."
  exit 0
fi

if ! command -v tesseract >/dev/null 2>&1; then
  echo "[AVISO] Tesseract OCR no está instalado en PATH (necesario solo para PDFs escaneados o imágenes)."
fi

# Abrir el navegador automáticamente cuando Flask empiece a escuchar
(
  for _ in $(seq 1 30); do
    if ./.venv/bin/python -c "import os, socket; s=socket.socket(); s.settimeout(0.2); s.connect(('127.0.0.1', int(os.environ.get('TACHON_PORT', '5000')))); s.close()" 2>/dev/null; then
      if command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        xdg-open "http://127.0.0.1:${TACHON_PORT}" >/dev/null 2>&1 || true
      fi
      break
    fi
    sleep 0.5
  done
) >/dev/null 2>&1 &

exec ./.venv/bin/python -u app.py
