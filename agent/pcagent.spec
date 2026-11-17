# PyInstaller spec for pcagent.exe (one-folder build: starts fast as a service,
# no unpacking to a temp folder, fewer antivirus false positives than one-file).
# Build: scripts/build-agent.ps1
from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "src" / "pc_agent" / "__main__.py")],
    pathex=[str(root / "src"), str(root.parent / "shared" / "src")],
    # win32timezone is imported dynamically by pywin32 service code.
    hiddenimports=["win32timezone"],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pcagent",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="pcagent", upx=False)
