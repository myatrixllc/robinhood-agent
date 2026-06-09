"""
auth.py — One-time Robinhood OAuth2 authentication
Run this ONCE on your VM to log in. Token is saved and auto-refreshed forever.

Usage:
    python auth.py
"""

import os
import json
import time
import secrets
import hashlib
import base64
import webbrowser
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN_FILE      = Path(__file__).parent.parent / ".robinhood_token.json"
AUTH_BASE_URL   = "https://agent.robinhood.com"
TOKEN_URL       = "https://api.robinhood.com/oauth2/token/"
REDIRECT_URI    = "http://localhost:8765/callback"
SCOPES          = "trading:read trading:write accounts:read"

# Robinhood's public MCP client ID (from their OAuth discovery doc)
CLIENT_ID       = "robinhood-trading-mcp"


# ── PKCE helpers ──────────────────────────────────────────────────────────────
def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── Local callback server ─────────────────────────────────────────────────────
_auth_code = None

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code
        params     = parse_qs(urlparse(self.path).query)
        _auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"""
        <html><body style='font-family:sans-serif;text-align:center;padding:60px'>
        <h2>&#10003; Robinhood Connected!</h2>
        <p>You can close this tab and return to your terminal.</p>
        </body></html>
        """)

    def log_message(self, *args):
        pass  # suppress request logs


def wait_for_callback(timeout: int = 120) -> str:
    """Start local server and wait for OAuth callback."""
    server = HTTPServer(("localhost", 8765), CallbackHandler)
    server.timeout = timeout
    print(f"  Waiting for Robinhood login (timeout: {timeout}s)...")
    server.handle_request()
    if not _auth_code:
        raise RuntimeError("No auth code received — did you complete the login?")
    return _auth_code


# ── Token exchange ────────────────────────────────────────────────────────────
def exchange_code(code: str, verifier: str) -> dict:
    """Exchange auth code for access + refresh tokens."""
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     CLIENT_ID,
        "code_verifier": verifier,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def save_token(token_data: dict):
    """Save token to file with expiry timestamp."""
    token_data["saved_at"] = time.time()
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    TOKEN_FILE.chmod(0o600)  # owner read/write only
    print(f"  Token saved to {TOKEN_FILE}")


# ── Main auth flow ────────────────────────────────────────────────────────────
def authenticate():
    print("\n🔐 Robinhood OAuth2 Authentication")
    print("=" * 45)

    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type":         "code",
        "client_id":             CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "scope":                 SCOPES,
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }

    auth_url = f"{AUTH_BASE_URL}/oauth2/authorize/?{urlencode(params)}"

    print("\nStep 1: Open this URL in your browser and log into Robinhood:")
    print(f"\n  {auth_url}\n")

    # Try to auto-open browser (works locally, not on headless VM)
    try:
        webbrowser.open(auth_url)
        print("  (Browser opened automatically)")
    except Exception:
        print("  (Copy the URL above into your browser manually)")

    print("\nStep 2: Log in and approve access...")
    print("Step 3: You'll be redirected back automatically.\n")

    # Wait for callback
    code = wait_for_callback()
    print(f"  Auth code received ✓")

    # Exchange for tokens
    print("\nExchanging code for tokens...")
    token_data = exchange_code(code, verifier)
    save_token(token_data)

    print("\n✅ Authentication complete!")
    print(f"   Access token expires in: {token_data.get('expires_in', '?')}s")
    print(f"   Refresh token saved — will auto-renew forever\n")


if __name__ == "__main__":
    if TOKEN_FILE.exists():
        print(f"⚠️  Token file already exists at {TOKEN_FILE}")
        ans = input("Re-authenticate? (y/N): ").strip().lower()
        if ans != "y":
            print("Skipping. Delete the token file to re-auth.")
            exit(0)
    authenticate()
