# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for DeskAudit (Windows).

Build on Windows with:  build_exe.bat
(or: pip install pyinstaller && pyinstaller DeskAudit.spec --clean --noconfirm)

Output: dist/DeskAudit/DeskAudit.exe  (onedir — the whole folder ships,
so config.yaml, projects.yaml and the dashboard page sit next to the exe
and stay editable. data/, reports/ and backups/ are created next to the
exe on first run.)
"""

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("config.yaml", "."),
        ("projects.yaml", "."),
        ("deskaudit/dashboard_static", "deskaudit/dashboard_static"),
    ],
    hiddenimports=[
        "watchdog.observers",
        "watchdog.observers.read_directory_changes",
        "yaml",
        "win32gui",
        "win32process",
        "win32api",
        "win32con",
        "pythoncom",
        "pywintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DeskAudit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DeskAudit",
)
