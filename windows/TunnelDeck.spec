# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['app.main', 'app.cf', 'app.db', 'app.net', 'app.runner', 'app.scan']
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('pystray')


a = Analysis(
    ['tunneldeck_win.py'],
    pathex=['D:/code/tunnel-deck'],
    binaries=[],
    datas=[('D:/code/tunnel-deck/web', 'web'), ('D:/code/tunnel-deck/windows/cloudflared.exe', '.')],
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
    name='TunnelDeck',
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
    icon=['D:/code/tunnel-deck/windows/icon.ico'],
)
