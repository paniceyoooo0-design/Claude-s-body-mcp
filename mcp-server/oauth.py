"""OAuth 2.1 endpoints — lets claude.ai mobile apps connect via Integrations.

Why this exists
---------------
Anthropic's official Claude iOS/Android apps reach MCP servers through
claude.ai's Integrations system, which uses OAuth 2.1 (Dynamic Client
Registration + Authorization Code + PKCE). The pre-existing static
MCP_TOKEN bearer is for explicit configs (Desktop's mcp-remote, Code's
~/.claude.json) and isn't reachable from the mobile app.

These endpoints implement the minimum OAuth surface the spec requires —
nothing more, nothing fancy.

Single-user simplifications
---------------------------
Panice is the only legitimate user. So:
- DCR auto-accepts any registration request and returns a fresh client_id
  without persistent storage (clients re-register cheaply after restart).
- /authorize auto-approves — no consent UI to click. Anyone who reached
  this endpoint via a registered client is treated as Panice.
- Tokens are stateless JWTs signed with HS256 + OAUTH_JWT_SECRET. No
  session table, no revocation. Lifetimes are short (1h access, 30d
  refresh) to limit blast radius.

If this ever becomes multi-tenant, ALL of the above needs revisiting.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
import uuid
from typing import Any

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logger = logging.getLogger("oauth")

# ── Config ──────────────────────────────────────────────────────────────────

# Public base URL (used to advertise endpoint URLs in metadata). Same one the
# rest of the gateway uses for device-fetched audio.
def _issuer() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "https://body.aerogelovepanice.com").rstrip("/")

# JWT secret. Generated once and pinned in .env. If unset, fall back to a
# process-lifetime random — works for dev but means tokens die on restart.
def _jwt_secret() -> str:
    sec = os.environ.get("OAUTH_JWT_SECRET")
    if not sec:
        global _FALLBACK_SECRET
        try:
            return _FALLBACK_SECRET  # type: ignore[name-defined]
        except NameError:
            _FALLBACK_SECRET = secrets.token_urlsafe(48)
            logger.warning(
                "OAUTH_JWT_SECRET not set — using ephemeral secret. Tokens "
                "will not survive gateway restarts. Set it in .env for prod."
            )
            return _FALLBACK_SECRET
    return sec

ACCESS_TOKEN_TTL = 3600          # 1h — short so leaks limit themselves
REFRESH_TOKEN_TTL = 30 * 86400   # 30d — convenience for mobile app
AUTH_CODE_TTL = 300              # 5min — code MUST be exchanged fast
JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = "stackchan-mcp"

# ── In-memory state ─────────────────────────────────────────────────────────
# Restart = forget; clients re-register transparently.

_clients: dict[str, dict[str, Any]] = {}
_codes: dict[str, dict[str, Any]] = {}


def _prune_codes() -> None:
    """Drop expired auth codes. Called opportunistically on each /token."""
    now = time.time()
    expired = [c for c, info in _codes.items() if info["expires_at"] < now]
    for c in expired:
        _codes.pop(c, None)


# ── JWT helpers ─────────────────────────────────────────────────────────────


def issue_access_token(client_id: str) -> str:
    now = int(time.time())
    payload = {
        "iss": _issuer(),
        "sub": "panice",
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
        "client_id": client_id,
        "typ": "access",
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def issue_refresh_token(client_id: str) -> str:
    now = int(time.time())
    payload = {
        "iss": _issuer(),
        "sub": "panice",
        "aud": JWT_AUDIENCE,
        "iat": now,
        "exp": now + REFRESH_TOKEN_TTL,
        "client_id": client_id,
        "typ": "refresh",
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def verify_access_token(token: str) -> bool:
    """Return True iff `token` is a structurally valid, unexpired access JWT
    signed by our secret. Used by the bearer middleware."""
    try:
        payload = jwt.decode(
            token, _jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            audience=JWT_AUDIENCE,
            issuer=_issuer(),
        )
    except jwt.InvalidTokenError as e:
        logger.debug("JWT rejected: %s", e)
        return False
    return payload.get("typ") == "access"


# ── Endpoint: /.well-known/oauth-protected-resource ────────────────────────
# Spec: clients hit /mcp, get 401 with WWW-Authenticate pointing here, then
# discover where to authenticate.


async def protected_resource_metadata(_request: Request) -> Response:
    return JSONResponse({
        "resource": f"{_issuer()}/mcp",
        "authorization_servers": [_issuer()],
        "bearer_methods_supported": ["header"],
    })


# ── Endpoint: /.well-known/oauth-authorization-server ──────────────────────
# Spec: tells clients which endpoints + grants we support.


async def auth_server_metadata(_request: Request) -> Response:
    iss = _issuer()
    return JSONResponse({
        "issuer": iss,
        "authorization_endpoint":  f"{iss}/oauth/authorize",
        "token_endpoint":          f"{iss}/oauth/token",
        "registration_endpoint":   f"{iss}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        # We don't authenticate the token endpoint ('none') — PKCE is the
        # proof of possession. Suitable for public clients (mobile apps).
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


# ── Endpoint: POST /oauth/register (Dynamic Client Registration) ───────────


async def register(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    redirect_uris = body.get("redirect_uris") or []
    if not isinstance(redirect_uris, list) or not redirect_uris:
        return JSONResponse(
            {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
            status_code=400,
        )

    client_id = uuid.uuid4().hex
    _clients[client_id] = {
        "client_name": body.get("client_name", "unnamed"),
        "redirect_uris": list(redirect_uris),
        "registered_at": int(time.time()),
    }
    logger.info("DCR: %s registered with redirect_uris=%s",
                _clients[client_id]["client_name"], redirect_uris)

    # Public client (no secret) — PKCE is the auth.
    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, status_code=201)


# ── Endpoint: GET /oauth/authorize (auto-approve) ───────────────────────────


async def authorize(request: Request) -> Response:
    q = request.query_params
    client_id = q.get("client_id", "")
    redirect_uri = q.get("redirect_uri", "")
    response_type = q.get("response_type", "")
    code_challenge = q.get("code_challenge", "")
    code_challenge_method = q.get("code_challenge_method", "")
    state = q.get("state", "")
    scope = q.get("scope", "mcp")

    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)

    client = _clients.get(client_id)
    if not client:
        return JSONResponse(
            {"error": "invalid_client", "error_description": "unknown client_id; re-register first"},
            status_code=400,
        )
    if redirect_uri not in client["redirect_uris"]:
        return JSONResponse(
            {"error": "invalid_redirect_uri",
             "error_description": f"{redirect_uri} not in client's registered redirect_uris"},
            status_code=400,
        )
    if code_challenge_method != "S256" or not code_challenge:
        return JSONResponse(
            {"error": "invalid_request",
             "error_description": "PKCE with S256 code_challenge required"},
            status_code=400,
        )

    # Auto-approve. Issue code, store PKCE challenge for verification at /token.
    code = secrets.token_urlsafe(32)
    _codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "scope": scope,
        "expires_at": time.time() + AUTH_CODE_TTL,
    }
    logger.info("authorize: auto-approved code for client=%s scope=%s",
                client.get("client_name"), scope)

    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}code={code}"
    if state:
        target += f"&state={state}"
    return RedirectResponse(target, status_code=302)


# ── Endpoint: POST /oauth/token ─────────────────────────────────────────────


def _pkce_match(verifier: str, challenge: str) -> bool:
    """code_challenge == base64url(sha256(verifier)) per RFC 7636."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return secrets.compare_digest(computed, challenge)


async def token(request: Request) -> Response:
    _prune_codes()
    form = await request.form()
    grant_type = form.get("grant_type", "")

    if grant_type == "authorization_code":
        code = form.get("code", "")
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        code_verifier = form.get("code_verifier", "")

        info = _codes.pop(code, None)  # single-use
        if not info or info["expires_at"] < time.time():
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "code missing or expired"},
                                status_code=400)
        if info["client_id"] != client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if info["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "redirect_uri mismatch"},
                                status_code=400)
        if not _pkce_match(code_verifier, info["code_challenge"]):
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": "PKCE verification failed"},
                                status_code=400)

        return JSONResponse({
            "access_token": issue_access_token(client_id),
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": issue_refresh_token(client_id),
            "scope": info["scope"],
        })

    if grant_type == "refresh_token":
        refresh = form.get("refresh_token", "")
        try:
            payload = jwt.decode(
                refresh, _jwt_secret(),
                algorithms=[JWT_ALGORITHM],
                audience=JWT_AUDIENCE,
                issuer=_issuer(),
            )
        except jwt.InvalidTokenError as e:
            return JSONResponse({"error": "invalid_grant",
                                 "error_description": str(e)},
                                status_code=400)
        if payload.get("typ") != "refresh":
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        client_id = payload.get("client_id", "")
        return JSONResponse({
            "access_token": issue_access_token(client_id),
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_TTL,
            "refresh_token": issue_refresh_token(client_id),
        })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


# ── Route export ────────────────────────────────────────────────────────────


def routes() -> list[Route]:
    """All OAuth-related routes for mounting in the main Starlette app."""
    return [
        Route("/.well-known/oauth-protected-resource", protected_resource_metadata),
        Route("/.well-known/oauth-authorization-server", auth_server_metadata),
        Route("/oauth/register", register, methods=["POST"]),
        Route("/oauth/authorize", authorize, methods=["GET"]),
        Route("/oauth/token", token, methods=["POST"]),
    ]
