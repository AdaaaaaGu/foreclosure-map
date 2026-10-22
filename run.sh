#!/bin/bash
# Start the foreclosure map: ./run.sh  (then open http://localhost:8765)
cd "$(dirname "$0")"
if [ ! -x ocr/pdf_ocr ] || [ ocr/pdf_ocr.swift -nt ocr/pdf_ocr ]; then
  echo "Compiling OCR helper (one time)…"
  swiftc -O ocr/pdf_ocr.swift -o ocr/pdf_ocr || exit 1
fi
PORT="${1:-8765}"
( sleep 1; open "http://localhost:$PORT" ) &
exec python3 server.py "$PORT"
