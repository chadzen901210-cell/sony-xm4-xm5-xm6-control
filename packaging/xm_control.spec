# PyInstaller spec: one folder with "XM Control.exe" (the app, no console) and
# "sonyxm.exe" (the command-line tool). Build with packaging/build.ps1.
from pathlib import Path

root = Path(SPECPATH).parent  # noqa: F821 (set by PyInstaller)
icon = str(root / "sonyxm" / "web" / "icon.ico")
datas = [(str(root / "sonyxm" / "web"), "sonyxm/web")]
excludes = ["tkinter", "unittest", "pydoc", "test"]


def analysis(script):
    return Analysis(  # noqa: F821
        [str(root / "packaging" / script)], pathex=[str(root)], datas=datas, excludes=excludes, noarchive=False
    )


gui = analysis("gui.py")
cli = analysis("cli.py")

gui_exe = EXE(  # noqa: F821
    PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="XM Control", icon=icon, console=False,
    upx=False,
)
cli_exe = EXE(  # noqa: F821
    PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="sonyxm", icon=icon, console=True, upx=False,
)
COLLECT(  # noqa: F821
    gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas, name="XM Control", upx=False,
)
