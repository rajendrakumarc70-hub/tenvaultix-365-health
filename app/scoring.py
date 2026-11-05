"""Turns check results into a 0-100 health score, category scores, counts and money wasted."""
from __future__ import annotations

from .checks import catalog

WEIGHTS = {"critical": 10, "high": 6, "medium": 3, "low": 1}
CREDIT = {"pass": 1.0, "warn": 0.5, "fail": 0.0}
SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]
STATUS_ORDER = ["fail", "warn", "error", "not_available", "info", "pass"]


def grade(score: int | None) -> str:
    if score is None:
        return "-"
    for limit, letter in ((85, "A"), (70, "B"), (55, "C"), (35, "D")):
        if score >= limit:
            return letter
    return "F"


def _score(results: list[dict]) -> int | None:
    scored = [r for r in results if r["status"] in CREDIT and r["severity"] in WEIGHTS]
    total = sum(WEIGHTS[r["severity"]] for r in scored)
    if not total:
        return None
    earned = sum(WEIGHTS[r["severity"]] * CREDIT[r["status"]] for r in scored)
    return round(100 * earned / total)


def is_problem(result: dict) -> bool:
    return result["status"] in ("fail", "warn")


def summarize(results: list[dict]) -> dict:
    score = _score(results)
    categories = []
    for name in catalog()["categories"]:
        in_cat = [r for r in results if r["category"] == name]
        if not in_cat:
            continue
        cat_score = _score(in_cat)
        categories.append({
            "name": name,
            "score": cat_score,
            "grade": grade(cat_score),
            "problems": sum(1 for r in in_cat if is_problem(r)),
            "checks": len(in_cat),
        })
    counts = {status: sum(1 for r in results if r["status"] == status) for status in STATUS_ORDER}
    return {
        "score": score,
        "grade": grade(score),
        "categories": categories,
        "counts": counts,
        "problems": counts["fail"] + counts["warn"],
        "urgent": sum(1 for r in results if is_problem(r) and r["severity"] in ("critical", "high")),
        "waste": round(sum(r.get("waste") or 0 for r in results), 2),
    }


GROUP = {"fail": 0, "warn": 0, "error": 1, "not_available": 1, "info": 2, "pass": 3}


def sort_for_report(results: list[dict]) -> list[dict]:
    """Problems first, most severe at the top (a critical warning outranks a low failure); passes last."""
    return sorted(results, key=lambda r: (GROUP[r["status"]], SEVERITY_ORDER.index(r["severity"]),
                                          STATUS_ORDER.index(r["status"])))


def compare(previous: list[dict] | None, current: list[dict]) -> dict:
    """What changed since the last scan: new problems and fixed ones, by check title."""
    if not previous:
        return {"new": [], "fixed": [], "first_scan": True}
    before = {r["id"]: r for r in previous}
    new, fixed = [], []
    for r in current:
        old = before.get(r["id"])
        if old is None:
            continue
        if is_problem(r) and old["status"] == "pass":
            new.append(r["title"])
        elif r["status"] == "pass" and is_problem(old):
            fixed.append(r["title"])
    return {"new": new, "fixed": fixed, "first_scan": False}
