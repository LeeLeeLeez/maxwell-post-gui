# -*- mode: python ; coding: utf-8 -*-
"""
MaxwellPost.spec —— PyInstaller 打包配置

用法（必须用系统 Python 3.12，venv 那个 3.13 没有 tkinter）:
    py -3.12 -m PyInstaller MaxwellPost.spec --noconfirm --clean

产出 dist/MaxwellPost.exe

注意：
  * datas 里必须带 indcalc_core.py，否则 matrix等效 tab 在打包版里不可用
  * 打出来的 exe 要放在旁边有 env\\Scripts\\python.exe（装了 PyAEDT）的目录里才能跑
    AEDT 相关的 tab；也可以在 GUI 顶部「环境设置」里手动指定 python.exe
  * matrix等效 是纯 numpy 离线计算，不依赖 AEDT，任何机器都能用
"""

a = Analysis(
    ['aedt_gui.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('aedt_gui_backend.py', '.'),
        ('current_integral_pipeline.py', '.'),
        ('aedt_env.py', '.'),
        ('section_cs.py', '.'),
        ('indcalc_core.py', '.'),
        ('maxwell_m.ico', '.'),
    ],
    hiddenimports=[
        'numpy',
        # 2026-09-10：这两个之前被 excludes 掉了，导致打包版
        # ① 柱状图降级成 Canvas ② 左侧菜单图标全空（_HAVE_PIL=False）
        'PIL',
        'PIL._tkinter_finder',
        'matplotlib',
        'matplotlib.backends.backend_tkagg',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'IPython', 'tkinter.test', 'PySide6', 'PyQt5'],
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
    name='MaxwellPost',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # 本机没装 UPX，开着会在收尾阶段报 warning
    runtime_tmpdir=None,
    console=False,             # GUI 程序，不要黑窗
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='maxwell_m.ico',
)
