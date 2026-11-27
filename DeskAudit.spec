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

import os

ICON_PATH = os.path.join(SPECPATH, "installer", "deskaudit.ico")
# The icon ships in the repo (installer/deskaudit.ico) but the GitHub
# snapshot push is text-only, so a fresh clone may not have it — build
# without an icon then, instead of failing.
ICON_PATH = ICON_PATH if os.path.exists(ICON_PATH) else None

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
    # v10.4: windowed build — the exe has NO console at all. The agent
    # must never show a CMD/black window on the desktop, not even the
    # brief flash a console exe shows before Python can hide it, so the
    # console is removed at build time instead of hidden at runtime.
    # One-shot CLI commands still work from the Python source
    # (python main.py --ai-summary, ...); the frozen exe writes its
    # output to the log file and the dashboard instead.
    console=False,
    icon=ICON_PATH,
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

# PyInstaller 6 onedir puts all datas under _internal/, but DeskAudit
# expects config.yaml / projects.yaml next to the exe (editable by the
# user, and data/reports/backups are created next to it). Copy them up
# one level so the shipped folder matches the documented layout.
import os as _os
import shutil as _shutil

_exe_dir = _os.path.join(DISTPATH, "DeskAudit")
for _name in ("config.yaml", "projects.yaml"):
    _src = _os.path.join(_exe_dir, "_internal", _name)
    _dst = _os.path.join(_exe_dir, _name)
    if _os.path.exists(_src):
        _shutil.copyfile(_src, _dst)

# Ship the helper .bat files next to the exe too — the README tells
# users to double-click schedule_summary.bat (9pm job) and
# usb_write_protect.bat (USB write block), but the installer only ships
# dist/DeskAudit/*, so without this copy those features are impossible
# on an installed PC.
for _name in ("schedule_summary.bat", "unschedule_summary.bat",
              "usb_write_protect.bat", "usb_write_allow.bat"):
    _src = _os.path.join(SPECPATH, _name)
    if _os.path.exists(_src):
        _shutil.copyfile(_src, _os.path.join(_exe_dir, _name))

# Ship config.local.yaml (Gemini key + Telegram token) when the build
# machine has it (v12.13, M17): the old flow copied it into dist/
# by hand AFTER the build, so a clean rebuild silently shipped
# without secrets and the bot + LLM fell back to unconfigured/local
# behaviour. Recover it from the latest Drive zip to the REPO ROOT
# before building; the spec copies it in when present. When absent,
# the build still succeeds and the secrets are entered later in
# Dashboard Settings (the Inno installer never deletes this file).
_local_src = _os.path.join(SPECPATH, "config.local.yaml")
if _os.path.exists(_local_src):
    _shutil.copyfile(_local_src, _os.path.join(_exe_dir, "config.local.yaml"))
