# PyInstaller: the ClearJAV Uploader as a Windows program in one folder (build.ps1 runs it).
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "clearjav_uploader.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "clearjav_aio" / "fonts"), "clearjav_aio/fonts"),
        (str(ROOT / "clearjav_aio" / "web"), "clearjav_aio/web"),
    ],
    hiddenimports=collect_submodules("keyring.backends"),
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClearJAV-Uploader",
    console=True,  # the window shows the page's address; closing it stops the uploader
    icon=str(ROOT / "cj.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ClearJAV-Uploader", upx=False)
