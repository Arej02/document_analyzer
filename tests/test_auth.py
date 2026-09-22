import jwt
import pytest
from fastapi import HTTPException, Request

from document_analyzer import auth as auth_module
from document_analyzer.auth import get_auth_context

from fakes import FakeClient


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")


def _make_request(headers: dict) -> Request:
    encoded_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "headers": encoded_headers, "method": "POST", "path": "/"}
    return Request(scope)


def _make_token(claims: dict, secret: str = "test-secret") -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.mark.asyncio
async def test_missing_authorization_header_raises_401():
    request = _make_request({})

    with pytest.raises(HTTPException) as exc_info:
        await get_auth_context(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_raises_401():
    request = _make_request({"authorization": "Bearer not-a-real-jwt"})

    with pytest.raises(HTTPException) as exc_info:
        await get_auth_context(request)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_resolves_org_id(monkeypatch):
    token = _make_token({"sub": "user-1", "aud": "authenticated"})
    request = _make_request({"authorization": f"Bearer {token}"})

    fake_client = FakeClient(org_id="org-456")
    monkeypatch.setattr(auth_module, "get_request_scoped_client", lambda access_token: fake_client)

    ctx = await get_auth_context(request)

    assert ctx.user_id == "user-1"
    assert ctx.org_id == "org-456"
    assert ctx.access_token == token
    assert ctx.client is fake_client


@pytest.mark.asyncio
async def test_no_org_membership_raises_403(monkeypatch):
    token = _make_token({"sub": "user-1", "aud": "authenticated"})
    request = _make_request({"authorization": f"Bearer {token}"})

    fake_client = FakeClient(org_id=None)
    monkeypatch.setattr(auth_module, "get_request_scoped_client", lambda access_token: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        await get_auth_context(request)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_es256_token_verified_via_jwks(monkeypatch):
    # Newer Supabase projects (JWT Signing Keys) sign session tokens with an
    # asymmetric key rather than the shared HS256 secret — the ES256 branch
    # in _decode_claims must go through the JWKS client, not os.environ.
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(
        {"sub": "user-1", "aud": "authenticated"},
        private_key,
        algorithm="ES256",
        headers={"kid": "test-kid"},
    )
    request = _make_request({"authorization": f"Bearer {token}"})

    class FakeSigningKey:
        key = private_key.public_key()

    class FakeJwksClient:
        def get_signing_key_from_jwt(self, _token):
            return FakeSigningKey()

    monkeypatch.setattr(auth_module, "_jwks_client", lambda supabase_url: FakeJwksClient())
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")

    fake_client = FakeClient(org_id="org-456")
    monkeypatch.setattr(auth_module, "get_request_scoped_client", lambda access_token: fake_client)

    ctx = await get_auth_context(request)

    assert ctx.user_id == "user-1"
    assert ctx.org_id == "org-456"
