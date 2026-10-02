"""Operator-only access to LLM cost (design.md section 7.4).

Cost is internal: org members must never see it, only operators listed in the
`COST_OPERATOR_USER_IDS` environment variable (comma-separated Supabase user ids).
It is empty by default, so nobody has access until someone is configured.

`get_cost_operator` wraps `get_auth_context`: a caller who is not an operator gets
the same `404` as a missing or other-org job, so the cost endpoints' existence
isn't revealed to ordinary users.
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, HTTPException

from document_analyzer.auth import AuthContext, get_auth_context

COST_OPERATOR_ENV = "COST_OPERATOR_USER_IDS"


def operator_user_ids() -> frozenset[str]:
    """Read at call time, so a changed environment takes effect without code changes."""
    raw = os.environ.get(COST_OPERATOR_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def is_cost_operator(user_id: str) -> bool:
    return user_id in operator_user_ids()


def get_cost_operator(auth: Annotated[AuthContext, Depends(get_auth_context)]) -> AuthContext:
    if not is_cost_operator(auth.user_id):
        raise HTTPException(status_code=404, detail="not found")
    return auth
