import copy

from app import demo
from app.checks import catalog, results_for_domain, run_checks
from app.checks.rules import RULES
from app.dnscheck import StaticResolver, check_domain
from app.scoring import compare, grade, sort_for_report, summarize


def by_id(results):
    return {r["id"]: r for r in results}


def test_every_catalog_check_has_a_rule_and_vice_versa():
    assert set(catalog()["checks"]) == set(RULES)
    categories = set(catalog()["categories"])
    for check in catalog()["checks"].values():
        assert check["category"] in categories
        assert check["severity"] in ("critical", "high", "medium", "low", "info")
        assert check["fix"] and check["why"]


def test_sample_tenant_report():
    results = by_id(run_checks(demo.snapshot()))
    assert results["ADM-001"]["status"] == "fail"
    assert results["ADM-001"]["evidence"] == ["Ananya Reddy (ananya.reddy@acmediagnostics.example): Global Administrator"]
    assert results["MFA-002"]["status"] == "warn"          # MFA only for admins
    assert results["MFA-003"]["status"] == "pass"          # legacy auth blocked
    assert results["EML-004"]["status"] == "fail"
    assert results["LIC-001"]["waste"] == 120.0           # 8 x $14 + 2 x $4
    assert results["LIC-002"]["waste"] == 28.0
    assert results["LIC-003"]["waste"] == 32.0
    summary = summarize(list(results.values()))
    assert summary["waste"] == 180.0
    assert summary["grade"] == grade(summary["score"])
    assert 0 < summary["score"] < 60


def test_missing_section_reports_not_available_with_reason():
    snap = demo.snapshot()
    snap["devices"] = None
    snap["unavailable"]["devices"] = "Needs Microsoft Intune."
    result = by_id(run_checks(snap))["DEV-001"]
    assert result["status"] == "not_available"
    assert result["summary"] == "Needs Microsoft Intune."


def test_a_crashing_rule_does_not_break_the_report(monkeypatch):
    def boom(snap):
        raise RuntimeError("bug")

    monkeypatch.setattr(RULES["SHR-001"], "fn", boom)
    results = by_id(run_checks(demo.snapshot()))
    assert results["SHR-001"]["status"] == "error"
    assert results["EML-004"]["status"] == "fail"


def test_security_defaults_satisfy_mfa_and_legacy_checks():
    snap = demo.snapshot()
    snap["security_defaults"] = True
    snap["conditional_access"] = None
    results = by_id(run_checks(snap))
    assert results["MFA-002"]["status"] == "pass"
    assert results["MFA-003"]["status"] == "pass"


def test_no_enforcement_without_premium_suggests_security_defaults():
    snap = demo.snapshot()
    snap["conditional_access"] = None
    result = by_id(run_checks(snap))["MFA-002"]
    assert result["status"] == "fail"
    assert "security defaults" in result["summary"]


def test_report_only_mfa_policy_is_not_enforcement():
    snap = demo.snapshot()
    snap["conditional_access"] = [{"name": "MFA all", "state": "enabledForReportingButNotEnforced",
                                   "include_users": ["All"], "include_apps": ["All"], "client_app_types": ["all"],
                                   "grant_controls": ["mfa"], "auth_strength": False}]
    result = by_id(run_checks(snap))["MFA-002"]
    assert result["status"] == "fail"
    assert "report-only" in result["summary"]


def test_global_admin_count_bands():
    snap = demo.snapshot()
    ga = snap["roles"][0]
    for members, status in ([["user-0"], "warn"], [["user-0", "user-1"], "pass"],
                            [[f"user-{i}" for i in range(6)], "fail"]):
        ga["members"] = members
        assert by_id(run_checks(snap))["ADM-002"]["status"] == status


def test_stale_guests_need_sign_in_logs():
    snap = demo.snapshot()
    snap["activity_source"] = "usage reports"
    assert by_id(run_checks(snap))["ACC-003"]["status"] == "not_available"


def test_unknown_huge_sku_pool_is_ignored_but_unknown_paid_sku_is_listed():
    snap = demo.snapshot()
    snap["skus"] = [
        {"sku_id": "a", "part_number": "SOME_VIRAL_TRIAL", "name": "x", "enabled": 10000, "consumed": 2, "has_mailbox": False},
        {"sku_id": "b", "part_number": "NEW_ADDON", "name": "y", "enabled": 10, "consumed": 4, "has_mailbox": False},
    ]
    result = by_id(run_checks(snap))["LIC-001"]
    assert result["status"] == "fail"
    assert result["waste"] is None
    assert result["evidence"] == ["New Addon: 6 of 10 unassigned (price unknown)"]


def test_sort_puts_critical_warning_above_low_failure():
    results = sort_for_report(run_checks(demo.snapshot()))
    order = [r["id"] for r in results]
    assert order.index("MFA-002") < order.index("LIC-003")
    assert results[-1]["status"] == "pass"


def test_compare_finds_new_and_fixed():
    before = run_checks(demo.snapshot())
    snap = demo.snapshot()
    snap["forwarding"] = []
    snap["sharepoint"] = {"sharing_capability": "externalUserSharingOnly"}
    snap["security_defaults"] = True
    snap["roles"][0]["members"] = [f"user-{i}" for i in range(7)]
    changes = compare(before, run_checks(snap))
    assert "Email forwarded outside the company" in changes["fixed"]
    assert "Number of Global Administrators" in changes["new"]
    assert compare(None, before)["first_scan"]


def test_grades():
    assert [grade(s) for s in (100, 85, 70, 55, 35, 34, None)] == ["A", "A", "B", "C", "D", "F", "-"]


def test_free_domain_results():
    resolver = StaticResolver(txt={"example.com": ["v=spf1 include:spf.protection.outlook.com -all"],
                                   "_dmarc.example.com": ["v=DMARC1; p=none"]},
                              mx={"example.com": ["example-com.mail.protection.outlook.com"]})
    results = by_id(results_for_domain(check_domain("example.com", resolver, tenant_lookup=None)))
    assert results["EML-001"]["status"] == "pass"
    assert results["EML-002"]["status"] == "warn"
    assert results["EML-003"]["status"] == "fail"
    assert results["EML-002"]["evidence"][0] == "Record: v=DMARC1; p=none"


def test_demo_snapshot_is_not_mutated_between_runs():
    first = demo.snapshot()
    snap = copy.deepcopy(first)
    run_checks(snap)
    assert snap == first
