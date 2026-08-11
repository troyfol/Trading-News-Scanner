# -*- mode: python ; coding: utf-8 -*-

import glob
import os
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# comtypes dynamically generates UI Automation wrappers into
# comtypes/gen/ on first use. In a frozen exe that target is a
# read-only _MEIPASS temp dir, causing TITAN mode to hang. The .py
# run in dev already populated the venv's gen/, so bundle those
# pre-generated files and skip runtime generation entirely.
_GEN_DIR = 'venv/Lib/site-packages/comtypes/gen'
_gen_files = [
    (f, 'comtypes/gen') for f in glob.glob(os.path.join(_GEN_DIR, '*.py'))
]
if not _gen_files:
    # Non-fatal, but surface it: a silently-empty glob ships an exe whose
    # TITAN (UIA) watch mode hangs at runtime. Populate gen/ by running the
    # app once in TITAN mode in the build venv (or install comtypes).
    print("WARNING [TNS.spec]: no comtypes/gen/*.py under %r — TITAN "
          "UIA mode will hang in the built exe (TS/TV modes unaffected)."
          % _GEN_DIR)


def _tcltk_paths():
    """Resolve the Tcl/Tk 8.6 DLLs + data dirs. Prefer the validated
    C:/Python310 layout this build machine uses (so the current build is
    byte-for-byte unchanged); fall back to the build interpreter's own
    install only if C:/Python310 is absent, so a second machine / CI can
    build without it."""
    candidates = [
        r'C:/Python310',
        os.path.dirname(sys.executable),
        sys.prefix,
        getattr(sys, 'base_prefix', sys.prefix),
    ]
    for base in candidates:
        tcl_dll = os.path.join(base, 'DLLs', 'tcl86t.dll')
        tk_dll = os.path.join(base, 'DLLs', 'tk86t.dll')
        tcl_dir = os.path.join(base, 'tcl', 'tcl8.6')
        tk_dir = os.path.join(base, 'tcl', 'tk8.6')
        if all(os.path.exists(p) for p in (tcl_dll, tk_dll, tcl_dir, tk_dir)):
            return tcl_dll, tk_dll, tcl_dir, tk_dir
    print("WARNING [TNS.spec]: could not locate Tcl/Tk 8.6 under any of "
          "%r; using the C:/Python310 literals (build may fail here)."
          % candidates)
    return ('C:/Python310/DLLs/tcl86t.dll', 'C:/Python310/DLLs/tk86t.dll',
            'C:/Python310/tcl/tcl8.6', 'C:/Python310/tcl/tk8.6')


_TCL_DLL, _TK_DLL, _TCL_DIR, _TK_DIR = _tcltk_paths()

# matplotlib ships data files (font cache, stylelib, mpl-data) that
# the runtime needs. PyInstaller's hooks usually catch these but
# being explicit keeps the build deterministic across versions.
_mpl_data = collect_data_files('matplotlib')

hiddenimports = (
    ['win32gui', 'win32api', 'pythoncom', 'tzdata',
     'feedparser', 'sgmllib3k',
     'rapidfuzz', 'rapidfuzz.fuzz', 'rapidfuzz.process',
     # Earnings chart deps
     'pyarrow', 'pyarrow.parquet', 'pyarrow.lib',
     'matplotlib', 'matplotlib.backends.backend_tkagg',
     'matplotlib.figure', 'matplotlib.colors', 'matplotlib.patches',
     # Historical lookup: keyring stores the Polygon API key in
     # Windows Credential Manager. Backend module is loaded
     # dynamically by keyring.get_keyring(), so PyInstaller can't
     # see it without a hidden-import hint.
     'keyring', 'keyring.backends.Windows',
     'keyring.backends.fail', 'keyring.backends.null',
     # New ETF-map modules. The scraper module is imported lazily from
     # inside the Refresh dialog so PyInstaller's static analysis can
     # miss it; list all three explicitly to be safe.
     'etf_map', 'etf_scraper', 'etf_holdings']
    + collect_submodules('comtypes')
    + collect_submodules('feedparser')
    + collect_submodules('pyarrow')
    + collect_submodules('matplotlib')
    + collect_submodules('pandas')
    + collect_submodules('keyring')
)


a = Analysis(
    ['scan_sec.py'],
    pathex=[],
    binaries=[(_TCL_DLL, '.'), (_TK_DLL, '.')],
    datas=[
        (_TCL_DIR, 'tcl/tcl8.6'),
        (_TK_DIR, 'tk/tk8.6'),
        # Bundled seed for the single-stock ETF map. On first launch
        # etf_map.EtfMap copies this out next to the exe so subsequent
        # Refresh runs overwrite a writable copy. If the writable copy
        # is missing, the indicator falls back to the bundled baseline
        # read-only via _MEIPASS.
        ('single_stock_etfs.json', '.'),
        # Bundled seed for the multi-holding ETF holdings map (sector /
        # index / thematic + leveraged-index funds). Same first-launch
        # copy-out + read-only _MEIPASS fallback as the single-stock seed.
        ('etf_holdings.json', '.'),
    ] + _gen_files + _mpl_data,
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
    name='TNS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['7.ico'],
    # Windows file-version resource. Without it the exe carries no
    # version metadata and builds can only be told apart by size/date.
    # Keep version_info.txt in step with scan_sec.__version__.
    version='version_info.txt',
)
