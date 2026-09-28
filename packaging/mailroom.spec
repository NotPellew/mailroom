# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for Mailroom standalone desktop application."""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

repo_root = Path(SPECPATH).resolve()
mailroom_pkg = repo_root / "Mailroom"
templates_dir = mailroom_pkg / "templates"

datas = [
    (str(templates_dir), "Mailroom/templates"),
]

hiddenimports = [
    "Mailroom",
    "Mailroom.desktop",
    "Mailroom.app",
    "Mailroom.routes",
    "Mailroom.cli",
    "Mailroom.config",
    "Mailroom.db",
    "Mailroom.classification",
    "Mailroom.classifier",
    "Mailroom.ingest",
    "Mailroom.export",
    "Mailroom.security",
    "Mailroom.taxonomy",
    "Mailroom.gmail",
    "tkinter",
    "tkinter.ttk",
    "werkzeug",
    "werkzeug.serving",
    "flask",
    "requests",
    "sqlite3",
]

a = Analysis(
    [str(mailroom_pkg / "desktop.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["playwright", "pytest", "ruff", "mypy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Mailroom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Windowed mode: no console window on double-click
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Mailroom.app",
        icon=None,
        bundle_identifier="com.notpellew.mailroom",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "CFBundleVersion": "0.1.0",
            "NSHighResolutionCapable": "True",
        },
    )
