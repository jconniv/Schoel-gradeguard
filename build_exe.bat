@echo off
pyinstaller --noconfirm --onefile --windowed --name SchoelGradeGuard_v9_0_4 --icon gradeguard_icon.ico --add-data "gradeguard_logo.png;." --add-data "gradeguard_icon.png;." --add-data "gradeguard_icon.ico;." schoel_gradeguard.py
pause
