"""Licences: which plan each customer is on, until when, and whether their scans keep running.

The Tenvaultix team manages licences and prices in the console (/admin). A customer record can be created before the
company connects (from its domain), so a sale can be set up first; when the company connects, its licence applies
instead of a free trial. Reports always stay visible; an expired or suspended licence only pauses new scans.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timezone

EXPIRING_DAYS = 7
DEFAULT_TRIAL_DAYS = 14
PLAN_ID = re.compile(r"^[a-z][a-z0-9-]{1,23}$")
CYCLES = {"monthly": "Monthly", "yearly": "Yearly"}
CTAS = {"trial": "Start free trial", "contact": "Talk to us"}

# Prices and plan details the site starts with. After the first start they live in the database and are changed
# in the console (Pricing); these are only used to fill an empty database.
DEFAULT_PLANS = [
    {"id": "trial", "name": "Free trial", "price_inr": 0, "price_usd": 0, "unit": "month", "max_users": None,
     "scan_days": 7, "tagline": "", "features": [], "public": False, "featured": False, "cta": "trial", "sort": 0},
    {"id": "essentials", "name": "Essentials", "price_inr": 1999, "price_usd": 39, "unit": "month", "max_users": 50,
     "scan_days": 7, "tagline": "", "public": True, "featured": False, "cta": "trial", "sort": 1,
     "features": ["All 20 checks, every week", "Money-wasted estimate", "Step-by-step fix guides",
                  "PDF report for insurers and auditors"]},
    {"id": "business", "name": "Business", "price_inr": 4999, "price_usd": 79, "unit": "month", "max_users": 200,
     "scan_days": 1, "tagline": "", "public": True, "featured": True, "cta": "trial", "sort": 2,
     "features": ["Everything in Essentials", "Daily scans", "History: what changed and when",
                  "Team access for your IT people", "soon: Instant email alerts"]},
    {"id": "partner", "name": "Partner", "price_inr": None, "price_usd": 15, "unit": "tenant/month",
     "max_users": None, "scan_days": 1, "tagline": "For IT providers", "public": True, "featured": False,
     "cta": "contact", "sort": 3,
     "features": ["Daily scans for each customer", "Reports you can share with customers",
                  "soon: All tenants on one screen", "soon: Your logo on reports"]},
]

STATES = {  # key: (label, tone used for the chip colour)
    "trial": ("Free trial", "info"),
    "active": ("Active", "good"),
    "expiring": ("Expiring soon", "warn"),
    "expired": ("Expired", "bad"),
    "trial_ended": ("Trial ended", "bad"),
    "suspended": ("Suspended", "bad"),
    "not_connected": ("Not connected yet", "na"),
}


def today() -> date:
    return datetime.now(timezone.utc).date()


def to_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def state(record: dict, on: date | None = None) -> dict:
    """Where a licence stands. `record` is a tenant joined with its account: plan, trial_ends, licence_status,
    paid_until (any may be missing)."""
    on = on or today()
    plan = record.get("plan") or "trial"
    ends = to_date(record.get("paid_until")) or (to_date(record.get("trial_ends")) if plan == "trial" else None)
    days_left = (ends - on).days if ends else None
    if record.get("licence_status") == "suspended":
        key = "suspended"
    elif ends and ends < on:
        key = "trial_ended" if plan == "trial" else "expired"
    elif plan == "trial":
        key = "trial"
    elif days_left is not None and days_left <= EXPIRING_DAYS:
        key = "expiring"
    else:
        key = "active"
    label, tone = STATES[key]
    return {"key": key, "label": label, "tone": tone, "plan": plan, "ends": ends,
            "days_left": max(0, days_left) if days_left is not None else None,
            "can_scan": key in ("trial", "active", "expiring")}


def monthly_value(record: dict, plan: dict | None) -> float:
    """What the customer pays per month in rupees (their agreed price, else the plan's list price), if paying."""
    if record.get("plan") in (None, "trial") or state(record)["key"] not in ("active", "expiring"):
        return 0.0
    if record.get("price_inr") is not None:
        return float(record["price_inr"])
    return float((plan or {}).get("price_inr") or 0)


def staff_count(meta: dict | None) -> int | None:
    """People who count against a plan's user limit: enabled, licensed staff from the last scan."""
    meta = meta or {}
    return (meta.get("facts") or {}).get("staff", meta.get("users"))


def seat_limit(record: dict, plan: dict | None) -> int | None:
    return record.get("seats") or (plan or {}).get("max_users")


def add_months(start: date, months: int) -> date:
    month = start.month - 1 + months
    year, month = start.year + month // 12, month % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


# ---------------------------------------------------------------- reading form fields

class FormError(ValueError):
    pass


def money(text: str | None, label: str) -> float | None:
    text = (text or "").replace(",", "").replace("₹", "").replace("$", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise FormError(f"{label} must be a number.") from None
    if value < 0 or value > 10_000_000:
        raise FormError(f"{label} must be between 0 and 1,00,00,000.")
    return round(value, 2)


def whole(text: str | None, label: str, low: int = 1, high: int = 1_000_000) -> int | None:
    text = (text or "").replace(",", "").strip()
    if not text:
        return None
    if not text.isdigit() or not low <= int(text) <= high:
        raise FormError(f"{label} must be a whole number from {low} to {high:,}.")
    return int(text)


def day(text: str | None, label: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    parsed = to_date(text)
    if parsed is None or len(text) != 10:
        raise FormError(f"{label} must be a date like 2026-12-31.")
    return parsed.isoformat()


def short(text: str | None, limit: int = 200) -> str | None:
    text = " ".join((text or "").split())
    return text[:limit] or None


def features(text: str | None) -> list[str]:
    """One feature per line; a line starting with "soon:" shows as coming soon."""
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    return [line[:120] for line in lines if line][:12]


def price_text(value, currency: str = "INR") -> str:
    """₹1,999 or $39 (no decimals for whole amounts); Indian digit grouping for rupees."""
    from .azure_costs import indian_grouping

    if value is None:
        return ""
    value = float(value)
    symbol = {"INR": "₹", "USD": "$"}.get(currency, currency + " ")
    if value.is_integer():
        digits = indian_grouping(int(value)) if currency == "INR" else f"{int(value):,}"
    else:
        digits = f"{value:,.2f}"
    return symbol + digits
