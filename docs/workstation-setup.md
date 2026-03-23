# Workstation Setup

How to set up WhisperSync on a new machine or move to a new computer.

## Fresh Install

```powershell
# 1. Clone the repo
git clone https://github.com/Pendentive/whisper-sync.git
cd whisper-sync

# 2. Run the installer
powershell -ExecutionPolicy Bypass -File install.ps1

# 3. Launch
powershell -ExecutionPolicy Bypass -File start.ps1
```

The installer handles everything: Python venv, CUDA detection, model downloads, desktop shortcut.

## Moving to a New Computer

### What to copy

You need three things from the old machine:

| What | From | To |
|------|------|----|
| SSH key (for git push) | `~/.ssh/id_ed25519` + `id_ed25519.pub` | Same path on new machine |
| App data (.whispersync) | `<output_dir>/.whispersync/` | Same path on new machine |
| Meetings (optional) | `<output_dir>/` (your recordings) | Same path on new machine |

### Quick copy script (run on OLD machine)

```powershell
# Set your paths
$OLD_DATA = "N:\Github\repos\icustomer\ic-product-mgmt\meetings\local-transcriptions"
$EXPORT = "$env:USERPROFILE\Desktop\whispersync-export"

# Create export folder
New-Item -ItemType Directory -Force -Path $EXPORT

# Copy SSH keys
Copy-Item "$env:USERPROFILE\.ssh\id_ed25519" "$EXPORT\id_ed25519"
Copy-Item "$env:USERPROFILE\.ssh\id_ed25519.pub" "$EXPORT\id_ed25519.pub"

# Copy app data
Copy-Item -Recurse "$OLD_DATA\.whispersync" "$EXPORT\.whispersync"

# Copy HuggingFace token
Copy-Item "$env:USERPROFILE\.huggingface\token" "$EXPORT\hf-token"

Write-Host "Export complete: $EXPORT"
Write-Host "Transfer this folder to the new machine."
```

### Setup script (run on NEW machine)

```powershell
# Set your paths
$IMPORT = "$env:USERPROFILE\Desktop\whispersync-export"
$NEW_DATA = "C:\Users\$env:USERNAME\Documents\whispersync-meetings"

# 1. Install SSH key
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.ssh"
Copy-Item "$IMPORT\id_ed25519" "$env:USERPROFILE\.ssh\id_ed25519"
Copy-Item "$IMPORT\id_ed25519.pub" "$env:USERPROFILE\.ssh\id_ed25519.pub"

# 2. Restore HuggingFace token
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.huggingface"
Copy-Item "$IMPORT\hf-token" "$env:USERPROFILE\.huggingface\token"

# 3. Clone and install WhisperSync
git clone git@github.com:Pendentive/whisper-sync.git
cd whisper-sync
powershell -ExecutionPolicy Bypass -File install.ps1

# 4. Restore app data
New-Item -ItemType Directory -Force -Path $NEW_DATA
Copy-Item -Recurse "$IMPORT\.whispersync" "$NEW_DATA\.whispersync"

# 5. Update output_dir in config if path changed
$config = "$NEW_DATA\.whispersync\config.json"
if (Test-Path $config) {
    $json = Get-Content $config | ConvertFrom-Json
    $json.output_dir = $NEW_DATA
    $json | ConvertTo-Json | Set-Content $config -Encoding UTF8
    Write-Host "Updated output_dir to: $NEW_DATA"
}

Write-Host "Setup complete. Run start.ps1 to launch."
```

### SSH config (if using multiple GitHub accounts)

Add to `~/.ssh/config`:
```
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519
```

### GitHub CLI (optional, for PRs/issues)

```powershell
winget install --id GitHub.cli
gh auth login
gh auth setup-git
```

## What Lives Where

| Location | Contents |
|----------|----------|
| `pendentive/whisper-sync/` | Source code + models + venv (the app) |
| `<output_dir>/` | Meeting recordings + INDEX.md |
| `<output_dir>/.whispersync/` | config.json, speaker config, dictation logs |
| `~/.ssh/` | SSH keys for git push |
| `~/.huggingface/token` | HuggingFace auth (for speaker diarization) |

## Updating

```powershell
cd path\to\whisper-sync
git pull
# Restart the app (or it auto-restarts via watchdog)
```
