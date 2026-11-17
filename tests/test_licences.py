"""Licence stock in the console: add licences per package, assign them to customers who signed up, take them back."""
import re
from datetime import date

from fastapi.testclient import TestClient

from app import licensing
from app.cli import main as cli
from app.db import Store

from .fakes import TENANT
from .test_console import NEWCO, console_world, token
from .test_web import ADMIN, STAFF

KEY = re.compile(r"^TVX-[A-Z0-9]{3}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")


def stocked(tmp_path):
    """A console with Ada's company signed up and the test licences added."""
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.post("/admin/licences/test", data={"csrf": token(ada, "/admin/licences")})
    assert "Added 5 test licences of each package" in page.text
    return w, ada


def first_in_stock(store: Store, plan: str) -> dict:
    return next(lic for lic in store.list_licences(status="available") if lic["plan"] == plan)


def test_test_licences_put_five_of_each_package_in_stock(tmp_path):
    w, ada = stocked(tmp_path)
    stock = w.store.licence_stock()
    assert stock == {p: {"total": 5, "available": 5, "assigned": 0} for p in ("essentials", "business", "partner")}
    codes = [lic["code"] for lic in w.store.list_licences()]
    assert len(set(codes)) == 15 and all(KEY.match(c) for c in codes)
    assert {lic["months"] for lic in w.store.list_licences()} == {12}
    page = ada.get("/admin/licences").text
    assert "TVX-BUS-" in page and "Choose a customer" in page and "Signed up" in page
    assert "Licences in stock" in ada.get("/admin").text


def test_assigning_a_licence_sets_the_customers_plan_and_end_date(tmp_path):
    w, ada = stocked(tmp_path)
    lic = first_in_stock(w.store, "business")
    ada.post(f"/admin/licences/{lic['id']}/assign", data={"csrf": token(ada, "/admin/licences"), "tenant_id": TENANT})
    tenant = w.store.get_tenant(TENANT)
    expected_end = licensing.add_months(date.today(), 12).isoformat()
    assert tenant["plan"] == "business" and tenant["paid_until"] == expected_end
    assert licensing.state(tenant)["key"] == "active"
    assert w.store.licence_for(TENANT)["code"] == lic["code"]
    assert w.store.licence_stock()["business"] == {"total": 5, "available": 4, "assigned": 1}
    assert lic["code"] in w.store.licence_events(TENANT)[0]["summary"]
    assert w.store.list_audit(prefix="licence.assign")[0]["detail"].startswith(lic["code"])
    assert lic["code"] in ada.get(f"/admin/customers/{TENANT}").text
    assert lic["code"] in ada.get("/admin/customers").text
    assert "Business" in ada.get("/app").text  # the customer's own dashboard shows the new plan


def test_a_new_licence_replaces_the_old_one_which_goes_back_to_stock(tmp_path):
    w, ada = stocked(tmp_path)
    old, new = first_in_stock(w.store, "essentials"), first_in_stock(w.store, "partner")
    tok = token(ada, "/admin/licences")
    ada.post(f"/admin/licences/{old['id']}/assign", data={"csrf": tok, "tenant_id": TENANT})
    ada.post(f"/admin/licences/{new['id']}/assign", data={"csrf": tok, "tenant_id": TENANT, "back": "customer"})
    assert w.store.get_licence(old["id"])["status"] == "available"
    assert w.store.licence_for(TENANT)["code"] == new["code"] and w.store.get_tenant(TENANT)["plan"] == "partner"
    assert f"replaces {old['code']}" in w.store.licence_events(TENANT)[0]["summary"]


def test_an_assigned_licence_cant_be_given_to_someone_else(tmp_path):
    w, ada = stocked(tmp_path)
    w.store.save_details(NEWCO, {"company": "NewCo"}, "ada")
    lic = first_in_stock(w.store, "business")
    tok = token(ada, "/admin/licences")
    ada.post(f"/admin/licences/{lic['id']}/assign", data={"csrf": tok, "tenant_id": TENANT})
    page = ada.post(f"/admin/licences/{lic['id']}/assign", data={"csrf": tok, "tenant_id": NEWCO})
    assert "no longer in stock" in page.text
    assert w.store.get_licence(lic["id"])["tenant_id"] == TENANT and w.store.licence_for(NEWCO) is None


def test_taking_a_licence_back_returns_the_customer_to_the_trial(tmp_path):
    w, ada = stocked(tmp_path)
    lic = first_in_stock(w.store, "business")
    tok = token(ada, "/admin/licences")
    ada.post(f"/admin/licences/{lic['id']}/assign", data={"csrf": tok, "tenant_id": TENANT})
    page = ada.post(f"/admin/licences/{lic['id']}/unassign", data={"csrf": tok, "back": "customer"})
    assert "taken back into stock" in page.text
    tenant = w.store.get_tenant(TENANT)
    assert tenant["plan"] == "trial" and tenant["paid_until"] is None
    assert w.store.get_licence(lic["id"])["status"] == "available" and w.store.licence_for(TENANT) is None


def test_only_licences_in_stock_can_be_removed(tmp_path):
    w, ada = stocked(tmp_path)
    given, spare = w.store.list_licences(status="available")[:2]
    tok = token(ada, "/admin/licences")
    ada.post(f"/admin/licences/{given['id']}/assign", data={"csrf": tok, "tenant_id": TENANT})
    assert "Take it back from the customer first" in ada.post(f"/admin/licences/{given['id']}/delete",
                                                              data={"csrf": tok}).text
    ada.post(f"/admin/licences/{spare['id']}/delete", data={"csrf": tok})
    assert w.store.get_licence(spare["id"]) is None and w.store.get_licence(given["id"]) is not None
    assert len(w.store.list_licences()) == 14


def test_adding_licences_checks_the_form(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    tok = token(ada, "/admin/licences")
    for bad in ({"plan": "trial"}, {"plan": "nope"}, {"plan": "business", "count": "0"},
                {"plan": "business", "count": "500"}, {"plan": "business", "months": "7"}):
        ada.post("/admin/licences/new", data={"csrf": tok, "count": "5", "months": "12", **bad})
    assert w.store.list_licences() == []
    ada.post("/admin/licences/new", data={"csrf": tok, "plan": "business", "count": "3", "months": "6", "seats": "25",
                                          "note": "Bundle for invoice 1042"})
    made = w.store.list_licences()
    assert len(made) == 3 and {(m["months"], m["seats"], m["note"]) for m in made} == {(6, 25, "Bundle for invoice 1042")}


def test_a_customer_set_up_before_connecting_gets_the_licence_when_they_connect(tmp_path):
    w = console_world(tmp_path)
    ada = w.client(ADMIN)
    ada.post("/admin/licences/test", data={"csrf": token(ada, "/admin/licences")})
    w.store.save_details(TENANT, {"company": "Contoso"}, "ada")  # in the console before signing up
    lic = first_in_stock(w.store, "essentials")
    page = ada.get(f"/admin/customers/{TENANT}").text
    assert f"/admin/licences/{lic['id']}/assign" in page and "5 left" in page  # assign straight from their page
    ada.post(f"/admin/licences/{lic['id']}/assign", data={"csrf": token(ada, "/admin/licences"), "tenant_id": TENANT,
                                                          "back": "customer"})
    w.connect(ada)
    assert w.store.get_tenant(TENANT)["plan"] == "essentials" and w.store.get_tenant(TENANT)["trial_ends"] is None


def test_licences_are_for_the_team_only(tmp_path):
    w, ada = stocked(tmp_path)
    lic = first_in_stock(w.store, "business")
    assert TestClient(w.app).get("/admin/licences", follow_redirects=False).headers["location"].startswith("/login")
    bob = w.client(STAFF)
    assert bob.get("/admin/licences").status_code == 404
    bob.post(f"/admin/licences/{lic['id']}/assign", data={"csrf": "x", "tenant_id": TENANT})
    bob.post("/admin/licences/test", data={"csrf": "x"})
    ada.post(f"/admin/licences/{lic['id']}/assign", data={"tenant_id": TENANT})  # no form token
    assert w.store.licence_for(TENANT) is None and len(w.store.list_licences()) == 15


def test_licences_travel_in_console_and_full_backups(tmp_path):
    w, ada = stocked(tmp_path)
    lic = first_in_stock(w.store, "business")
    ada.post(f"/admin/licences/{lic['id']}/assign", data={"csrf": token(ada, "/admin/licences"), "tenant_id": TENANT})
    data = w.store.export_admin()
    assert len(data["licences"]) == 15
    other = Store(tmp_path / "other.db")
    assert other.import_admin(data, "ada")["licences"] == 15
    assert other.licence_stock()["business"]["assigned"] == 1
    w.store.backup_to(tmp_path / "full.db")
    third = Store(tmp_path / "third.db")
    third.restore_from(tmp_path / "full.db")
    assert third.licence_for(TENANT)["code"] == lic["code"]
    assert len(third.add_licences("business", 1, 12, "ada")) == 1  # new keys still work after a restore


def test_cli_adds_licences(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TVX_DB_PATH", str(tmp_path / "cli.db"))
    assert cli(["add-licences", "business", "3", "--months", "6"]) == 0
    codes = capsys.readouterr().out.split()
    assert len(codes) == 3 and all(KEY.match(c) for c in codes)
    assert cli(["add-licences", "trial", "3"]) == 1
    assert Store(tmp_path / "cli.db").licence_stock()["business"]["available"] == 3
