"""The "digital office" blueprint: the tenant drawn as an office building an inspector has marked up.

facts() pulls a few counts out of a snapshot (stored with each scan); build() combines them with the check
results into rooms, desks, keys and numbered inspector notes for the template (_blueprint.html).
"""
from __future__ import annotations

import re
from math import ceil

from .checks.rules import GLOBAL_ADMIN_TEMPLATE, NONCOMPLIANT, _inactive, admin_roles, staff
from .licenses import sku_price

MAX_DESKS = 72
MAX_KEYS = 8
MAX_DEVICES = 12
MAX_BADGES = 8

# Rooms of the office, the check category each one stands for, and how it's explained to an owner.
ROOMS = [
    {"key": "mailroom", "name": "Mailroom", "real": "Email security",
     "plain": "How letters leave the building, and whether they carry your official seal."},
    {"key": "equipment", "name": "Equipment room", "real": "Devices",
     "plain": "Company laptops and phones, and whether they follow the rules."},
    {"key": "files", "name": "File room", "real": "Sharing",
     "plain": "Where documents are kept, and who can carry them out."},
    {"key": "desks", "name": "Desks", "real": "Licenses & cost",
     "plain": "One desk for every license you pay for."},
    {"key": "keys", "name": "Master keys", "real": "Admin & apps",
     "plain": "Admin accounts: the keys that open every room."},
    {"key": "entrance", "name": "Front door", "real": "Sign-in & MFA",
     "plain": "How people get in: the lock on the door, and any back doors."},
    {"key": "reception", "name": "Reception", "real": "Accounts & guests",
     "plain": "Staff badges and visitor passes."},
]
ROOM_FOR_CATEGORY = {
    "Email security": "mailroom", "Devices": "equipment", "Sharing": "files", "Licenses & cost": "desks",
    "Admin & apps": "keys", "Sign-in & MFA": "entrance", "Accounts & guests": "reception",
}

# The inspector's note for each check, in office language. {n} etc. are filled from facts.
NOTES = {
    "MFA-002": {"fail": "The front door has no lock: nothing makes people use MFA.",
                "warn": "Only some doors are locked: MFA is required for some people, not everyone."},
    "MFA-001": {"fail": "{staff_without_mfa|person still walks|people still walk} in with just a password.",
                "warn": "{staff_without_mfa|person still walks|people still walk} in with just a password."},
    "MFA-003": {"fail": "A back door is open: old sign-in methods skip MFA."},
    "ADM-001": {"fail": "A master key is left under the mat: {admins_without_mfa|admin has|admins have} no MFA."},
    "ADM-002": {"fail": "Too many master keys: {global_admins|person|people} can open everything.",
                "warn": "Only one master key. If it's lost, nobody can get in."},
    "ADM-003": {"fail": "Anyone can hand a spare key to an outside app."},
    "ADM-004": {"warn": "Anyone can fit new locks (register apps)."},
    "ACC-001": {"fail": "Badges still work for {inactive_licensed|person who hasn't|people who haven't} been in for 90 days."},
    "ACC-002": {"fail": "Visitors can invite more visitors."},
    "ACC-003": {"fail": "{stale_guests|old visitor pass still opens|old visitor passes still open} the door."},
    "EML-001": {"fail": "No list of who may post letters in your name (SPF).",
                "warn": "The list of who may post your letters needs fixing (SPF)."},
    "EML-002": {"fail": "Fake letters with your letterhead get delivered (DMARC).",
                "warn": "Fake letters with your letterhead still get delivered (DMARC)."},
    "EML-003": {"fail": "Your letters go out without your seal (DKIM).",
                "warn": "Some letters go out without your seal (DKIM)."},
    "EML-004": {"fail": "Mail is being copied to someone outside the building."},
    "SHR-001": {"fail": "Files can be left out on the street with 'Anyone' links."},
    "LIC-001": {"fail": "{seats_unassigned|empty desk|empty desks} you still pay rent for."},
    "LIC-002": {"fail": "{blocked_licensed|desk is still kept for someone who has|desks are still kept for people who have} left."},
    "LIC-003": {"fail": "{inactive_licensed|desk nobody has|desks nobody has} sat at for 90 days."},
    "DEV-001": {"fail": "{devices_bad|device breaks|devices break} the building rules.",
                "warn": "{devices_grace|device|devices} will break the rules soon."},
}
COUNTED = re.compile(r"\{(\w+)\|([^|}]*)\|([^}]*)\}")
RANK = {"fail": 3, "warn": 2, "error": 1, "not_available": 1, "pass": 0, "info": 0}


def facts(snap: dict) -> dict:
    """Counts the drawing needs. Missing data simply leaves a key out."""
    f: dict = {}
    users = snap.get("users")
    skus = snap.get("skus")
    if skus is not None:
        def paid(sku: dict) -> bool:
            price = sku_price(sku["part_number"])
            return price is None and sku["enabled"] < 1000 or (price or 0) > 0
        bought = [s for s in skus if paid(s)]
        f["seats_bought"] = sum(s["enabled"] for s in bought)
        f["seats_unassigned"] = sum(max(0, s["enabled"] - s["consumed"]) for s in bought)
    if users is not None:
        people = staff(snap)
        f["staff"] = len(people)
        f["blocked_licensed"] = sum(
            1 for u in users if not u["enabled"] and any((sku_price(p) or 0) > 0 for p in u["license_skus"]))
        guests = [u for u in users if u["type"] == "Guest" and u["enabled"]]
        f["guests"] = len(guests)
        if snap.get("activity") is not None:
            f["inactive_licensed"] = len(_inactive(snap, people))
            if snap.get("activity_source") == "sign-in logs":
                f["stale_guests"] = len(_inactive(snap, guests))
        if snap.get("mfa") is not None:
            f["staff_without_mfa"] = sum(1 for u in people if u.get("mfa") is False)
        if snap.get("roles") is not None:
            holders = admin_roles(snap)
            ga = {m for r in snap["roles"]
                  if r.get("template_id") == GLOBAL_ADMIN_TEMPLATE or r["name"] == "Global Administrator"
                  for m in r["members"]}
            enabled = {u["id"]: u for u in users if u["enabled"]}
            f["global_admins"] = sum(1 for i in ga if i in enabled)
            if snap.get("mfa") is not None:
                f["global_admins_without_mfa"] = sum(1 for i in ga if i in enabled and enabled[i].get("mfa") is False)
                f["admins_without_mfa"] = sum(1 for i in holders if i in enabled and enabled[i].get("mfa") is False)
    if snap.get("forwarding") is not None:
        f["forwarding_rules"] = len(snap["forwarding"])
    if snap.get("sharepoint") is not None:
        f["anyone_links"] = snap["sharepoint"].get("sharing_capability") == "externalUserAndGuestSharing"
    if snap.get("devices") is not None:
        f["devices"] = len(snap["devices"])
        f["devices_bad"] = sum(1 for d in snap["devices"] if d["compliance"] in NONCOMPLIANT)
        f["devices_grace"] = sum(1 for d in snap["devices"] if d["compliance"] == "inGracePeriod")
    return f


def _note(check: dict, f: dict) -> str:
    template = NOTES.get(check["id"], {}).get(check["status"])
    if not template:
        return check["summary"]
    try:  # "{key|one thing|many things}" becomes "1 thing" / "3 things"
        return COUNTED.sub(lambda m: f"{f[m[1]]} {m[2] if f[m[1]] == 1 else m[3]}", template)
    except KeyError:  # a count we don't have (older scan): fall back to the check's own summary
        return check["summary"]


def _desks(f: dict) -> dict | None:
    if "seats_bought" not in f:
        return None
    total = f["seats_bought"]
    empty = f.get("seats_unassigned", 0)
    left = f.get("blocked_licensed", 0)
    idle = f.get("inactive_licensed", 0)
    used = max(0, total - empty - left - idle)
    per = max(1, ceil(total / MAX_DESKS))
    kinds = (["used"] * ceil(used / per) + ["idle"] * ceil(idle / per)
             + ["left"] * ceil(left / per) + ["empty"] * ceil(empty / per))
    cols = 18
    return {"total": total, "per_desk": per, "used": used, "idle": idle, "left": left, "empty": empty,
            "cells": [{"kind": k, "x": 92 + (i % cols) * 48, "y": 286 + (i // cols) * 46}
                      for i, k in enumerate(kinds[:MAX_DESKS])]}


def build(results: list[dict], f: dict | None) -> dict:
    f = f or {}
    by_room: dict[str, list[dict]] = {r["key"]: [] for r in ROOMS}
    for r in results:
        room = ROOM_FOR_CATEGORY.get(r["category"])
        if room:
            by_room[room].append(r)

    rooms, notes = {}, []
    for meta in ROOMS:
        checks = by_room[meta["key"]]
        worst = max((c["status"] for c in checks), key=lambda s: RANK.get(s, 0), default="pass")
        scored = [c for c in checks if c["status"] in ("pass", "warn", "fail")]
        status = worst if worst in ("fail", "warn") else ("pass" if scored else "na")
        rooms[meta["key"]] = {**meta, "status": status, "checks": checks}
    order = ["entrance", "keys", "mailroom", "files", "desks", "reception", "equipment"]
    for key in order:
        for c in sorted(rooms[key]["checks"], key=lambda c: -RANK.get(c["status"], 0)):
            if c["status"] in ("fail", "warn"):
                notes.append({"n": len(notes) + 1, "room": key, "status": c["status"], "text": _note(c, f),
                              "check_id": c["id"], "title": c["title"]})
    for key in rooms:
        rooms[key]["notes"] = [n["n"] for n in notes if n["room"] == key]

    status_of = {r["id"]: r["status"] for r in results}
    keys = None
    if "global_admins" in f:
        shown = min(f["global_admins"], MAX_KEYS)
        bad = min(f.get("global_admins_without_mfa", 0), shown)
        keys = {"count": f["global_admins"], "more": max(0, f["global_admins"] - MAX_KEYS),
                "items": [{"bad": i < bad, "x": 64 + i * 30} for i in range(shown)]}
    devices = None
    if "devices" in f:
        shown = min(f["devices"], MAX_DEVICES)
        bad, grace = f.get("devices_bad", 0), f.get("devices_grace", 0)
        devices = {"count": f["devices"], "more": max(0, f["devices"] - MAX_DEVICES),
                   "items": [{"state": "bad" if i < bad else "grace" if i < bad + grace else "ok",
                              "x": 352 + (i % 6) * 40, "y": 92 + (i // 6) * 38} for i in range(shown)]}
    badges = None
    if "guests" in f:
        shown = min(f["guests"], MAX_BADGES)
        stale = f.get("stale_guests", 0)
        badges = {"count": f["guests"], "more": max(0, f["guests"] - MAX_BADGES),
                  "items": [{"stale": i < stale, "x": 700 + (i % 4) * 34, "y": 528 + (i // 4) * 30}
                            for i in range(shown)]}
    return {
        "has_facts": bool(f),
        "rooms": rooms,
        "notes": notes,
        "facts": f,
        "desks": _desks(f),
        "keys": keys,
        "devices": devices,
        "badges": badges,
        "front_door": status_of.get("MFA-002", "na"),
        "back_door_open": status_of.get("MFA-003") == "fail",
        "mail_leak": status_of.get("EML-004") == "fail",
        "seals": [{"label": label, "status": status_of.get(cid, "na")}
                  for label, cid in (("SPF", "EML-001"), ("DMARC", "EML-002"), ("DKIM", "EML-003"))],
        "street_door": status_of.get("SHR-001") == "fail",
        "problems": len(notes),
    }
