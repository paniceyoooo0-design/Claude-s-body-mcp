"""OAuth 2.1 endpoint coverage.

Spins up the full Starlette app the way main() does in --http mode (oauth
routes + MCP mount + bearer middleware) and exercises the discovery,
DCR, authorize, token, and protected-resource flows in-process via
httpx's ASGITransport — no network, no real MCP loop.

Coverage targets:
- discovery endpoints reachable without auth
- DCR issues fresh client_id and remembers redirect_uris
- /authorize rejects unknown clients / unregistered redirect_uri / no PKCE
- /authorize auto-approves and redirects with code
- /token authorization_code grant verifies PKCE, issues JWT
- /token refresh_token grant
- /mcp without bearer → 401 with WWW-Authenticate
- /mcp with static MCP_TOKEN bearer → passes auth
- /mcp with OAuth JWT bearer → passes auth
- /mcp with bad bearer → 401
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "mcp-server"))

# Set env BEFORE importing oauth/server so module-level reads see them.
os.environ.setdefault("PUBLIC_BASE_URL", "https://test.example.com")
os.environ["OAUTH_JWT_SECRET"] = "test-jwt-secret-not-real"
os.environ["MCP_TOKEN"] = "test-mcp-token"

import oauth  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402


# ── Tiny fake MCP behind the bearer middleware ──────────────────────────────
# We don't spin up FastMCP here — just need ANY downstream app the middleware
# can either gate or allow through, so we can assert on the 200/401 boundary.


async def fake_mcp(request):
    return JSONResponse({"ok": True, "path": request.url.path})


def make_app() -> Starlette:
    """Mirror the structure server.py main() builds in --http mode."""
    OAUTH_EXEMPT_PREFIXES = (b"/oauth/", b"/.well-known/")

    def bearer_middleware(asgi_app):
        async def mw(scope, receive, send):
            if scope["type"] != "http":
                await asgi_app(scope, receive, send)
                return
            path = scope.get("path", "").encode("latin-1", "replace")
            if any(path.startswith(p) for p in OAUTH_EXEMPT_PREFIXES):
                await asgi_app(scope, receive, send)
                return
            static_expected = os.environ.get("MCP_TOKEN", "")
            headers = dict(scope.get("headers", []))
            got = headers.get(b"authorization", b"").decode("latin-1", "replace")
            bearer = got[len("Bearer "):] if got.startswith("Bearer ") else ""
            ok = False
            if static_expected and bearer == static_expected:
                ok = True
            elif bearer and oauth.verify_access_token(bearer):
                ok = True
            if not ok:
                iss = os.environ.get("PUBLIC_BASE_URL").rstrip("/")
                meta = f"{iss}/.well-known/oauth-protected-resource"
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", f'Bearer resource_metadata="{meta}"'.encode("latin-1")),
                    ],
                })
                await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
                return
            await asgi_app(scope, receive, send)
        return mw

    inner = Starlette(
        routes=oauth.routes() + [Route("/mcp", fake_mcp, methods=["GET", "POST"])],
    )
    return bearer_middleware(inner)


@pytest.fixture
def client():
    app = make_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="https://test.example.com")


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) per RFC 7636 S256."""
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


# ── Discovery ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_protected_resource_metadata(client):
    async with client as c:
        r = await c.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        body = r.json()
        assert body["resource"].endswith("/mcp")
        assert "test.example.com" in body["authorization_servers"][0]


@pytest.mark.asyncio
async def test_auth_server_metadata(client):
    async with client as c:
        r = await c.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        body = r.json()
        assert "S256" in body["code_challenge_methods_supported"]
        assert "authorization_code" in body["grant_types_supported"]
        assert "refresh_token" in body["grant_types_supported"]


# ── DCR ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dcr_issues_client_id(client):
    async with client as c:
        r = await c.post("/oauth/register", json={
            "client_name": "Test Client",
            "redirect_uris": ["https://claude.ai/oauth/callback"],
        })
        assert r.status_code == 201
        body = r.json()
        assert body["client_id"]
        assert body["redirect_uris"] == ["https://claude.ai/oauth/callback"]
        assert body["token_endpoint_auth_method"] == "none"


@pytest.mark.asyncio
async def test_dcr_requires_redirect_uris(client):
    async with client as c:
        r = await c.post("/oauth/register", json={"client_name": "X"})
        assert r.status_code == 400


# ── Authorize ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_rejects_unknown_client(client):
    verifier, challenge = _pkce_pair()
    async with client as c:
        r = await c.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": "does-not-exist",
            "redirect_uri": "https://x/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_authorize_rejects_mismatched_redirect_uri(client):
    verifier, challenge = _pkce_pair()
    async with client as c:
        reg = (await c.post("/oauth/register", json={
            "client_name": "X", "redirect_uris": ["https://a/cb"],
        })).json()
        r = await c.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://EVIL/cb",  # not registered
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_authorize_requires_pkce(client):
    async with client as c:
        reg = (await c.post("/oauth/register", json={
            "client_name": "X", "redirect_uris": ["https://a/cb"],
        })).json()
        r = await c.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://a/cb",
            # no code_challenge
        })
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_authorize_redirects_with_code(client):
    verifier, challenge = _pkce_pair()
    async with client as c:
        reg = (await c.post("/oauth/register", json={
            "client_name": "X", "redirect_uris": ["https://a/cb"],
        })).json()
        r = await c.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://a/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
        }, follow_redirects=False)
        assert r.status_code == 302
        target = urlparse(r.headers["location"])
        q = parse_qs(target.query)
        assert q["code"]
        assert q["state"] == ["xyz"]


# ── Token: authorization_code ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_token_exchange_returns_access_and_refresh(client):
    verifier, challenge = _pkce_pair()
    async with client as c:
        reg = (await c.post("/oauth/register", json={
            "client_name": "X", "redirect_uris": ["https://a/cb"],
        })).json()
        auth_r = await c.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://a/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }, follow_redirects=False)
        code = parse_qs(urlparse(auth_r.headers["location"]).query)["code"][0]

        tok = (await c.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://a/cb",
            "client_id": reg["client_id"],
            "code_verifier": verifier,
        })).json()
        assert tok["access_token"]
        assert tok["refresh_token"]
        assert tok["token_type"] == "Bearer"
        assert tok["expires_in"] == 3600


@pytest.mark.asyncio
async def test_token_rejects_bad_pkce(client):
    verifier, challenge = _pkce_pair()
    async with client as c:
        reg = (await c.post("/oauth/register", json={
            "client_name": "X", "redirect_uris": ["https://a/cb"],
        })).json()
        auth_r = await c.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://a/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }, follow_redirects=False)
        code = parse_qs(urlparse(auth_r.headers["location"]).query)["code"][0]

        r = await c.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://a/cb",
            "client_id": reg["client_id"],
            "code_verifier": "wrong-verifier",
        })
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_token_code_is_single_use(client):
    verifier, challenge = _pkce_pair()
    async with client as c:
        reg = (await c.post("/oauth/register", json={
            "client_name": "X", "redirect_uris": ["https://a/cb"],
        })).json()
        auth_r = await c.get("/oauth/authorize", params={
            "response_type": "code",
            "client_id": reg["client_id"],
            "redirect_uri": "https://a/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }, follow_redirects=False)
        code = parse_qs(urlparse(auth_r.headers["location"]).query)["code"][0]
        # First exchange: succeeds.
        r1 = await c.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://a/cb",
            "client_id": reg["client_id"],
            "code_verifier": verifier,
        })
        assert r1.status_code == 200
        # Second exchange with same code: fails.
        r2 = await c.post("/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://a/cb",
            "client_id": reg["client_id"],
            "code_verifier": verifier,
        })
        assert r2.status_code == 400


# ── Token: refresh_token ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_token_grant_works(client):
    async with client as c:
        # Skip the auth dance; mint a refresh token directly to test the grant.
        refresh = oauth.issue_refresh_token("test-client")
        r = await c.post("/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["access_token"]
        assert body["refresh_token"]


# ── /mcp gating ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_without_bearer_returns_401_with_www_authenticate(client):
    async with client as c:
        r = await c.get("/mcp")
        assert r.status_code == 401
        www = r.headers.get("www-authenticate", "")
        assert "Bearer" in www
        assert "resource_metadata" in www


@pytest.mark.asyncio
async def test_mcp_with_static_mcp_token_passes(client):
    async with client as c:
        r = await c.get("/mcp", headers={"Authorization": "Bearer test-mcp-token"})
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_mcp_with_oauth_jwt_passes(client):
    token = oauth.issue_access_token("some-client-id")
    async with client as c:
        r = await c.get("/mcp", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_mcp_with_bad_bearer_fails(client):
    async with client as c:
        r = await c.get("/mcp", headers={"Authorization": "Bearer total-garbage"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_oauth_endpoints_are_exempt_from_bearer(client):
    """Sanity: discovery must be reachable WITHOUT any bearer (chicken/egg)."""
    async with client as c:
        for path in [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
        ]:
            r = await c.get(path)
            assert r.status_code == 200, f"{path} should be public"
