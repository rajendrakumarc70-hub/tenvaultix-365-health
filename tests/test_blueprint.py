import json
import re

from app import blueprint, demo
from app.checks import run_checks

from .test_web import ADMIN, World


def demo_model():
    snap = demo.snapshot()
    return blueprint.build(run_checks(snap), blueprint.facts(snap))


def test_facts_from_sample_tenant():
    f = blueprint.facts(demo.snapshot())
    assert f["seats_bought"] == 47 and f["seats_unassigned"] == 10
    assert f["blocked_licensed"] == 2 and f["inactive_licensed"] == 3
    assert f["global_admins"] == 3 and f["global_admins_without_mfa"] == 1
    assert f["anyone_links"] is True and f["devices"] == 34


def test_rooms_and_notes():
    bp = demo_model()
    assert bp["rooms"]["mailroom"]["status"] == "fail"
    assert bp["rooms"]["entrance"]["status"] == "warn"
    # Notes are numbered 1..n, front door first, and every note belongs to the room that lists it.
    assert [n["n"] for n in bp["notes"]] == list(range(1, len(bp["notes"]) + 1))
    assert bp["notes"][0]["room"] == "entrance"
    for key, room in bp["rooms"].items():
        assert all(bp["notes"][n - 1]["room"] == key for n in room["notes"])
    assert bp["problems"] == len(bp["notes"])


def test_notes_count_with_the_right_grammar():
    texts = [n["text"] for n in demo_model()["notes"]]
    assert "10 empty desks you still pay rent for." in texts
    assert "A master key is left under the mat: 1 admin has no MFA." in texts
    assert "1 device will break the rules soon." in texts


def test_single_counts_read_naturally():
    f = {"inactive_licensed": 1, "blocked_licensed": 1}
    assert blueprint._note({"id": "ACC-001", "status": "fail", "summary": ""}, f) == \
        "Badges still work for 1 person who hasn't been in for 90 days."
    assert blueprint._note({"id": "LIC-002", "status": "fail", "summary": ""}, f) == \
        "1 desk is still kept for someone who has left."


def test_note_falls_back_to_summary_without_counts():
    check = {"id": "LIC-001", "status": "fail", "summary": "Some licenses are unassigned."}
    assert blueprint._note(check, {}) == "Some licenses are unassigned."


def test_desks_one_per_license_until_the_room_is_full():
    desks = demo_model()["desks"]
    kinds = [c["kind"] for c in desks["cells"]]
    assert desks["per_desk"] == 1 and len(kinds) == 47
    assert (kinds.count("used"), kinds.count("idle"), kinds.count("left"), kinds.count("empty")) == (32, 3, 2, 10)
    big = blueprint._desks({"seats_bought": 300, "seats_unassigned": 30})
    assert big["per_desk"] == 5 and len(big["cells"]) <= blueprint.MAX_DESKS


def test_doors_seals_and_leaks():
    bp = demo_model()
    assert bp["front_door"] == "warn" and bp["back_door_open"] is False
    assert bp["mail_leak"] and bp["street_door"]
    assert {s["label"]: s["status"] for s in bp["seals"]} == {"SPF": "pass", "DMARC": "warn", "DKIM": "fail"}


def test_sample_report_draws_the_office(tmp_path):
    page = World(tmp_path).client().get("/demo").text
    assert "Your digital office, inspected" in page
    markers = re.findall(r'class="bp-marker st-\w+" href="#([A-Z]+-\d+)"', page)
    assert len(markers) == 15
    for check_id in markers:  # every mark links to a finding on the same page
        assert f'id="{check_id}"' in page
    assert 'style="' not in page.split('id="office"')[1].split("</section>")[0]  # CSP: no inline styles


def test_real_report_draws_the_office_and_old_scans_still_render(tmp_path):
    world = World(tmp_path)
    client = world.client(ADMIN)
    world.connect(client)
    scan = world.store.latest_scan(ADMIN["tid"])
    assert "facts" in scan["meta"]
    page = client.get(f"/app/report/{scan['id']}").text
    assert "Your digital office, inspected" in page and "bp-marker" in page

    # A scan stored before the drawing existed has no counts: the plan still draws, and asks for a new scan.
    with world.store._conn() as db:
        meta = dict(scan["meta"])
        meta.pop("facts")
        db.execute("UPDATE scans SET meta=? WHERE id=?", (json.dumps(meta), scan["id"]))
    page = client.get(f"/app/report/{scan['id']}").text
    assert "Your digital office, inspected" in page and "Run a new scan to draw this room" in page
