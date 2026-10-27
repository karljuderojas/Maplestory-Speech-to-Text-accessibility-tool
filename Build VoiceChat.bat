@echo off
setlocal
cd /d "%~dp0"
title Building Voice Chat
echo.
echo  ===  Voice Chat: building VoiceChat.exe  ===
echo.

rem --- find Python -----------------------------------------------------
set "PY=python"
%PY% --version >nul 2>nul || set "PY=py"
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo  Python isn't installed, or wasn't added to PATH.
    echo  Install Python 3.12 from https://www.python.org/downloads/
    echo  and tick "Add python.exe to PATH" on the first screen of the installer.
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('%PY% --version') do echo  Using %%v
echo.

rem --- GPU or not ------------------------------------------------------
rem  Run with "auto" to detect an NVIDIA card instead of asking.
if /i "%~1"=="auto" goto :detectgpu
choice /c YN /m " Do you have an NVIDIA graphics card and want GPU speed-up (adds about 1 GB)"
if errorlevel 2 (set "GPU=0") else (set "GPU=1")
goto :gpudone
:detectgpu
nvidia-smi -L >nul 2>nul
if errorlevel 1 (set "GPU=0" & echo  No NVIDIA card found: building without GPU support.) else (set "GPU=1" & echo  NVIDIA card found: building with GPU support.)
:gpudone
echo.

rem --- install ---------------------------------------------------------
echo  [1/3] Installing packages. This takes a few minutes...
%PY% -m pip install --upgrade pip --quiet
%PY% -m pip install --upgrade faster-whisper sounddevice numpy keyboard pyperclip sv-ttk pyinstaller || goto :fail
set "EXTRA="
if "%GPU%"=="1" (
    %PY% -m pip install --upgrade nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" || goto :fail
    set "EXTRA=--collect-binaries nvidia.cublas --collect-binaries nvidia.cudnn"
)
echo.

rem --- build -----------------------------------------------------------
echo  [2/3] Building VoiceChat.exe. A few more minutes...
%PY% -m PyInstaller --noconfirm --clean --log-level WARN ^
    --windowed --name VoiceChat --icon voice_chat.ico --uac-admin ^
    --collect-all faster_whisper --collect-all ctranslate2 --collect-data sv_ttk ^
    --add-data "voice_chat.ico;." ^
    %EXTRA% voice_chat.pyw || goto :fail
echo.

rem --- shortcut --------------------------------------------------------
echo  [3/3] Adding a "Voice Chat" shortcut to your desktop...
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Voice Chat.lnk'); $s.TargetPath='%~dp0dist\VoiceChat\VoiceChat.exe'; $s.WorkingDirectory='%~dp0dist\VoiceChat'; $s.IconLocation='%~dp0dist\VoiceChat\VoiceChat.exe'; $s.Save()"

rmdir /s /q build 2>nul
del /q VoiceChat.spec 2>nul
echo.
echo  Done!  Your app is in:  %~dp0dist\VoiceChat\VoiceChat.exe
echo  Double-click "Voice Chat" on your desktop to start it.
echo.
explorer "%~dp0dist\VoiceChat"
pause
exit /b 0

:fail
echo.
echo  Build failed. The error is a few lines above.
echo  If it mentions "no matching distribution" or "requires Python", install
echo  Python 3.12 from python.org (newer versions may not be supported yet) and
echo  run this file again.
echo.
pause
exit /b 1
