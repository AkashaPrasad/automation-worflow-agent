"""Google OAuth (per-workspace user credentials). Mount with `app.include_router(oauth.router)`.

Active only when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are set (otherwise 501).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.tools.connectors import tokens

router = APIRouter(prefix="/api/oauth")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
STATE_TTL_S = 600
_WS_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

# Overridable in tests: httpx transport used for the token exchange.
_transport: httpx.AsyncBaseTransport | None = None


def _require_configured() -> tuple[str, str]:
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret):
        raise HTTPException(501, "Google OAuth is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)")
    return s.google_client_id, s.google_client_secret


def _signing_key() -> bytes:
    s = get_settings()
    return (os.getenv("OAUTH_STATE_SECRET") or s.google_client_secret or "unset").encode()


def sign_state(workspace_id: str, now: float | None = None) -> str:
    payload = {"ws": workspace_id, "exp": int((now or time.time()) + STATE_TTL_S), "n": secrets.token_hex(4)}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_state(state: str, now: float | None = None) -> str | None:
    try:
        body, sig = state.rsplit(".", 1)
        good = hmac.new(_signing_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload["exp"] < (now or time.time()):
            return None
        return payload["ws"]
    except Exception:  # noqa: BLE001
        return None


def redirect_uri() -> str:
    return f"{get_settings().public_base_url}/api/oauth/google/callback"


@router.get("/google/start")
async def google_start(workspace_id: str = Query(...)) -> RedirectResponse:
    client_id, _ = _require_configured()
    if not _WS_RE.match(workspace_id):
        raise HTTPException(400, "invalid workspace_id")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": sign_state(workspace_id),
    }
    return RedirectResponse(f"{AUTH_URL}?{urlencode(params)}", status_code=302)


@router.get("/google/callback")
async def google_callback(code: str = "", state: str = "", error: str = "") -> RedirectResponse:
    client_id, client_secret = _require_configured()
    front = get_settings().frontend_url
    if error:
        return RedirectResponse(f"{front}/workspace?connected=google&error={error[:60]}", status_code=302)
    workspace_id = verify_state(state)
    if not workspace_id or not code:
        raise HTTPException(400, "invalid or expired OAuth state")
    async with httpx.AsyncClient(transport=_transport, timeout=20) as http:
        r = await http.post(TOKEN_URL, data={
            "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
        })
    if r.status_code != 200:
        raise HTTPException(502, "Google token exchange failed")
    tok = r.json()
    prior = tokens.load_token(workspace_id, "google") or {}
    refresh = tok.get("refresh_token") or prior.get("refresh_token")
    if not refresh:
        raise HTTPException(400, "Google returned no refresh token; revoke access and retry")
    tokens.save_token(workspace_id, "google", {
        "token": tok.get("access_token"),
        "refresh_token": refresh,
        "expiry": time.time() + int(tok.get("expires_in", 3600)),
        "scopes": (tok.get("scope") or " ".join(GOOGLE_SCOPES)).split(),
    })
    return RedirectResponse(f"{front}/workspace?connected=google", status_code=302)


@router.get("/google/status")
async def google_status(workspace_id: str = Query(...)) -> dict:
    t = tokens.load_token(workspace_id, "google") if _WS_RE.match(workspace_id) else None
    s = get_settings()
    return {"configured": bool(s.google_client_id and s.google_client_secret), "connected": bool(t),
            "encrypted_at_rest": tokens.encryption_enabled()}


@router.delete("/google")
async def google_disconnect(workspace_id: str = Query(...)) -> dict:
    if not _WS_RE.match(workspace_id):
        raise HTTPException(400, "invalid workspace_id")
    tokens.delete_token(workspace_id, "google")
    return {"ok": True}
