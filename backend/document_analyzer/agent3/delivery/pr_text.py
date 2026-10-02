"""Branch names, PR titles and PR bodies — design_agent3.md §15.4, §15.9.

Pure. Everything here that comes from repository content (phase and unit labels are LLM-derived from
untrusted source code) is neutralised before it reaches GitHub:

- Titles and branch names are FIXED templates: they contain an execution id prefix and a phase number,
  never a label. A branch slug is built from a label but only from `[a-z0-9-]`, 40 characters at most.
- In a PR body a label only ever appears inside a single code span, after control and invisible characters,
  backticks and newlines are removed and the text is truncated. Inside a code span GitHub does not render
  markdown, HTML, images or autolinked references, and `@name` does not mention anyone. `|` is replaced so a
  label cannot break out of a table cell.
- Statuses, flags and slugs are checked against fixed sets / patterns; anything unexpected is shown as a
  neutralised code span, not trusted.
"""

import os
import re
import unicodedata
from typing import Any, Mapping
from urllib.parse import quote

from ..commit_plan import KNOWN_FLAGS, KNOWN_STATUSES
from ..project_layout import UNIT_SLUG_PATTERN

_LABEL_LIMIT = 80
_SLUG_LIMIT = 40
_MAX_TABLE_ROWS = 200
_APP_URL = re.compile(r"^https?://[^\s<>\"'`]{1,300}$")


def neutralize(text: Any, limit: int = _LABEL_LIMIT) -> str:
    """Single-line, printable, no backticks or pipes, at most `limit` characters."""
    cleaned = []
    for char in str(text if text is not None else ""):
        category = unicodedata.category(char)
        if category[0] == "C" or category in ("Zl", "Zp"):  # control, format (bidi, zero-width), separators
            cleaned.append(" ")
        elif char == "`":
            continue
        elif char == "|":
            cleaned.append("¦")
        else:
            cleaned.append(char)
    return " ".join("".join(cleaned).split())[:limit].rstrip()


def code(text: Any) -> str:
    """A label as one inline code span (see module docstring)."""
    inner = neutralize(text)
    return f"`{inner}`" if inner else "`(unnamed)`"


def slugify(label: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(label or "").lower()).strip("-")
    return slug[:_SLUG_LIMIT].strip("-")


def exec_ref(execution_job_id: str) -> str:
    """First 8 hex characters of the execution id: the part of every branch name that keeps two
    executions' branches apart."""
    ref = re.sub(r"[^0-9a-f]", "", str(execution_job_id).lower())[:8]
    return ref or "00000000"


def branch_name(execution_job_id: str, group_key: str, label: Any = None) -> str:
    """`migrate/{exec8}/scaffold` or `migrate/{exec8}/phase-{n}[-{slug}]`. `group_key` is the plan's
    `scaffold` / `phase-{n}`; the number is checked, the slug comes only from `[a-z0-9-]`."""
    prefix = f"migrate/{exec_ref(execution_job_id)}/"
    if group_key == "scaffold":
        return prefix + "scaffold"
    match = re.fullmatch(r"phase-(\d{1,6})", group_key)
    if not match:
        raise ValueError(f"unexpected group key {group_key!r}")
    slug = slugify(label)
    return prefix + f"phase-{int(match.group(1))}" + (f"-{slug}" if slug else "")


def pr_title(execution_job_id: str, group_key: str, total_phases: int) -> str:
    if group_key == "scaffold":
        return f"Add project scaffold (migration {exec_ref(execution_job_id)})"
    number = int(group_key.removeprefix("phase-"))
    return f"Migrate phase {number} of {int(total_phases)} (migration {exec_ref(execution_job_id)})"


def _app_link(execution_job_id: str) -> str:
    base = os.environ.get("APP_BASE_URL", "").strip()
    if not _APP_URL.match(base):
        return ""
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}execution={quote(str(execution_job_id), safe='')}"


def _stack_notice(base_branch: str, default_branch: str) -> str:
    return (
        f"> **This pull request is part of a stack.** Its base is `{neutralize(base_branch)}`"
        + ("" if base_branch != default_branch else " (the default branch)")
        + ", so GitHub's *Files changed* shows only this step when compared with that base. Merge the stack in order, "
        "oldest first, using **Create a merge commit**: squash or rebase merging rewrites the commits and makes the next "
        "pull request show conflicts. The repository must delete head branches after merge so the next pull request is "
        "retargeted automatically."
    )


_ADVISORY = (
    "Agent 3's sandbox verdicts are advisory: generated code and its tests run in the same process, so a test can pass "
    "without the code being correct. The CI run on this pull request is the independent check."
)


def _status_cell(status: Any) -> str:
    return f"`{status}`" if status in KNOWN_STATUSES else code(status)


def _flags_cell(flags: Any) -> str:
    flags = flags if isinstance(flags, list) else []
    if not flags:
        return "—"
    return ", ".join(f"`{flag}`" if flag in KNOWN_FLAGS else code(flag) for flag in flags)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _summary_line(final_test_summary: Mapping[str, Any] | None) -> str:
    if not isinstance(final_test_summary, Mapping):
        return ""
    return (
        f"Agent 3's final run of the whole project: {_int(final_test_summary.get('passed'))} passed, "
        f"{_int(final_test_summary.get('failed'))} failed, {_int(final_test_summary.get('errors'))} errors."
    )


def scaffold_body(*, execution_job_id: str, repo_full_name: str, default_branch: str, total_phases: int, final_test_summary=None) -> str:
    lines = [
        "## Project scaffold",
        "",
        "The fixed project template (FastAPI app skeleton, the `wp_compat` layer, test fixtures), pinned requirements and a "
        "CI workflow that runs the tests on every pull request. No generated migration code is in this pull request.",
        "",
        f"The migrated code follows in {int(total_phases)} more pull request{'' if total_phases == 1 else 's'}, one per migration phase, "
        "each stacked on the one before.",
        "",
        _stack_notice(default_branch, default_branch),
        "",
        _ADVISORY,
    ]
    summary = _summary_line(final_test_summary)
    if summary:
        lines += ["", summary]
    link = _app_link(execution_job_id)
    lines += ["", f"Migration `{exec_ref(execution_job_id)}`" + (f" · [open in the app]({link})" if link else f" · execution `{execution_job_id}`")]
    return "\n".join(lines) + "\n"


def phase_body(
    *,
    execution_job_id: str,
    phase_order: int,
    phase_label: Any,
    total_phases: int,
    base_branch: str,
    default_branch: str,
    units: list[Mapping[str, Any]],
    final_test_summary=None,
) -> str:
    attention = [u for u in units if u.get("status") != "passed" or "stubbed" in (u.get("flags") or [])]
    lines = [f"## Migration phase {int(phase_order)} of {int(total_phases)}: {code(phase_label)}", ""]
    if attention:
        lines += [
            f"**{len(attention)} of {len(units)} unit{'' if len(units) == 1 else 's'} in this phase need attention** "
            "(a failed verification, an error, or a stand-in that was generated because the unit could not be built):",
            "",
        ]
        for unit in attention[:_MAX_TABLE_ROWS]:
            slug = unit.get("unit_slug")
            slug_text = f"`{slug}`" if isinstance(slug, str) and UNIT_SLUG_PATTERN.match(slug) else code(slug)
            lines.append(f"- {slug_text} ({code(unit.get('label'))}): {_status_cell(unit.get('status'))}")
        lines.append("")
    lines += ["| Unit | Label | Status | Attempts | Flags |", "|---|---|---|---|---|"]
    for unit in units[:_MAX_TABLE_ROWS]:
        slug = unit.get("unit_slug")
        slug_text = f"`{slug}`" if isinstance(slug, str) and UNIT_SLUG_PATTERN.match(slug) else code(slug)
        lines.append(
            f"| {slug_text} | {code(unit.get('label'))} | {_status_cell(unit.get('status'))} | "
            f"{_int(unit.get('attempts'))} | {_flags_cell(unit.get('flags'))} |"
        )
    if len(units) > _MAX_TABLE_ROWS:
        lines.append(f"\n…and {len(units) - _MAX_TABLE_ROWS} more units not listed here.")
    lines += ["", _stack_notice(base_branch, default_branch), "", _ADVISORY]
    summary = _summary_line(final_test_summary)
    if summary:
        lines += ["", summary]
    link = _app_link(execution_job_id)
    lines += ["", f"Migration `{exec_ref(execution_job_id)}`" + (f" · [open in the app]({link})" if link else f" · execution `{execution_job_id}`")]
    return "\n".join(lines) + "\n"
