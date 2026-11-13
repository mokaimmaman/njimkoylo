#!/usr/bin/env bash
set -euo pipefail

# Install core dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Optional GPU/CPU neural inpainter (SimpleLama) when running with full internet access
pip install simple-lama-inpainting || echo "[Info] simple-lama-inpainting skipped; OpenCV multi-scale inpainter remains active."

# Ensure headless OpenCV replaces GUI opencv-python pulled transitively by rapidocr-onnxruntime
pip uninstall -y opencv-python opencv-python-headless || true
pip install --no-cache-dir opencv-python-headless
