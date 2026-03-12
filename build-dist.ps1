# Build WhisperSync distribution zip
# Usage: powershell -ExecutionPolicy Bypass -File scripts/whisper_sync/build-dist.ps1

$ErrorActionPreference = "Stop"
$PkgSource = $PSScriptRoot
$Version = "1.0"
$ZipName = "whisper-sync-v$Version.zip"
$OutputZip = "$PSScriptRoot\..\$ZipName"
$StagingDir = "$env:TEMP\whisper-sync-build"

Write-Host "=== Building WhisperSync distribution ===" -ForegroundColor Cyan

# ── Clean staging area ──

if (Test-Path $StagingDir) {
    Remove-Item -Recurse -Force $StagingDir
}

$StageRoot = "$StagingDir\whisper-sync"
$StagePkg = "$StageRoot\whisper_sync"

New-Item -ItemType Directory -Path $StagePkg -Force | Out-Null

# ── Copy Python package files ──

$pyFiles = @(
    "__init__.py",
    "__main__.py",
    "capture.py",
    "transcribe.py",
    "config.py",
    "config.defaults.json",
    "paste.py",
    "icons.py",
    "model_status.py",
    "flatten.py",
    "paths.py",
    "logger.py",
    "benchmark.py",
    "dictation_log.py",
    "streaming_wav.py",
    "crash_diagnostics.py",
    "whisper-capture.ico"
)

foreach ($f in $pyFiles) {
    $src = "$PkgSource\$f"
    if (Test-Path $src) {
        Copy-Item $src "$StagePkg\$f"
        Write-Host "  + whisper_sync/$f" -ForegroundColor Gray
    } else {
        Write-Host "  ! Missing: $f" -ForegroundColor Yellow
    }
}

# ── Create .standalone marker ──

"" | Out-File -Encoding ASCII "$StagePkg\.standalone"
Write-Host "  + whisper_sync/.standalone (marker)" -ForegroundColor Gray

# ── Copy top-level files ──

# Requirements
Copy-Item "$PkgSource\requirements.txt" "$StageRoot\requirements.txt"
Write-Host "  + requirements.txt" -ForegroundColor Gray

# Install and launch scripts
Copy-Item "$PkgSource\install.ps1" "$StageRoot\install.ps1"
Write-Host "  + install.ps1" -ForegroundColor Gray

Copy-Item "$PkgSource\start.ps1" "$StageRoot\start.ps1"
Write-Host "  + start.ps1" -ForegroundColor Gray

# README + Transcription Guide
Copy-Item "$PkgSource\README.md" "$StageRoot\README.md"
Write-Host "  + README.md" -ForegroundColor Gray

Copy-Item "$PkgSource\TRANSCRIPTION-GUIDE.md" "$StageRoot\TRANSCRIPTION-GUIDE.md"
Write-Host "  + TRANSCRIPTION-GUIDE.md" -ForegroundColor Gray

# ── Create zip ──

if (Test-Path $OutputZip) {
    Remove-Item $OutputZip
}

Write-Host ""
Write-Host "Creating $ZipName..." -ForegroundColor Green
Compress-Archive -Path $StageRoot -DestinationPath $OutputZip

# ── Cleanup ──

Remove-Item -Recurse -Force $StagingDir

$size = (Get-Item $OutputZip).Length / 1KB
Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Cyan
Write-Host "Output: $OutputZip ($([math]::Round($size)) KB)" -ForegroundColor Green
Write-Host ""
Write-Host "To distribute: send the zip file. Recipient extracts and runs install.ps1" -ForegroundColor Gray
