"""Public frontend config — design.md §4 `GET /api/config`.

The React app fetches the Supabase project URL and anon key from here at startup,
so the only Supabase settings live in the backend's .env. Unauthenticated on
purpose: the browser needs these values to sign in in the first place, and the
anon key is public by design (RLS + the backend's JWT validation, design.md §1a,
are what enforce access). Never add the service-role key or any other secret here.
"""

import os

from fastapi import APIRouter, HTTPException

from .supabase_client import supabase_url

router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
def get_config() -> dict:
    anon_key = os.environ.get("SUPABASE_ANON_KEY")
    if not anon_key:
        raise HTTPException(status_code=503, detail="SUPABASE_ANON_KEY is not set in the backend's .env")
    try:
        url = supabase_url()
    except (KeyError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail="Set SUPABASE_DB_URL (a Supabase connection string) or SUPABASE_URL in the backend's .env",
        ) from None
    return {"supabase_url": url, "supabase_anon_key": anon_key}
