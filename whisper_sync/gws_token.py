"""Get a fresh Google OAuth access token from gws credentials.

Usage:
    python scripts/gws-token.py
    TOKEN=$(python scripts/gws-token.py)
    curl -H "Authorization: Bearer $TOKEN" https://...

Reads the encrypted credentials from ~/.config/gws/ using the same
encryption key that gws uses. Returns only the access token on stdout.
"""

import json
import sys
import urllib.request
import urllib.parse
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "gws"
CLIENT_FILE = CONFIG_DIR / "client_secret.json"

def get_client_creds():
    with open(CLIENT_FILE) as f:
        data = json.load(f)
    installed = data.get("installed", data)
    return installed["client_id"], installed["client_secret"]

def get_refresh_token():
    """Try multiple sources for the refresh token."""
    # Source 1: plain credentials.json (if keyring backend is 'file')
    plain = CONFIG_DIR / "credentials.json"
    if plain.exists():
        with open(plain) as f:
            data = json.load(f)
        return data.get("refresh_token")

    # Source 2: ask gws auth export and parse (token is masked in newer versions)
    import subprocess
    result = subprocess.run(["gws", "auth", "export"], capture_output=True, text=True, shell=True)
    output = "\n".join(l for l in result.stdout.splitlines() if not l.startswith("Using"))
    if output.strip():
        data = json.loads(output)
        rt = data.get("refresh_token", "")
        if len(rt) > 20:  # not masked
            return rt

    print("ERROR: Cannot extract refresh token. gws masks it and credentials are encrypted.", file=sys.stderr)
    print("Workaround: run 'gws auth login' with GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file", file=sys.stderr)
    print("This stores credentials in plain JSON instead of OS keyring.", file=sys.stderr)
    sys.exit(1)

def exchange_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["access_token"]

if __name__ == "__main__":
    client_id, client_secret = get_client_creds()
    refresh_token = get_refresh_token()
    token = exchange_token(client_id, client_secret, refresh_token)
    print(token)
