from app.checks import run_checks
from app.collector import CONCEALED, NEEDS_INTUNE, NEEDS_P1, collect
from app.graph import GraphError

from .fakes import NOW, TENANT, FakeGraph, tenant_resolver, tenant_routes


def by_id(results):
    return {r["id"]: r for r in results}


def scan(routes):
    return collect(FakeGraph(routes), tenant_resolver(), TENANT, now=NOW, workers=2)


def test_business_premium_tenant():
    snap = scan(tenant_routes(premium=True))
    assert snap["tenant"]["name"] == "Contoso Ltd"
    assert snap["features"]["entra_p1"] and snap["features"]["intune"]
    assert snap["activity_source"] == "sign-in logs"
    assert snap["mfa_source"] == "registration report"
    assert snap["unavailable"] == {}
    assert snap["mailboxes_checked"] == 2  # u3 has no mailbox, u4 is blocked
    assert snap["forwarding"] == [{"user": "Bob Staff (bob@contoso.example)", "rule": "Send to home",
                                   "targets": ["bob.home@mail.example"]}]

    results = by_id(run_checks(snap))
    assert results["MFA-002"]["status"] == "pass"
    assert results["MFA-001"]["evidence"] == ["Bob Staff (bob@contoso.example)"]
    assert results["ADM-001"]["status"] == "pass"
    assert results["ADM-002"]["status"] == "warn"  # only one Global Admin
    assert results["ADM-003"]["status"] == "pass"
    assert results["ACC-001"]["evidence"] == ["Cara Idle (cara@contoso.example): last active 150 days ago"]
    assert results["ACC-002"]["status"] == "fail"
    assert results["ACC-003"]["status"] == "fail"  # guest idle 120 days
    assert results["EML-001"]["status"] == results["EML-002"]["status"] == results["EML-003"]["status"] == "pass"
    assert results["EML-004"]["status"] == "fail"
    assert results["SHR-001"]["status"] == "pass"
    assert results["LIC-001"]["waste"] == 2 * 22.0   # 2 spare Business Premium
    assert results["LIC-002"]["waste"] == 22.0       # Dan Left
    assert results["LIC-003"]["waste"] == 22.0       # Cara Idle
    assert results["DEV-001"]["status"] == "fail"
    assert results["GEN-001"]["summary"].startswith("Your Microsoft Secure Score is 50%")


def test_business_standard_tenant_uses_fallbacks():
    snap = scan(tenant_routes(premium=False))
    assert not snap["features"]["entra_p1"]
    assert snap["activity_source"] == "usage reports"
    assert snap["mfa_source"] == "sign-in methods"
    assert snap["unavailable"]["conditional_access"] == NEEDS_P1
    assert snap["unavailable"]["devices"] == NEEDS_INTUNE

    results = by_id(run_checks(snap))
    assert results["MFA-002"]["status"] == "pass"            # security defaults on
    assert results["MFA-001"]["evidence"] == ["Bob Staff (bob@contoso.example)"]
    assert results["ACC-001"]["evidence"][0].startswith("Cara Idle")
    assert results["ACC-003"]["status"] == "not_available"
    assert results["DEV-001"]["status"] == "not_available"
    assert results["LIC-001"]["waste"] == 2 * 14.0


def test_hidden_report_names_explain_how_to_fix():
    routes = tenant_routes(premium=False)
    routes["/reports/getOffice365ActiveUserDetail(period='D180')"] = [
        {"User Principal Name": "5A1B9C0D2E3F", "Exchange Last Activity Date": "2026-11-01"}]
    snap = scan(routes)
    assert snap["activity"] is None
    assert snap["unavailable"]["activity"] == CONCEALED
    assert by_id(run_checks(snap))["ACC-001"]["status"] == "not_available"


def test_missing_permission_marks_only_that_section():
    routes = tenant_routes(premium=True)
    denied = GraphError(403, "Authorization_RequestDenied", "Insufficient privileges")
    for user in ("u1", "u2", "u3"):
        routes[f"/users/{user}/mailFolders/inbox/messageRules"] = denied
    snap = scan(routes)
    assert snap["forwarding"] is None
    assert "permission" in snap["unavailable"]["forwarding"]
    results = by_id(run_checks(snap))
    assert results["EML-004"]["status"] == "not_available"
    assert results["MFA-001"]["status"] in ("warn", "fail")


def test_premium_error_on_sign_in_logs_falls_back_to_usage_reports():
    routes = tenant_routes(premium=True)
    routes["/users?id,signInActivity"] = GraphError(
        403, "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
        "Neither tenant is B2C or tenant doesn't have premium license")
    routes["/reports/getOffice365ActiveUserDetail(period='D180')"] = [
        {"User Principal Name": "cara@contoso.example", "Exchange Last Activity Date": "2026-10-30"}]
    snap = scan(routes)
    assert snap["activity_source"] == "usage reports"
