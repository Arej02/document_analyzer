"""Request-scoped auth — design.md §1a.

org_id is never trusted from the request; it's always resolved server-side from
the validated Supabase JWT via the `current_org_id()` RPC (backed by `org_members`).
"""

import logging
import os
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient
from supabase import Client

from .supabase_client import get_request_scoped_client, supabase_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    org_id: str
    access_token: str
    client: Client


def _extract_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization")
    if not header or not header.lower().startswith("bearer "):
        logger.warning("rejecting request: missing or malformed Authorization header")
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return header.split(" ", 1)[1].strip()


# Keyed by SUPABASE_URL (not just a bare singleton) so switching projects
# between test/dev environments in the same process can't serve a stale
# client — PyJWKClient itself caches fetched keys internally per URL.
_jwks_clients: dict[str, PyJWKClient] = {}


def _jwks_client(supabase_url: str) -> PyJWKClient:
    client = _jwks_clients.get(supabase_url)
    if client is None:
        client = PyJWKClient(f"{supabase_url}/auth/v1/.well-known/jwks.json")
        _jwks_clients[supabase_url] = client
    return client


def _decode_claims(token: str) -> dict:
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg == "HS256":
            # Legacy Supabase projects sign session tokens directly with the
            # shared JWT secret — the only reason SUPABASE_JWT_SECRET is needed.
            secret = os.environ.get("SUPABASE_JWT_SECRET")
            if not secret:
                logger.warning(
                    "rejecting an HS256 token: SUPABASE_JWT_SECRET is not set. This project "
                    "still signs with the legacy JWT secret; set it, or switch the project to "
                    "JWT signing keys (Auth > JWT Keys)"
                )
                raise HTTPException(status_code=401, detail="Invalid or expired token")
            return jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated")
        # Newer Supabase projects sign session tokens with an asymmetric key
        # (typically ES256) under Auth > JWT Signing Keys — verify against
        # the project's published JWKS instead of a shared secret. See
        # design.md §1a.
        signing_key = _jwks_client(supabase_url()).get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key, algorithms=[alg], audience="authenticated")
    except (jwt.PyJWTError, KeyError) as exc:
        logger.warning("rejecting token: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc


def _resolve_org_id(client: Client) -> str:
    result = client.rpc("current_org_id").execute()
    org_id = result.data
    if not org_id:
        raise HTTPException(status_code=403, detail="No organization membership found for this user")
    return org_id


async def get_auth_context(request: Request) -> AuthContext:
    """FastAPI dependency: validates the caller's JWT and resolves their org_id.

    Never call this from worker code — the worker uses the service-role client
    (see supabase_client.get_service_role_client), a separate, unauthenticated-request path.
    """
    token = _extract_bearer_token(request)
    claims = _decode_claims(token)
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    client = get_request_scoped_client(token)
    org_id = _resolve_org_id(client)
    return AuthContext(user_id=user_id, org_id=org_id, access_token=token, client=client)
