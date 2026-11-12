"""The Tenvaultix console: licences, customer records, pricing, backups, and what customers see as a result."""
import json
import re
from datetime import date, timedelta

import pytest

from app import licensing
from app.db import Store
from app.scanner import can_scan, scan_due
from app.web import create_app

from .fakes import TENANT, FakeGraph, make_settings, tenant_resolver
from .test_web import ADMIN, STAFF, World

NEWCO = "22222222-3333-4444-5555-666666666666"
TODAY = date(2026, 11, 12)


def day(offset: int) -> str:
    return (licensing.today() + timedelta(days=offset)).isoformat()


# ---------------------------------------------------------------- licence rules

@pytest.mark.parametrize("record,key,can", [
    ({"plan": "trial", "trial_ends": "2026-11-20T00:00:00+00:00"}, "trial", True),
    ({"plan": "trial", "trial_ends": "2026-11-01T00:00:00+00:00"}, "trial_ended", False),
    ({"plan": "business"}, "active", True),
    ({"plan": "business", "paid_until": "2026-11-15"}, "expiring", True),
    ({"plan": "business", "paid_until": "2026-12-31"}, "active", True),
    ({"plan": "business", "paid_until": "2026-11-11"}, "expired", False),
    ({"plan": "business", "paid_until": "2026-11-12"}, "expiring", True),  # valid through its last day
    ({"plan": "business", "licence_status": "suspended"}, "suspended", False),
    ({"plan": "trial", "trial_ends": "2026-11-01", "paid_until": "2026-12-01"}, "trial", True),  # trial extended
])
def test_licence_states(record, key, can):
    st = licensing.state(record, on=TODAY)
    assert st["key"] == key and st["can_scan"] is can


def test_helpers():
    assert licensing.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert licensing.add_months(date(2026, 11, 12), 12) == date(2027, 11, 12)
    assert licensing.price_text(199999, "INR") == "₹1,99,999" and licensing.price_text(39, "USD") == "$39"
    assert licensing.price_text(12.5, "USD") == "$12.50" and licensing.price_text(None) == ""
    assert licensing.money("₹1,999", "Price") == 1999 and licensing.money("", "Price") is None
    assert licensing.features("A\n\n  soon:  B  \n") == ["A", "soon: B"]
    assert licensing.staff_count({"facts": {"staff": 12}, "users": 40}) == 12
    for bad in (lambda: licensing.money("abc", "Price"), lambda: licensing.money("-5", "Price"),
                lambda: licensing.whole("2.5", "Seats"), lambda: licensing.day("31/12/2026", "Valid until")):
        with pytest.raises(licensing.FormError):
            bad()


def test_monthly_value_uses_agreed_price_only_while_paying():
    plan = {"price_inr": 1999}
    assert licensing.monthly_value({"plan": "essentials"}, plan) == 1999
    assert licensing.monthly_value({"plan": "essentials", "price_inr": 1500}, plan) == 1500
    assert licensing.monthly_value({"plan": "essentials", "paid_until": "2020-01-01"}, plan) == 0
    assert licensing.monthly_value({"plan": "trial"}, plan) == 0


# ---------------------------------------------------------------- store

def test_default_plans_and_editing(tmp_path):
    store = Store(tmp_path / "s.db")
    assert [p["id"] for p in store.list_plans()] == ["trial", "essentials", "business", "partner"]
    assert [p["id"] for p in store.list_plans(public_only=True)] == ["essentials", "business", "partner"]
    plan = store.get_plan("essentials")
    store.save_plan({**plan, "price_inr": 2499}, "ada@contoso.example")
    assert store.get_plan("essentials")["price_inr"] == 2499
    assert Store(tmp_path / "s.db").get_plan("essentials")["price_inr"] == 2499  # not re-seeded on restart
    assert not store.delete_plan("trial")


def test_pre_assigned_licence_applies_when_the_company_connects(tmp_path):
    store = Store(tmp_path / "s.db")
    store.save_licence(TENANT, {"plan": "business", "paid_until": day(60)}, "ada", "Customer added")
    store.create_tenant(TENANT, None, "ada@contoso.example")
    tenant = store.get_tenant(TENANT)
    assert tenant["plan"] == "business" and tenant["trial_ends"] is None and tenant["paid_until"] == day(60)
    assert store.plan_in_use("business") == 1 and not store.delete_plan("business")


def test_trial_length_comes_from_the_console(tmp_path):
    store = Store(tmp_path / "s.db")
    store.set_option("trial_days", "30")
    store.create_tenant(TENANT, None, "ada@contoso.example")
    assert licensing.state(store.get_tenant(TENANT))["days_left"] in (29, 30)


def test_scans_follow_the_licence_and_plan_frequency(tmp_path):
    store = Store(tmp_path / "s.db")
    store.create_tenant(TENANT, None, "ada@contoso.example")
    assert can_scan(store.get_tenant(TENANT)) and scan_due(store, store.get_tenant(TENANT))
    store.save_licence(TENANT, {"status": "suspended"}, "ada", "Suspended")
    assert not can_scan(store.get_tenant(TENANT)) and not scan_due(store, store.get_tenant(TENANT))


def test_backup_round_trip(tmp_path):
    store = Store(tmp_path / "a.db")
    store.save_details(NEWCO, {"company": "NewCo", "contact_email": "it@newco.example"}, "ada")
    store.save_licence(NEWCO, {"plan": "essentials", "price_inr": 1500}, "ada", "Customer added")
    store.set_option("trial_days", "21")
    data = json.loads(json.dumps(store.export_admin()))
    other = Store(tmp_path / "b.db")
    assert other.import_admin(data, "ada") == {"plans": 4, "accounts": 1}
    assert other.get_account(NEWCO)["price_inr"] == 1500 and other.trial_days() == 21
    assert other.licence_events(NEWCO)[0]["summary"] == "Customer added"
    with pytest.raises(ValueError):
        other.import_admin({"kind": "something else"}, "ada")


# ---------------------------------------------------------------- console pages

def console_world(tmp_path):
    w = World(tmp_path)
    lookups = {"newco.example": NEWCO, "contoso.example": TENANT}
    w.app = create_app(settings=make_settings(ops_emails=("ada@contoso.example",), ops_tenant_id=TENANT),
                       store=w.store, graph_factory=lambda tid: FakeGraph(w.routes), resolver=tenant_resolver(),
                       auth=w.auth, tenant_lookup=lambda d: lookups.get(d), sleep=lambda s: None)
    return w


def token(client, path="/admin/customers/new"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).text).group(1)


def test_console_is_for_the_team_only(tmp_path):
    w = console_world(tmp_path)
    for path in ("/admin", "/admin/customers", "/admin/pricing", "/admin/backup", "/admin/customers.csv",
                 "/admin/backup.json", f"/admin/customers/{TENANT}"):
        assert w.client().get(path, follow_redirects=False).headers["location"].startswith("/login")
        assert w.client(STAFF).get(path).status_code == 404
    w.client(STAFF).post("/admin/pricing/essentials", data={"csrf": "x", "name": "Hacked", "scan_days": "7"})
    assert w.store.get_plan("essentials")["name"] == "Essentials"


def test_overview_and_customer_list(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.get("/admin").text
    assert "Monthly revenue" in page and "Contoso Ltd" in page and "Free trial" in page
    assert "Contoso Ltd" in ada.get("/admin/customers?show=trial").text
    assert "Contoso Ltd" not in ada.get("/admin/customers?show=paying").text
    assert "Contoso Ltd" in ada.get("/admin/customers?q=contoso").text
    assert "No customers match" in ada.get("/admin/customers?q=nothing-like-this").text


def test_add_customer_before_they_connect(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    page = ada.post("/admin/customers/new", data={
        "csrf": token(ada), "company": "NewCo Pvt Ltd", "who": "newco.example", "contact_email": "IT@NewCo.example",
        "plan": "business", "paid_until": day(365), "price": "4,500", "cycle": "yearly"}, follow_redirects=True).text
    assert "Added NewCo Pvt Ltd" in page and "not connected yet" in page and "Business" in page
    acc = w.store.get_account(NEWCO)
    assert acc["plan"] == "business" and acc["price_inr"] == 4500 and acc["contact_email"] == "it@newco.example"
    assert "Customer added" in w.store.licence_events(NEWCO)[0]["summary"]
    assert "NewCo Pvt Ltd" in ada.get("/admin/customers?show=waiting").text
    assert "₹4,500" in ada.get("/admin").text  # counts towards monthly revenue


def test_add_customer_errors(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    page = ada.post("/admin/customers/new", data={"csrf": token(ada), "company": "X", "who": "unknown.example",
                                                  "plan": "trial"}).text
    assert "couldn&#39;t find a Microsoft 365 tenant" in page and 'value="X"' in page
    page = ada.post("/admin/customers/new", data={"csrf": token(ada), "company": "X", "who": NEWCO,
                                                  "plan": "business", "paid_until": "next year"}).text
    assert "Valid until must be a date" in page and w.store.get_account(NEWCO) is None
    ada.post("/admin/customers/new", data={"csrf": token(ada), "company": "X", "who": NEWCO, "plan": "trial"})
    page = ada.post("/admin/customers/new", data={"csrf": token(ada), "company": "Y", "who": "newco.example",
                                                  "plan": "trial"}, follow_redirects=True).text
    assert "already a customer" in page


def test_licence_set_up_first_applies_on_connect(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    ada.post("/admin/customers/new", data={"csrf": token(ada), "company": "Contoso", "who": TENANT,
                                           "plan": "business", "paid_until": day(90)})
    page = w.connect(ada).text
    assert "Business" in page and "licence valid until" in page and "free trial left" not in page


def test_change_licence_records_history_and_customer_sees_it(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.post(f"/admin/customers/{TENANT}/licence", data={
        "csrf": token(ada), "plan": "essentials", "status": "active", "paid_until": day(-1), "seats": "",
        "cycle": "monthly", "price": ""})
    event = w.store.licence_events(TENANT)[0]
    assert "Plan: Free trial → Essentials" in event["summary"] and event["by_email"] == "ada@contoso.example"
    dash = ada.get("/app").text
    assert "Your licence ended on" in dash and "disabled" in dash
    page = ada.post("/app/scan", data={"csrf": token(ada, "/app")}, follow_redirects=True).text
    assert "Your licence has ended" in page
    page = ada.post(f"/admin/customers/{TENANT}/licence", data={
        "csrf": token(ada), "plan": "essentials", "status": "active", "paid_until": day(-1), "seats": "",
        "cycle": "monthly", "price": ""}, follow_redirects=True).text
    assert "Nothing changed" in page


def test_quick_actions(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.post(f"/admin/customers/{TENANT}/licence", data={
        "csrf": token(ada), "plan": "business", "status": "active", "paid_until": "", "cycle": "monthly"})
    ada.post(f"/admin/customers/{TENANT}/quick", data={"csrf": token(ada), "action": "month"})
    assert w.store.get_account(TENANT)["paid_until"] == licensing.add_months(licensing.today(), 1).isoformat()
    ada.post(f"/admin/customers/{TENANT}/quick", data={"csrf": token(ada), "action": "suspend"})
    assert "Your account is paused" in ada.get("/app").text
    ada.post(f"/admin/customers/{TENANT}/quick", data={"csrf": token(ada), "action": "year"})
    acc = w.store.get_account(TENANT)
    assert acc["status"] == "active"  # extending also resumes
    assert acc["paid_until"] == licensing.add_months(licensing.add_months(licensing.today(), 1), 12).isoformat()


def test_seat_limit_warning(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.post(f"/admin/customers/{TENANT}/licence", data={
        "csrf": token(ada), "plan": "essentials", "status": "active", "seats": "1", "cycle": "monthly"})
    assert "licensed staff; your plan covers up to 1" in ada.get("/app").text
    assert "staff but 1 seats" in ada.get("/admin").text


def test_customer_details(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.post(f"/admin/customers/{TENANT}/details", data={
        "csrf": token(ada), "company": "Contoso Pvt Ltd", "domain": "contoso.example", "contact_name": "Ada",
        "contact_email": "ada@contoso.example", "contact_phone": "+91 98450 00000", "gstin": "29abcde1234f1z5",
        "address": "MG Road, Bengaluru", "notes": "Pays on the 5th"}, follow_redirects=True).text
    assert "Customer details saved" in page and "29ABCDE1234F1Z5" in page and "Pays on the 5th" in page
    page = ada.post(f"/admin/customers/{TENANT}/details", data={"csrf": token(ada), "gstin": "123"},
                    follow_redirects=True).text
    assert "GSTIN should be 15 characters" in page
    assert w.store.get_account(TENANT)["gstin"] == "29ABCDE1234F1Z5"


def test_unknown_customer_is_404(tmp_path):
    w = console_world(tmp_path)
    assert w.client(ADMIN).get(f"/admin/customers/{NEWCO}").status_code == 404
    assert w.client(ADMIN).get("/admin/customers/not-an-id").status_code == 404


def test_pricing_changes_show_on_the_public_page(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    assert "₹1,999/month in India" in w.client().get("/").text
    page = ada.post("/admin/pricing/essentials", data={
        "csrf": token(ada, "/admin/pricing"), "name": "Essentials", "price_inr": "2,499", "price_usd": "45",
        "unit": "month", "max_users": "75", "scan_days": "7", "tagline": "", "sort": "1", "cta": "trial",
        "features": "All checks\nsoon: WhatsApp alerts", "public": "1"}, follow_redirects=True).text
    assert "Saved Essentials" in page
    home = w.client().get("/").text
    assert "₹2,499/month in India" in home and "$45" in home and "up to 75 users" in home
    assert "WhatsApp alerts (coming soon)" in home
    ada.post("/admin/pricing/partner", data={"csrf": token(ada, "/admin/pricing"), "name": "Partner",
                                             "unit": "tenant/month", "scan_days": "1", "cta": "contact"})
    assert 'id="plan-partner"' not in w.client().get("/").text  # unticked "show on pricing page"


def test_pricing_validation_add_delete_and_trial(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    t = token(ada, "/admin/pricing")
    page = ada.post("/admin/pricing/business", data={"csrf": t, "name": "Business", "price_inr": "lots",
                                                     "unit": "month", "scan_days": "1", "cta": "trial"},
                    follow_redirects=True).text
    assert "Price in rupees must be a number" in page and w.store.get_plan("business")["price_inr"] == 4999
    page = ada.post("/admin/pricing/new", data={"csrf": t, "plan_id": "enterprise", "name": "Enterprise"},
                    follow_redirects=True).text
    assert "Plan added" in page and w.store.get_plan("enterprise")["public"] is False
    assert "Bad ID" not in page
    ada.post("/admin/pricing/new", data={"csrf": t, "plan_id": "Bad ID", "name": "x"})
    assert w.store.get_plan("bad id") is None
    ada.post("/admin/pricing/enterprise/delete", data={"csrf": t})
    assert w.store.get_plan("enterprise") is None
    ada.post("/admin/pricing/trial", data={"csrf": t, "trial_days": "21"})
    assert "21-day free trial" in w.client().get("/").text


def test_csv_export_escapes_formulas(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    ada.post("/admin/customers/new", data={"csrf": token(ada), "company": "=HYPERLINK(\"x\")", "who": NEWCO,
                                           "plan": "trial"})
    resp = ada.get("/admin/customers.csv")
    assert resp.headers["content-type"].startswith("text/csv") and "attachment" in resp.headers["content-disposition"]
    assert "Tenant ID,Company" in resp.text and "'=HYPERLINK" in resp.text


def test_backup_download_and_restore(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    ada.post("/admin/customers/new", data={"csrf": token(ada), "company": "NewCo", "who": NEWCO,
                                           "plan": "essentials"})
    backup = ada.get("/admin/backup.json")
    assert "attachment" in backup.headers["content-disposition"]
    w.store.import_admin({**backup.json(), "accounts": [], "licence_events": []}, "test")  # simulate a wipe
    assert w.store.get_account(NEWCO) is None
    t = token(ada, "/admin/backup")
    files = {"backup_file": ("backup.json", backup.content, "application/json")}
    page = ada.post("/admin/restore", data={"csrf": t, "confirm": "nope"}, files=files, follow_redirects=True).text
    assert "Type RESTORE" in page and w.store.get_account(NEWCO) is None
    page = ada.post("/admin/restore", data={"csrf": t, "confirm": "RESTORE"}, files=files,
                    follow_redirects=True).text
    assert "Restored 4 plans and 1 customer records" in page and w.store.get_account(NEWCO)["plan"] == "essentials"
    bad = {"backup_file": ("x.json", b"{not json", "application/json")}
    page = ada.post("/admin/restore", data={"csrf": t, "confirm": "RESTORE"}, files=bad, follow_redirects=True).text
    assert "Nothing was restored" in page
