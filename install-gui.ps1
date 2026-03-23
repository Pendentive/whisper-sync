# WhisperSync GUI Installer launcher
# Usage: powershell -ExecutionPolicy Bypass -File install-gui.ps1

$Python = "python.exe"
& $Python -m whisper_sync.installer_gui
