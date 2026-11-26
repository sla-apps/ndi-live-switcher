@echo off
REM Builds NDI Live Switcher into a single Windows .exe
REM Run this from the folder that contains ndi_live.py

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --windowed ^
  --name "NDI Live Switcher" ^
  --collect-all NDIlib ^
  ndi_live.py

echo.
echo Done. Your app is in the "dist" folder:  dist\NDI Live Switcher.exe
pause
