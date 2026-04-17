from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REQUIRED_RUNTIME_FILES = (
    "DataFusion-RT.exe",
    "python38.dll",
    "VCRUNTIME140.dll",
    "MSVCP140.dll",
    "base_library.zip",
    "config\\config.yaml",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify packaged DataFusion build.")
    parser.add_argument("build_dir", help="Path to packaged build directory.")
    parser.add_argument(
        "--smoke-arg",
        default="--list-ports",
        help="Argument for smoke start. Default: --list-ports",
    )
    args = parser.parse_args()

    build_dir = Path(args.build_dir).resolve()
    if not build_dir.exists():
        print(f"Build dir not found: {build_dir}", file=sys.stderr)
        return 1

    missing = [name for name in REQUIRED_RUNTIME_FILES if not (build_dir / name).exists()]
    if missing:
        print("Missing runtime files:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        return 2

    exe_path = build_dir / "DataFusion-RT.exe"
    process = subprocess.run(
        [str(exe_path), args.smoke_arg],
        cwd=str(build_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    output = process.stdout.decode("utf-8", errors="replace")
    sys.stdout.buffer.write(output.encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
    if process.returncode != 0:
        print(f"Smoke run failed: exit code {process.returncode}", file=sys.stderr)
        return process.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
