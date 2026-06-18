"""Feedback rules dry-run evaluator.

Loads feedback_rules.yaml from skill directories and matches recent feedback
entries against `when` conditions via `pattern_type`. Outputs matches with
`would_execute` action, but does NOT execute any strategy changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from plugins.crypto_guard.logging_utils import get_logger
from plugins.crypto_guard.storage.migrations import check_schema_health
from plugins.crypto_guard.storage.repository import CryptoGuardRepository

LOGGER = get_logger("crypto_guard.feedback_rules_dry_run")

# Skills directory is relative to this file's location
SKILLS_DIR = Path(__file__).parent.parent / "skills"


def evaluate_feedback_rules_dry_run(
    repo: CryptoGuardRepository,
    *,
    lookback_days: int = 30,
) -> dict[str, Any]:
    """Evaluate feedback rules against recent feedback entries (dry-run only).

    Returns:
        {
            ok: bool,
            matches: [{skill, pattern_type, action, feedback_ids, would_execute}],
            summary: {total_matches, by_skill, by_pattern},
            rules_loaded: int,
            feedback_checked: int,
        }
    """
    # Check schema health first
    schema = check_schema_health()
    if not schema["ok"]:
        return {
            "ok": False,
            "error": "schema_unhealthy",
            "missing_columns": schema["missing_columns"],
        }

    # Load all feedback rules
    rules_by_skill = _load_feedback_rules()
    total_rules = sum(len(rules) for rules in rules_by_skill.values())

    # Get recent feedback entries
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat().replace("+00:00", "Z")
    feedback_entries = repo.conn.execute(
        """
        SELECT id, skill_name, pattern_type, finding, created_at
        FROM skill_feedback_memory
        WHERE datetime(created_at) >= datetime(?) AND pattern_type IS NOT NULL AND pattern_type != ''
        ORDER BY created_at DESC
        """,
        (cutoff,),
    ).fetchall()

    # Match feedback against rules
    matches: list[dict[str, Any]] = []
    for entry in feedback_entries:
        skill_name = entry["skill_name"]
        pattern_type = entry["pattern_type"]

        # Check rules for this skill
        skill_rules = rules_by_skill.get(skill_name, [])
        for rule in skill_rules:
            if rule["when"] == pattern_type:
                matches.append({
                    "skill": skill_name,
                    "pattern_type": pattern_type,
                    "action": rule["action"],
                    "feedback_id": entry["id"],
                    "feedback_finding": entry["finding"],
                    "feedback_created_at": entry["created_at"],
                    "would_execute": True,
                })

    # Aggregate by skill and pattern
    by_skill: dict[str, int] = {}
    by_pattern: dict[str, int] = {}
    for match in matches:
        by_skill[match["skill"]] = by_skill.get(match["skill"], 0) + 1
        by_pattern[match["pattern_type"]] = by_pattern.get(match["pattern_type"], 0) + 1

    return {
        "ok": True,
        "matches": matches,
        "summary": {
            "total_matches": len(matches),
            "by_skill": by_skill,
            "by_pattern": by_pattern,
        },
        "rules_loaded": total_rules,
        "feedback_checked": len(feedback_entries),
    }


def _load_feedback_rules() -> dict[str, list[dict[str, str]]]:
    """Load all feedback_rules.yaml from skill directories."""
    rules_by_skill: dict[str, list[dict[str, str]]] = {}

    if not SKILLS_DIR.exists():
        LOGGER.warning("Skills directory not found: %s", SKILLS_DIR)
        return rules_by_skill

    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue

        # Normalize skill name (remove _skill suffix if present)
        skill_name = skill_dir.name.replace("_skill", "")

        rules_file = skill_dir / "feedback_rules.yaml"
        if not rules_file.exists():
            continue

        try:
            with open(rules_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)

            if not data or "feedback_rules" not in data:
                continue

            rules = data["feedback_rules"]
            if not isinstance(rules, list):
                continue

            parsed_rules: list[dict[str, str]] = []
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                when = rule.get("when")
                action = rule.get("action")
                if when and action:
                    parsed_rules.append({"when": str(when), "action": str(action)})

            if parsed_rules:
                # Merge rules: append to existing rules for same skill name
                if skill_name in rules_by_skill:
                    existing_whens = {r["when"] for r in rules_by_skill[skill_name]}
                    for rule in parsed_rules:
                        if rule["when"] not in existing_whens:
                            rules_by_skill[skill_name].append(rule)
                            existing_whens.add(rule["when"])
                    LOGGER.debug("Merged %d rules from %s (total %d)", len(parsed_rules), skill_dir.name, len(rules_by_skill[skill_name]))
                else:
                    rules_by_skill[skill_name] = parsed_rules
                    LOGGER.debug("Loaded %d rules from %s", len(parsed_rules), skill_dir.name)

        except Exception as exc:
            LOGGER.warning("Failed to load feedback_rules.yaml from %s: %s", skill_dir.name, exc)

    return rules_by_skill
