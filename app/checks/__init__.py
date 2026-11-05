"""Check library: wording in catalog.yaml, logic in rules.py, joined here into report results."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

CATALOG_FILE = Path(__file__).with_name("catalog.yaml")
MAX_EVIDENCE = 100

# Status values a check can end with. Only pass / warn / fail count towards the score.
STATUSES = ("fail", "warn", "pass", "info", "not_available", "error")


@dataclass
class Outcome:
    status: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    waste: float | None = None  # estimated USD per month


@lru_cache(maxsize=1)
def catalog() -> dict:
    return yaml.safe_load(CATALOG_FILE.read_text())


def _result(check_id: str, outcome: Outcome) -> dict:
    meta = catalog()["checks"][check_id]
    return {
        "id": check_id,
        "title": meta["title"],
        "category": meta["category"],
        "severity": meta["severity"],
        "status": outcome.status,
        "summary": outcome.summary,
        "evidence": outcome.evidence[:MAX_EVIDENCE],
        "evidence_total": len(outcome.evidence),
        "waste": round(outcome.waste, 2) if outcome.waste else None,
        "why": meta["why"],
        "fix": meta["fix"],
        "learn_more": meta.get("learn_more"),
        "plan_note": meta.get("plan_note"),
    }


def run_checks(snapshot: dict) -> list[dict]:
    """Run every check in catalog order against a tenant snapshot (see collector.py)."""
    from .rules import RULES

    unavailable = snapshot.get("unavailable", {})
    results = []
    for check_id in catalog()["checks"]:
        rule = RULES[check_id]
        missing = [section for section in rule.needs if snapshot.get(section) is None]
        if missing:
            reason = unavailable.get(missing[0]) or "We couldn't read this part of your tenant."
            outcome = Outcome("not_available", reason)
        else:
            try:
                outcome = rule.fn(snapshot)
            except Exception:  # one broken check must never break the report
                log.exception("Check %s failed", check_id)
                outcome = Outcome("error", "This check hit an unexpected error. We've logged it and will look into it.")
        results.append(_result(check_id, outcome))
    return results


DOMAIN_CHECKS = {"spf": "EML-001", "dmarc": "EML-002", "dkim": "EML-003"}


def results_for_domain(report: dict) -> list[dict]:
    """Turn a single dnscheck.check_domain() report into report results (used by the free check)."""
    results = []
    for key, check_id in DOMAIN_CHECKS.items():
        if report.get("error"):
            outcome = Outcome("error", "We couldn't look up this domain's DNS records just now. Please try again.")
        else:
            found = report["results"][key]
            evidence = [f"Record: {found['record']}"] if found.get("record") else []
            outcome = Outcome(found["status"], found["summary"], evidence + found.get("details", []))
        results.append(_result(check_id, outcome))
    return results
