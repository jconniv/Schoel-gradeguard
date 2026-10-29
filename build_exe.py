"""Build an executable named from GradeGuard's canonical application version."""
from pathlib import Path
import subprocess
import sys

from schoel_gradeguard import APP_VERSION


def main():
    root = Path(__file__).resolve().parent
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
               "--name", "SchoelGradeGuard_v" + APP_VERSION.replace(".", "_"),
               "--icon", "gradeguard_icon.ico"]
    for asset in ("gradeguard_logo.png", "gradeguard_icon.png", "gradeguard_icon.ico"):
        command += ["--add-data", f"{asset};."]
    subprocess.run(command + ["schoel_gradeguard.py"], cwd=root, check=True)


if __name__ == "__main__":
    main()
