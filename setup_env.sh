#!/usr/bin/env bash
set -euo pipefail

# 1. Install lightweight core dependencies (PP-OCRv4 ONNX + Pillow + Requests + NumPy + tqdm)
pip install --upgrade pip
pip install -r requirements.txt

# 2. Install SimpleLama neural inpainter (uses pre-installed CUDA PyTorch on Colab, or lightweight CPU PyTorch on GitHub Actions)
if ! python3 -c "import torch" >/dev/null 2>&1; then
    if command -v nvidia-smi >/dev/null 2>&1; then
        pip install torch torchvision
    else
        pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    fi
fi
pip install simple-lama-inpainting || echo "[Info] simple-lama-inpainting skipped; OpenCV fallback remains active."

# 3. Ensure headless OpenCV replaces GUI opencv-python pulled transitively by rapidocr-onnxruntime / simple-lama-inpainting
pip uninstall -y opencv-python opencv-python-headless || true
pip install --no-cache-dir opencv-python-headless
