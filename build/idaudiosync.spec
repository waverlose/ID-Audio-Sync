# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller 打包配置 —— 单文件、无控制台（托盘常驻）
# 用法：  pyinstaller --clean --noconfirm build/idaudiosync.spec
# 产物：  dist/IDAudioSync.exe
#
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['comtypes', 'pycaw', 'PIL']

for pkg in ('soundcard', 'pystray', 'tkinter'):
    _d, _b, _h = collect_all(pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h


a = Analysis(
    ['../src/idaudiosync.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='IDAudioSync',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
