import re

import pytest

from app import fixes
from app.checks.rules import RULES
from app.graph import GraphError

from .fakes import LEGACY_CONSENT, TEAMS_RSC, TENANT, FakeWriter
from .test_web import ADMIN, STAFF, World, csrf


def snapshot_from(writer: FakeWriter) -> dict:
    """The parts of a scan snapshot the four fixable checks read."""
    policy = writer.settings["/policies/authorizationPolicy"]
    return {"authorization_policy": {"allowInvitesFrom": policy["allowInvitesFrom"],
                                     "defaultUserRolePermissions": policy["defaultUserRolePermissions"]},
            "sharepoint": {"sharing_capability": writer.settings["/admin/sharepoint/settings"]["sharingCapability"]}}


@pytest.mark.parametrize("check_id", sorted(fixes.FIXES))
def test_each_fix_turns_its_check_from_problem_to_pass(check_id):
    writer = FakeWriter()
    assert RULES[check_id].fn(snapshot_from(writer)).status in ("fail", "warn")
    outcome = fixes.apply(fixes.FIXES[check_id], writer)
    assert outcome["verified"] and len(writer.patches) == 1
    assert RULES[check_id].fn(snapshot_from(writer)).status == "pass"


def test_consent_fix_keeps_teams_and_chat_policies():
    writer = FakeWriter()
    fixes.apply(fixes.FIXES["ADM-003"], writer)
    assigned = writer.settings["/policies/authorizationPolicy"]["defaultUserRolePermissions"]["permissionGrantPoliciesAssigned"]
    assert TEAMS_RSC in assigned and fixes.LOW_RISK_CONSENT in assigned and LEGACY_CONSENT not in assigned


def test_nothing_is_sent_when_the_setting_is_already_safe():
    writer = FakeWriter()
    writer.settings["/admin/sharepoint/settings"]["sharingCapability"] = "externalUserSharingOnly"
    outcome = fixes.apply(fixes.FIXES["SHR-001"], writer)
    assert outcome["verified"] and writer.patches == []


def test_undo_puts_the_old_value_back():
    writer = FakeWriter()
    fix = fixes.FIXES["ADM-003"]
    first = fixes.apply(fix, writer)
    fixes.apply(fix, writer, value=first["before"])
    assigned = writer.settings["/policies/authorizationPolicy"]["defaultUserRolePermissions"]["permissionGrantPoliciesAssigned"]
    assert sorted(assigned) == sorted([LEGACY_CONSENT, TEAMS_RSC])


def test_owner_fixes_a_problem_then_undoes_it(tmp_path):
    world = World(tmp_path)
    client = world.client(ADMIN)
    world.connect(client)
    scan = world.store.latest_scan(TENANT)
    assert 'href="/app/fix/ACC-002"' in client.get(f"/app/report/{scan['id']}").text  # guests can invite in the fake

    preview = client.get("/app/fix/ACC-002")
    assert "Stop guests inviting other guests" in preview.text and "Policy.ReadWrite.Authorization" in preview.text

    done = client.post("/app/fix/ACC-002", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Done: Stop guests inviting other guests" in done.text and "Undo this change" in done.text
    assert world.auth.fix_requests == [(fixes.GRAPH + "Policy.ReadWrite.Authorization", ADMIN["email"])]
    assert world.tokens == ["delegated-token"]
    assert world.writer.settings["/policies/authorizationPolicy"]["allowInvitesFrom"] == "adminsGuestInvitersAndAllMembers"
    record = world.store.get_fix(1)
    assert (record["status"], record["before"], record["by_email"]) == ("done", "everyone", ADMIN["email"])
    assert "Changes made" in client.get("/app").text

    undone = client.post("/app/fixes/1/undo", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Changed back: Stop guests inviting other guests" in undone.text
    assert world.writer.settings["/policies/authorizationPolicy"]["allowInvitesFrom"] == "everyone"
    assert world.store.get_fix(1)["undone_by"] == 2
    assert "Undo this change" not in client.get("/app/fixes/1").text


def test_staff_cannot_make_changes(tmp_path):
    world = World(tmp_path)
    world.connect(world.client(ADMIN))
    world.store.add_member(TENANT, STAFF["email"])
    staff = world.client(STAFF)
    scan = world.store.latest_scan(TENANT)
    assert "Fix it for me" not in staff.get(f"/app/report/{scan['id']}").text
    assert staff.get("/app/fix/ACC-002").status_code == 403
    staff.post("/app/fix/ACC-002", data={"csrf": csrf(staff)}, follow_redirects=False)
    assert world.auth.fix_requests == [] and world.writer.patches == []


def test_missing_admin_role_is_explained_and_recorded(tmp_path):
    world = World(tmp_path)
    world.writer.fail = GraphError(403, "Authorization_RequestDenied", "Insufficient privileges")
    client = world.client(ADMIN)
    world.connect(client)
    page = client.post("/app/fix/SHR-001", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Nothing was changed" in page.text and "SharePoint Administrator" in page.text
    assert world.store.get_fix(1)["status"] == "failed"


def test_declined_approval_changes_nothing(tmp_path):
    world = World(tmp_path)
    client = world.client(ADMIN)
    world.connect(client)
    client.post("/app/fix/ADM-003", data={"csrf": csrf(client)}, follow_redirects=False)
    page = client.get("/auth/callback?error=access_denied&error_description=declined&state=fixstate")
    assert "Nothing was changed" in page.text and world.writer.patches == []


def test_approval_from_another_company_is_refused(tmp_path):
    world = World(tmp_path)
    client = world.client(ADMIN)
    world.connect(client)
    world.auth.fix_claims = {"tid": "99999999-0000-0000-0000-000000000000", "oid": ADMIN["oid"]}
    page = client.post("/app/fix/ADM-004", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "different account or company" in page.text and world.writer.patches == []


def test_sample_report_has_no_fix_buttons(tmp_path):
    assert "Fix it for me" not in World(tmp_path).client().get("/demo").text


def test_unknown_fix_is_a_friendly_404(tmp_path):
    world = World(tmp_path)
    client = world.client(ADMIN)
    world.connect(client)
    page = client.get("/app/fix/EML-001")
    assert page.status_code == 404 and re.search(r"step-by-step guide", page.text)
