#!/usr/bin/env python3
"""Build script for Mailroom standalone desktop application."""

import subprocess
import sys
from pathlib import Path


def build():
    repo_root = Path(__file__).resolve().parent.parent
    spec_file = repo_root / "packaging" / "mailroom.spec"

    print("Checking build environment...")
    try:
        import PyInstaller
        print(f"PyInstaller {PyInstaller.__version__} detected.")
    except ImportError:
        print("PyInstaller is not installed in the current environment.")
        print("To build the standalone executable, install PyInstaller with: pip install pyinstaller")
        return 1

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        str(spec_file),
    ]

    print(f"Building Mailroom standalone executable: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(repo_root))
    if result.returncode == 0:
        dist_dir = repo_root / "dist"
        print(f"\nBuild complete! Standalone executable is located in: {dist_dir}")
        for item in dist_dir.iterdir():
            print(f" - {item.name}")
    else:
        print(f"\nBuild failed with exit code {result.returncode}", file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    sys.exit(build())
