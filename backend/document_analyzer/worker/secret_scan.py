"""Worker step: secret/credential scan — tasks.md Phase 2
"Secret/credential scan before persisting extracted strings — on a match,
redact the value (placeholder) before persisting and log the finding with
the job/entity reference for audit; never fails the entity or the job".

specs.md NFR2: extracted strings are scanned for secrets/credentials before
persisting to Supabase; a match is redacted (replaced with a placeholder)
before it's persisted, and the finding is logged for audit with the
job/entity reference — a match never fails the entity or blocks the job.

Cheap heuristics only, same spirit as the boilerplate filter (specs.md FR3's
sibling concern) — both format-specific patterns (AWS keys, GitHub/Slack/
Stripe tokens, PEM private key blocks, JWTs) and a context-specific check
(a dict key that reads like a credential field — `password`, `api_key`, ...
— paired with a non-trivial string value) will false-positive sometimes; that
is by design here, since the alternative (missing a real secret) is worse and
a false positive only costs a redacted placeholder, never a failed job.

The matched secret value itself is never logged or persisted anywhere — only
where (entity + field path) and why (a `reason` name) a finding occurred.
"""

import re
from dataclasses import dataclass
from typing import Any

from supabase import Client

from document_analyzer.supabase_client import insert_many_scoped

_PATTERN_REASONS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |)PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

_CREDENTIAL_KEY_NAMES = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "access_token",
    "auth_token",
    "private_key",
    "client_secret",
}


@dataclass(frozen=True)
class Finding:
    field_path: str
    reason: str


def _pattern_reason(value: str) -> str | None:
    for reason, pattern in _PATTERN_REASONS:
        if pattern.search(value):
            return reason
    return None


def _key_name_reason(key: str | None, value: str) -> str | None:
    if key is not None and key.lower() in _CREDENTIAL_KEY_NAMES and len(value.strip()) >= 4:
        return "hardcoded_credential_field"
    return None


def _redact(value: Any, path: list[str], key: str | None, findings: list[Finding]) -> Any:
    if isinstance(value, dict):
        return {k: _redact(v, path + [k], k, findings) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, path + [str(i)], key, findings) for i, v in enumerate(value)]
    if isinstance(value, str):
        reason = _pattern_reason(value) or _key_name_reason(key, value)
        if reason is not None:
            findings.append(Finding(field_path=".".join(path) if path else "(root)", reason=reason))
            return f"[REDACTED:{reason}]"
        return value
    return value


def scan_and_redact(raw_facts: dict) -> tuple[dict, list[Finding]]:
    """Returns (redacted_raw_facts, findings) — `raw_facts` itself is left untouched."""
    findings: list[Finding] = []
    redacted = _redact(raw_facts, [], None, findings)
    return redacted, findings


def log_findings(client: Client, job_id: str, org_id: str, findings_by_entity_id: dict[str, list[Finding]]) -> None:
    """Batch-logs every entity's findings in one insert — callers resolve entity ids first."""
    rows = [
        {"job_id": job_id, "entity_id": entity_id, "field_path": f.field_path, "reason": f.reason}
        for entity_id, findings in findings_by_entity_id.items()
        for f in findings
    ]
    if rows:
        insert_many_scoped(client, "secret_findings", org_id, rows)
