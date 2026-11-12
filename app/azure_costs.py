"""Azure spend for a connected company, read from Azure Cost Management.

Read-only and app-only, like the Microsoft 365 scan, but it needs no extra Graph permission: the company gives the
Tenvaultix app the "Cost Management Reader" role on its Azure subscriptions (or on the Tenant Root management group),
and Microsoft then lets the app read costs for those subscriptions and nothing else.

Azure updates cost data a few times a day (usually within 8-24 hours for EA/MCA, up to 72 hours for pay-as-you-go), so
"live" here means the latest figures Microsoft has. Results are cached for an hour.
"""
from __future__ import annotations

import calendar
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from .graph import AuthError, GraphError

log = logging.getLogger(__name__)

ARM_ROOT = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
SUBSCRIPTIONS_API = "2022-12-01"
COST_API = "2023-03-01"
MAX_SUBSCRIPTIONS = 25      # keep one refresh quick and inside Cost Management's rate limits
TOP_SERVICES = 8
FRESH_FOR = timedelta(hours=1)
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "SGD": "S$", "AED": "AED "}

QUERY = {
    "type": "ActualCost",
    "timeframe": "MonthToDate",
    "dataset": {
        "granularity": "Daily",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        "grouping": [{"type": "Dimension", "name": "ServiceName"}],
    },
}


# ---------------------------------------------------------------- reading Microsoft's answer

def _day(value) -> str | None:
    """UsageDate comes back as a number like 20261105 (or sometimes an ISO string)."""
    text = str(value)
    if text.isdigit() and len(text) == 8:
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def parse_rows(properties: dict) -> list[dict]:
    """Cost Management rows -> [{date, service, cost, currency}]. Column order isn't fixed, so go by name."""
    names = [c.get("name", "") for c in properties.get("columns") or []]
    lower = [n.lower() for n in names]

    def find(*options):
        return next((lower.index(o) for o in options if o in lower), None)

    i_date, i_service, i_currency = find("usagedate"), find("servicename"), find("currency")
    i_cost = find("cost", "pretaxcost", "costusd", "totalcost")
    if i_cost is None:  # fall back to the first number column that isn't the date
        numeric = [i for i, c in enumerate(properties.get("columns") or [])
                   if c.get("type") == "Number" and i != i_date]
        i_cost = numeric[0] if numeric else None
    if i_cost is None or i_date is None:
        return []
    rows = []
    for row in properties.get("rows") or []:
        day = _day(row[i_date])
        if day is None:
            continue
        rows.append({
            "date": day,
            "service": (row[i_service] if i_service is not None else None) or "Other",
            "cost": float(row[i_cost] or 0),
            "currency": (row[i_currency] if i_currency is not None else None) or "",
        })
    return rows


def _query(arm, subscription_id: str) -> list[dict]:
    path = f"/subscriptions/{subscription_id}/providers/Microsoft.CostManagement/query"
    data = arm.post(path, QUERY, {"api-version": COST_API})
    rows = parse_rows(data.get("properties") or {})
    pages = 0
    while (data.get("properties") or {}).get("nextLink") and pages < 20:  # same body, posted to the next page
        data = arm.post(data["properties"]["nextLink"], QUERY)
        rows += parse_rows(data.get("properties") or {})
        pages += 1
    return rows


# ---------------------------------------------------------------- fetching and summarising

def fetch(arm, today: date | None = None) -> dict:
    """Read this month's Azure costs for every subscription the app can see. Never raises for Microsoft errors."""
    today = today or datetime.now(timezone.utc).date()
    try:
        subscriptions = arm.get_all("/subscriptions", {"api-version": SUBSCRIPTIONS_API})
    except AuthError as exc:
        return {"state": "error", "code_text": str(exc), "error": "Microsoft wouldn't give the app access to Azure."}
    except GraphError as exc:
        if exc.forbidden:
            return {"state": "no_access"}
        return {"state": "error", "code_text": str(exc), "error": f"Azure returned an error ({exc.status})."}
    if not subscriptions:
        # A service principal only sees subscriptions it has a role on, so "none" usually means "not shared yet".
        return {"state": "no_access"}

    subs, rows = [], []
    for sub in subscriptions[:MAX_SUBSCRIPTIONS]:
        entry = {"id": sub.get("subscriptionId", ""), "name": sub.get("displayName") or sub.get("subscriptionId"),
                 "status": "ok", "cost": 0.0, "currency": ""}
        try:
            sub_rows = _query(arm, entry["id"])
        except GraphError as exc:
            entry["status"] = "no_access" if exc.forbidden else "error"
            entry["error"] = f"{exc.status} {exc.code}"
            log.info("Azure costs for subscription %s: %s", entry["id"], exc)
        else:
            entry["cost"] = sum(r["cost"] for r in sub_rows)
            entry["currency"] = Counter(r["currency"] for r in sub_rows).most_common(1)[0][0] if sub_rows else ""
            rows += sub_rows
        subs.append(entry)

    readable = [s for s in subs if s["status"] == "ok"]
    if not readable:
        state = "no_access" if all(s["status"] == "no_access" for s in subs) else "error"
        return {"state": state, "subscriptions": subs,
                "error": "Azure didn't return costs for any subscription." if state == "error" else ""}
    return summarise(rows, subs, today, skipped=max(0, len(subscriptions) - MAX_SUBSCRIPTIONS))


def summarise(rows: list[dict], subs: list[dict], today: date, skipped: int = 0) -> dict:
    by_currency: dict[str, float] = defaultdict(float)
    for r in rows:
        by_currency[r["currency"]] += r["cost"]
    currency = max(by_currency, key=by_currency.get) if by_currency else (subs[0]["currency"] if subs else "")
    main = [r for r in rows if r["currency"] == currency]

    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    per_day: dict[str, float] = defaultdict(float)
    per_service: dict[str, float] = defaultdict(float)
    for r in main:
        if r["date"] >= month_start.isoformat():
            per_day[r["date"]] += r["cost"]
            per_service[r["service"]] += r["cost"]
    total = sum(per_day.values())
    last_day = max(per_day) if per_day else None

    days = [{"date": (month_start + timedelta(days=i)).isoformat(),
             "cost": round(per_day.get((month_start + timedelta(days=i)).isoformat(), 0.0), 2)}
            for i in range(days_in_month)]
    ranked = sorted(per_service.items(), key=lambda kv: kv[1], reverse=True)
    services = [{"name": n, "cost": round(c, 2)} for n, c in ranked[:TOP_SERVICES]]
    rest = sum(c for _, c in ranked[TOP_SERVICES:])
    if rest > 0:
        services.append({"name": f"{len(ranked) - TOP_SERVICES} other services", "cost": round(rest, 2)})

    days_with_data = int(last_day[-2:]) if last_day else 0
    projected = round(total / days_with_data * days_in_month, 2) if days_with_data >= 3 else None
    return {
        "state": "ok",
        "month": month_start.strftime("%B %Y"),
        "currency": currency,
        "total": round(total, 2),
        "projected": projected,
        "last_day": last_day,
        "days": days,
        "services": services,
        "subscriptions": sorted(subs, key=lambda s: s["cost"], reverse=True),
        "other_currencies": {c: round(v, 2) for c, v in by_currency.items() if c != currency and v},
        "skipped": skipped,
    }


def is_fresh(cached: dict | None, now: datetime | None = None) -> bool:
    if not cached:
        return False
    fetched = datetime.fromisoformat(cached["fetched_at"])
    return (now or datetime.now(timezone.utc)) - fetched < FRESH_FOR


# ---------------------------------------------------------------- showing it

def indian_grouping(n: int) -> str:
    """12345678 -> 1,23,45,678 (Indian digit grouping)."""
    text = str(n)
    if len(text) <= 3:
        return text
    head, tail = text[:-3], text[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def fmt_money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "–"
    symbol = SYMBOLS.get(currency, f"{currency} " if currency else "")
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 100:
        whole = int(round(value))
        digits = indian_grouping(whole) if currency == "INR" else f"{whole:,}"
        return f"{sign}{symbol}{digits}"
    return f"{sign}{symbol}{value:,.2f}"


def chart(days: list[dict], width: int = 720, height: int = 220) -> dict:
    """Geometry for the daily bar chart (an SVG drawn by the template; no scripts needed)."""
    left, right, top, bottom = 56, 8, 12, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    peak = max((d["cost"] for d in days), default=0)
    step = _nice_step(peak / 4) if peak > 0 else 1
    ceiling = step * max(1, -(-peak // step))  # round the top up to a whole step
    slot = plot_w / max(1, len(days))
    bar_w = min(24.0, max(2.0, slot - 2))  # 2px gap between neighbouring bars, never thicker than 24px
    bars = []
    for i, d in enumerate(days):
        h = 0 if ceiling == 0 else d["cost"] / ceiling * plot_h
        bars.append({"x": round(left + i * slot + (slot - bar_w) / 2, 2), "y": round(top + plot_h - h, 2),
                     "w": round(bar_w, 2), "h": round(h, 2), "date": d["date"], "cost": d["cost"],
                     "label": str(int(d["date"][-2:])), "show_label": i == 0 or (i + 1) % 5 == 0})
    ticks = [{"y": round(top + plot_h - (v / ceiling * plot_h), 2), "value": v}
             for v in [step * k for k in range(int(ceiling // step) + 1)]] if peak > 0 else []
    return {"width": width, "height": height, "left": left, "right": width - right, "top": top,
            "base": top + plot_h, "bars": bars, "ticks": ticks}


def _nice_step(raw: float) -> float:
    magnitude = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * magnitude:
            return m * magnitude
    return 10 * magnitude


def compact(value: float, currency: str = "") -> str:
    """Axis labels: 0, 500, 1K, 2.5K, 1L (lakh) for rupees, 1M otherwise."""
    symbol = SYMBOLS.get(currency, "")
    if currency == "INR" and value >= 100000:
        return f"{symbol}{value / 100000:g}L"
    if value >= 1_000_000:
        return f"{symbol}{value / 1_000_000:g}M"
    if value >= 1000:
        return f"{symbol}{value / 1000:g}K"
    return f"{symbol}{value:g}"
