#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Linux virtual environment was not found."
  echo "Create it first, for example:"
  echo "  python3 -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  python -m pip install -r requirements.txt"
  exit 1
fi

source ".venv/bin/activate"

if ! python -m pip show pyinstaller >/dev/null 2>&1; then
  echo "Installing PyInstaller..."
  python -m pip install pyinstaller
fi

rm -rf build-linux dist-linux/DataFusion-RT

echo "Building Linux bundle..."
pyinstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name DataFusion-RT \
  --distpath dist-linux \
  --workpath build-linux \
  --specpath build-linux \
  app/main.py

mkdir -p "dist-linux/DataFusion-RT/config"
mkdir -p "dist-linux/DataFusion-RT/data"
mkdir -p "dist-linux/DataFusion-RT/logs"

cp "config/config.yaml" "dist-linux/DataFusion-RT/config/config.yaml"
cp "config/config.example.yaml" "dist-linux/DataFusion-RT/config/config.example.yaml"
cp "README.md" "dist-linux/DataFusion-RT/README.md"
chmod +x "dist-linux/DataFusion-RT/DataFusion-RT"

echo "Linux build completed."
echo "Binary: dist-linux/DataFusion-RT/DataFusion-RT"
