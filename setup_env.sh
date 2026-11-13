#!/usr/bin/env bash
set -euo pipefail

# Install lightweight core dependencies (PP-OCRv4 ONNX + Pillow + Requests)
pip install --upgrade pip
pip install -r requirements.txt

# Install SimpleLama neural inpainter automatically if a GPU is present (e.g. Google Colab) or --with-lama is passed
if [[ "${1:-}" == "--with-lama" ]] || command -v nvidia-smi >/dev/null 2>&1; then
    pip install simple-lama-inpainting || echo "[Info] simple-lama-inpainting skipped; OpenCV multi-scale inpainter remains active."
fi

# Ensure headless OpenCV replaces GUI opencv-python pulled transitively by rapidocr-onnxruntime
pip uninstall -y opencv-python opencv-python-headless || true
pip install --no-cache-dir opencv-python-headless
