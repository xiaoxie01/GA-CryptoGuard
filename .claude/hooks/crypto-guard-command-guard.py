#!/usr/bin/env python3
"""Guard high-risk CryptoGuard shell operations behind short-lived approval."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
APPROVAL_PATH = REPO_ROOT / ".trellis" / ".runtime" / "crypto-guard-approval.json"
TOKEN_RE = re.compile(r"crypto-guard-approval:([A-Za-z0-9_-]+)")
NON_PRODUCTION_DB_RE = re.compile(
    r"crypto-guard-non-production-db:([^\s#]+)",
    re.IGNORECASE,
)

_DANGEROUS_GIT = (
    re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clean\s+-[A-Za-z]*[fd][A-Za-z]*\b", re.IGNORECASE),
    re.compile(r"\bgit\s+checkout\s+--\s+", re.IGNORECASE),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_read_only_command(command: str) -> bool:
    stripped = command.strip().lower()
    prefixes = (
        "rg ",
        "grep ",
        "get-content ",
        "select-string ",
        "git diff",
        "git status",
        "git show",
        "python -m pytest",
        "pytest ",
    )
    return stripped.startswith(prefixes)


def _declares_non_production_db(command: str) -> bool:
    """Allow explicit temp-DB repros without weakening production guards."""
    match = NON_PRODUCTION_DB_RE.search(command)
    if not match or "crypto_guard_db" not in command.lower():
        return False

    raw_path = match.group(1).strip("'\"")
    try:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return False

    protected_roots = (
        (REPO_ROOT / "data" / "crypto_guard").resolve(),
        (REPO_ROOT / "plugins" / "crypto_guard" / "data").resolve(),
    )
    if any(candidate == root or candidate.is_relative_to(root) for root in protected_roots):
        return False
    return raw_path.lower() in command.lower()


def _split_segments(command: str) -> list[str]:
    """Split a command line on shell separators that sit OUTSIDE quotes.

    Separators: ``&&``, ``||``, ``;``, ``|`` and newlines. Quotes (single,
    double, backtick, and PowerShell here-string openers ``@'``/``@\"``) are
    respected so ``python -c "import x; initialize_database()"`` stays one
    segment — splitting inside a quoted string would drop the mutation token
    and open a bypass (08-02 review P2-C).
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            elif quote == '"' and ch == "`" and i + 1 < n:
                # PowerShell backtick escape inside double quotes.
                current.append(command[i + 1])
                i += 1
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "@" and nxt in ("'", '"'):
            # PowerShell here-string opener.
            quote = nxt
            current.append(ch)
            current.append(nxt)
            i += 2
            continue
        if ch in "\r\n":
            if current:
                segments.append("".join(current))
                current = []
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            if current:
                segments.append("".join(current))
                current = []
            i += 2
            continue
        if ch in ";|":
            if current:
                segments.append("".join(current))
                current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if current:
        segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def classify_command(command: str) -> set[str]:
    """Return operation classes requiring a guard.

    Compound commands (``&&``, ``||``, ``;``, ``|``, newline) are split into
    independently-classified segments and the union decides blocking. This
    prevents a ``pytest`` or read-only segment from exempting a sibling
    ``python -c "initialize_database()"`` segment in the same command line
    (08-02 review P2-C).
    """
    if any(pattern.search(command) for pattern in _DANGEROUS_GIT):
        return {"dangerous-git"}

    operations: set[str] = set()
    for segment in _split_segments(command):
        operations |= _classify_segment(segment, command)
    return operations


def _classify_segment(segment: str, full_command: str) -> set[str]:
    """Classify one shell segment; ``full_command`` carries the temp-DB marker."""
    if _is_read_only_command(segment):
        return set()

    lower = segment.lower()
    operations: set[str] = set()

    runs_python = bool(re.search(r"\bpython(?:w|3)?(?:\.exe)?\b", lower))
    # 08-02 Finding 3 (P2): the bare-``pytest`` matcher previously accepted any
    # whitespace separator (``\s``), so an inert trailing shell comment
    # ``# pytest`` flipped ``runs_pytest`` to True and exempted the whole
    # inline ``python -c ...`` mutation from classification. Only a real test
    # run matches now: segment start, or an actual shell separator (``;``,
    # ``&&``, ``||``, ``|``) — a whitespace-only ``# pytest`` comment is dead
    # shell syntax and must NOT count. ``python -m pytest`` (first branch) is
    # unaffected. ``_split_segments`` already strips each segment, so a lone
    # ``pytest`` at segment start is covered by ``^``.
    #
    # 08-02 fresh-reviewer P2: the ``(?:^|[;&|])pytest`` branch ALSO matched a
    # ``;pytest`` INSIDE a quoted ``python -c "..."`` string (quote-aware
    # ``_split_segments`` keeps the in-string ``;`` inside the segment, so it is
    # python code, not a shell separator). That flipped ``runs_pytest`` and let
    # an inline mutation escape classification. Real separators were already
    # split into their own segments, so segment-start ``^pytest`` is the exact
    # and only correct anchor — an in-string ``;pytest`` no longer exempts the
    # sibling mutation.
    runs_pytest = bool(
        re.search(r"\bpython(?:3)?(?:\.exe)?\s+-m\s+pytest\b", lower)
        or re.search(r"^pytest(?:\.exe)?\b", lower)
    )
    production_db = "crypto_guard.sqlite3" in lower or "crypto_guard.db" in lower
    mutating_sql = bool(
        re.search(
            r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|"
            r"vacuum|reindex|replace|cluster|refresh\s+materialized|"
            r"comment\s+on|security\s+label)\b",
            lower,
        )
    )
    non_production_db = _declares_non_production_db(full_command)

    if (
        runs_python
        and not runs_pytest
        and "initialize_database" in lower
        and not non_production_db
    ):
        operations.add("database-mutation")
    if (
        runs_python
        and not runs_pytest
        and "repair_" in lower
        and "crypto_guard" in lower
        and not non_production_db
    ):
        operations.add("database-mutation")
    if production_db and mutating_sql:
        operations.add("database-mutation")
    if production_db and re.search(
        r"\b(remove-item|del|erase|rm)\b",
        lower,
    ):
        operations.add("database-mutation")

    # PostgreSQL administrative clients. ``pg_dump`` and ``pg_restore --list``
    # are read-only evidence commands; an actual restore and all role/database
    # administration are production mutations. A psql script file is treated
    # as mutating because the guard cannot prove the file contains SELECT only.
    pg_admin_tool = bool(
        re.search(
            r"\b(createdb|dropdb|createuser|dropuser|vacuumdb|reindexdb)"
            r"(?:\.exe)?\b",
            lower,
        )
    )
    runs_psql = bool(re.search(r"\bpsql(?:\.exe)?\b", lower))
    psql_file = runs_psql and bool(
        re.search(r"(?:^|\s)(?:-f|--file(?:=|\s))", lower)
        or re.search(r"\\i[r]?\s+[^\r\n]+\.sql\b", lower)
        or re.search(r"<\s*[^\r\n]+\.sql\b", lower)
    )
    runs_pg_restore = bool(re.search(r"\bpg_restore(?:\.exe)?\b", lower))
    pg_restore_list_only = runs_pg_restore and bool(
        re.search(r"(?:^|\s)(?:-l\b|--list\b)", lower)
    )
    if pg_admin_tool or psql_file or (runs_psql and mutating_sql):
        operations.add("database-mutation")
    if runs_pg_restore and not pg_restore_list_only:
        operations.add("database-mutation")

    # Release helpers often use inline psycopg instead of psql so secrets can
    # stay in environment variables. The transport does not change the side
    # effect: inline PostgreSQL DDL/DML still requires approval. Pytest command
    # names may contain words such as "create", so test runners are excluded.
    if (
        runs_python
        and not runs_pytest
        and mutating_sql
        and re.search(r"\b(psycopg|postgres(?:ql)?|crypto_guard)\b", lower)
        and not non_production_db
    ):
        operations.add("database-mutation")

    # Persisting a production DSN changes the next service launch contract and
    # may expose credentials in user/machine configuration. Per-process $env:
    # assignments are intentionally not matched; they are the approved secret
    # injection mechanism for a single guarded release command.
    persistent_database_env = bool(
        re.search(
            r"\bsetx(?:\.exe)?\b[^\r\n]*(?:crypto_guard_database_url|"
            r"crypto_guard_migration_database_url|crypto_guard_replay_database_url)",
            lower,
        )
        or (
            "setenvironmentvariable" in lower
            and re.search(
                r"crypto_guard_(?:migration_|replay_)?database_url", lower
            )
            and re.search(r"['\"](?:user|machine)['\"]", lower)
        )
        or (
            re.search(r"\breg(?:\.exe)?\s+add\b", lower)
            and re.search(
                r"crypto_guard_(?:migration_|replay_)?database_url", lower
            )
        )
        or (
            "set-itemproperty" in lower
            and "environment" in lower
            and re.search(
                r"crypto_guard_(?:migration_|replay_)?database_url", lower
            )
        )
    )
    if persistent_database_env:
        operations.add("service-control")

    # Match Windows PostgreSQL service names (postgresql-x64-17) and the
    # fsapp/hub launchers. The bare ``postgres``/``postgresql`` token is NOT
    # a service target here: inside an inline ``python -c`` admin command it
    # is a psycopg connection user (``user='postgres'``), not a Windows
    # service. A bare service name only counts under an explicit
    # service-manager verb or ``Start-Process`` (see ``postgres_service_name``
    # below). The URI scheme ``postgresql://`` is never a service target.
    service_target = bool(
        re.search(
            r"\b(frontends[./\\])?(fsapp(?:\.py)?|hub(?:\.pyw)?|"
            r"postgresql-x64-\d+)\b",
            lower,
        )
    )
    postgres_service_name = bool(
        re.search(r"\bpostgres(?:ql)?(?!://)\b", lower)
    )
    service_start = bool(
        re.search(
            r"\b(start-process|pythonw?|py)\b",
            lower,
        )
    )
    service_stop = bool(re.search(r"\b(stop-process|taskkill)\b", lower))
    service_manager = bool(
        re.search(r"\b(start-service|stop-service|restart-service)\b", lower)
        or re.search(r"\bnet(?:\.exe)?\s+(?:start|stop)\b", lower)
        or re.search(r"\bsc(?:\.exe)?\s+(?:start|stop)\b", lower)
    )
    explicit_service_launch = bool(re.search(r"\bstart-process\b", lower))
    if (
        service_stop
        or (service_target and (service_start or service_manager))
        or (
            postgres_service_name
            and (service_manager or explicit_service_launch)
        )
    ):
        operations.add("service-control")

    return operations


def _load_approval(path: Path = APPROVAL_PATH) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_approval(payload: dict[str, Any], path: Path = APPROVAL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def authorize(
    operations: list[str],
    task: str,
    ttl_minutes: int,
    uses: int,
    path: Path = APPROVAL_PATH,
) -> str:
    allowed = {"database-mutation", "service-control"}
    requested = sorted(set(operations))
    if not requested or any(operation not in allowed for operation in requested):
        raise ValueError("operation must be database-mutation or service-control")
    if not task.strip():
        raise ValueError("task is required")
    if ttl_minutes < 1 or ttl_minutes > 60:
        raise ValueError("ttl-minutes must be between 1 and 60")
    if uses < 1 or uses > 50:
        raise ValueError("uses must be between 1 and 50")

    now = _utc_now()
    token = secrets.token_urlsafe(24)
    _write_approval(
        {
            "token": token,
            "task": task,
            "operations": requested,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
            "remaining_uses": uses,
        },
        path,
    )
    return token


def revoke(path: Path = APPROVAL_PATH) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _approval_allows(
    command: str,
    operations: set[str],
    expected_task: str | None = None,
    path: Path | None = None,
) -> tuple[bool, str]:
    path = path or APPROVAL_PATH
    match = TOKEN_RE.search(command)
    if not match:
        return False, "missing crypto-guard approval token"

    approval = _load_approval(path)
    if not approval or not secrets.compare_digest(
        str(approval.get("token", "")),
        match.group(1),
    ):
        return False, "approval token is invalid"

    try:
        expires_at = datetime.fromisoformat(str(approval["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False, "approval metadata is invalid"
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if _utc_now() >= expires_at:
        revoke(path)
        return False, "approval token has expired"

    approved_operations = set(approval.get("operations") or [])
    if not operations.issubset(approved_operations):
        return False, "approval does not cover this operation"
    if not expected_task:
        return False, "active Trellis task could not be resolved"
    if str(approval.get("task", "")).replace("\\", "/").rstrip("/") != (
        expected_task.replace("\\", "/").rstrip("/")
    ):
        return False, "approval belongs to a different Trellis task"

    remaining_uses = approval.get("remaining_uses")
    if type(remaining_uses) is not int or remaining_uses <= 0:
        revoke(path)
        return False, "approval has no remaining uses"

    approval["remaining_uses"] = remaining_uses - 1
    if approval["remaining_uses"] == 0:
        revoke(path)
    else:
        _write_approval(approval, path)
    return True, ""


def _deny(reason: str) -> None:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output, ensure_ascii=False))


def _resolve_active_task(input_data: dict[str, Any]) -> str | None:
    scripts_dir = REPO_ROOT / ".trellis" / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    try:
        from common.active_task import resolve_active_task

        active = resolve_active_task(REPO_ROOT, input_data, platform="claude")
    except Exception:
        return None
    return active.task_path


def evaluate_hook(input_data: dict[str, Any]) -> tuple[bool, str]:
    tool_input = input_data.get("tool_input")
    if not isinstance(tool_input, dict):
        return True, ""
    command = tool_input.get("command")
    if not isinstance(command, str):
        return True, ""

    operations = classify_command(command)
    if not operations:
        return True, ""
    if "dangerous-git" in operations:
        return False, (
            "Blocked destructive Git command. Preserve the dirty worktree and use "
            "a non-destructive, path-scoped operation."
        )

    allowed, reason = _approval_allows(
        command,
        operations,
        expected_task=_resolve_active_task(input_data),
    )
    if allowed:
        return True, ""
    operation_text = ", ".join(sorted(operations))
    return False, (
        f"Blocked CryptoGuard production operation ({operation_text}): {reason}. "
        "Use /trellis:crypto-guard-release, obtain explicit user confirmation, then create "
        "a short-lived task-bound approval. For an isolated reproduction, set "
        "CRYPTO_GUARD_DB to an external temporary path and append "
        "# crypto-guard-non-production-db:<same-path>."
    )


def _hook_main() -> int:
    if os.environ.get("CRYPTO_GUARD_COMMAND_GUARD") == "0":
        return 0
    try:
        input_data = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    allowed, reason = evaluate_hook(input_data)
    if not allowed:
        _deny(reason)
    return 0


def _cli_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    authorize_parser = subparsers.add_parser("authorize")
    authorize_parser.add_argument(
        "--operation",
        action="append",
        required=True,
        choices=("database-mutation", "service-control"),
    )
    authorize_parser.add_argument("--task", required=True)
    authorize_parser.add_argument("--ttl-minutes", type=int, default=15)
    authorize_parser.add_argument("--uses", type=int, default=12)

    subparsers.add_parser("status")
    subparsers.add_parser("revoke")
    args = parser.parse_args(argv)

    if args.action == "authorize":
        token = authorize(
            args.operation,
            args.task,
            args.ttl_minutes,
            args.uses,
        )
        print(f"crypto-guard-approval:{token}")
        return 0
    if args.action == "status":
        approval = _load_approval()
        print(json.dumps(approval or {"active": False}, indent=2))
        return 0
    revoke()
    print("CryptoGuard production approval revoked.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(_cli_main(sys.argv[1:]))
    raise SystemExit(_hook_main())
