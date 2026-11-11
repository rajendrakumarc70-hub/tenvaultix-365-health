"""Azure spend: reading Cost Management, summarising it, and the pages that show it."""
from datetime import date, datetime, timedelta, timezone

from app import azure_costs, demo
from app.graph import AuthError, GraphError
from app.web import create_app

from .fakes import TENANT, FakeGraph, make_settings, tenant_resolver
from .test_web import ADMIN, STAFF, World, csrf, world  # noqa: F401 (fixture)

COLUMNS = [{"name": "Cost", "type": "Number"}, {"name": "UsageDate", "type": "Number"},
           {"name": "ServiceName", "type": "String"}, {"name": "Currency", "type": "String"}]
TODAY = date(2026, 11, 11)
QUERY_PATH = "/subscriptions/{}/providers/Microsoft.CostManagement/query"


def cost_page(rows, next_link=None):
    props = {"columns": COLUMNS, "rows": rows}
    if next_link:
        props["nextLink"] = next_link
    return {"properties": props}


class FakeArm:
    """Azure Resource Manager: a subscription list and a cost answer (or an error) per subscription."""

    def __init__(self, subscriptions=None, costs=None, list_error=None):
        self.subscriptions = subscriptions if subscriptions is not None else [
            {"subscriptionId": "sub-1", "displayName": "Production"}]
        self.costs = costs if costs is not None else {"sub-1": cost_page([
            [1200.5, 20261101, "Virtual Machines", "INR"], [300, 20261101, "Storage", "INR"],
            [900, 20261102, "Virtual Machines", "INR"], [100, 20261109, "Storage", "INR"]])}
        self.list_error = list_error
        self.posts = []

    def get_all(self, path, params=None):
        assert path == "/subscriptions" and params == {"api-version": azure_costs.SUBSCRIPTIONS_API}
        if self.list_error:
            raise self.list_error
        return self.subscriptions

    def post(self, path, body, params=None):
        self.posts.append((path, body, params))
        assert body == azure_costs.QUERY
        key = path if path.startswith("https://") else path.split("/")[2]
        answer = self.costs[key]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_parse_rows_goes_by_column_name():
    props = {"columns": [{"name": "UsageDate", "type": "Number"}, {"name": "Currency", "type": "String"},
                         {"name": "PreTaxCost", "type": "Number"}, {"name": "ServiceName", "type": "String"}],
             "rows": [[20261103, "USD", 12.5, "Storage"], [20261104, "USD", 1, None]]}
    rows = azure_costs.parse_rows(props)
    assert rows[0] == {"date": "2026-11-03", "service": "Storage", "cost": 12.5, "currency": "USD"}
    assert rows[1]["service"] == "Other"
    assert azure_costs.parse_rows({"columns": [], "rows": [[1]]}) == []


def test_fetch_summarises_the_month():
    arm = FakeArm()
    data = azure_costs.fetch(arm, today=TODAY)
    assert data["state"] == "ok" and data["currency"] == "INR"
    assert data["total"] == 2500.5
    assert data["last_day"] == "2026-11-09"
    assert data["projected"] == round(2500.5 / 9 * 30, 2)
    assert len(data["days"]) == 30 and data["days"][0]["cost"] == 1500.5 and data["days"][2]["cost"] == 0
    assert data["services"][0] == {"name": "Virtual Machines", "cost": 2100.5}
    assert data["subscriptions"][0]["cost"] == 2500.5
    path, body, params = arm.posts[0]
    assert path == QUERY_PATH.format("sub-1") and params == {"api-version": azure_costs.COST_API}


def test_fetch_follows_next_page():
    page2 = "https://management.azure.com/next?skiptoken=abc"
    arm = FakeArm(costs={"sub-1": cost_page([[10, 20261101, "Storage", "INR"]], next_link=page2),
                         page2: cost_page([[5, 20261102, "Storage", "INR"]])})
    assert azure_costs.fetch(arm, today=TODAY)["total"] == 15
    assert arm.posts[1][0] == page2


def test_no_subscriptions_means_not_shared_yet():
    assert azure_costs.fetch(FakeArm(subscriptions=[]), today=TODAY)["state"] == "no_access"


def test_some_subscriptions_not_shared():
    arm = FakeArm(subscriptions=[{"subscriptionId": "sub-1", "displayName": "Production"},
                                 {"subscriptionId": "sub-2", "displayName": "Finance"}],
                  costs={"sub-1": FakeArm().costs["sub-1"],
                         "sub-2": GraphError(403, "AuthorizationFailed", "no role")})
    data = azure_costs.fetch(arm, today=TODAY)
    assert data["state"] == "ok"
    assert [s["status"] for s in data["subscriptions"]] == ["ok", "no_access"]


def test_all_subscriptions_refused_is_no_access_and_errors_are_errors():
    refused = FakeArm(costs={"sub-1": GraphError(403, "AuthorizationFailed", "no role")})
    assert azure_costs.fetch(refused, today=TODAY)["state"] == "no_access"
    broken = FakeArm(costs={"sub-1": GraphError(500, "InternalServerError", "boom")})
    assert azure_costs.fetch(broken, today=TODAY)["state"] == "error"
    token = FakeArm(list_error=AuthError("AADSTS7000215: Invalid client secret provided."))
    data = azure_costs.fetch(token, today=TODAY)
    assert data["state"] == "error" and "AADSTS7000215" in data["code_text"]


def test_mixed_currencies_kept_apart():
    arm = FakeArm(subscriptions=[{"subscriptionId": "a"}, {"subscriptionId": "b"}],
                  costs={"a": cost_page([[1000, 20261101, "Storage", "INR"]]),
                         "b": cost_page([[20, 20261101, "Storage", "USD"]])})
    data = azure_costs.fetch(arm, today=TODAY)
    assert data["currency"] == "INR" and data["total"] == 1000 and data["other_currencies"] == {"USD": 20}


def test_money_formatting():
    assert azure_costs.fmt_money(1234567.4, "INR") == "₹12,34,567"
    assert azure_costs.fmt_money(999, "INR") == "₹999"
    assert azure_costs.fmt_money(1234567, "USD") == "$1,234,567"
    assert azure_costs.fmt_money(12.5, "EUR") == "€12.50"
    assert azure_costs.fmt_money(5, "JPY") == "JPY 5.00"
    assert azure_costs.fmt_money(None, "INR") == "–"
    assert azure_costs.compact(250000, "INR") == "₹2.5L" and azure_costs.compact(2500, "USD") == "$2.5K"


def test_chart_geometry():
    days = [{"date": f"2026-11-{d:02d}", "cost": c} for d, c in [(1, 100), (2, 0), (3, 380)]]
    chart = azure_costs.chart(days)
    assert chart["ticks"][-1]["value"] >= 380 and chart["ticks"][0]["value"] == 0
    assert all(b["w"] <= 24 for b in chart["bars"])
    assert chart["bars"][1]["h"] == 0 and chart["bars"][2]["h"] > chart["bars"][0]["h"]
    assert azure_costs.chart([{"date": "2026-11-01", "cost": 0}])["ticks"] == []


def test_demo_sample_page(world):
    page = world.client().get("/demo/azure")
    assert page.status_code == 200
    assert "Spend per day" in page.text and "₹" in page.text and "Virtual Machines" in page.text
    assert "Finance sandbox" in page.text and "Cost Management Reader" in page.text
    data = demo.azure_costs(TODAY)
    assert data["state"] == "ok" and data["last_day"] == "2026-11-09"


def azure_world(tmp_path, arm):
    w = World(tmp_path)
    w.arm = arm
    w.app = create_app(settings=make_settings(), store=w.store, graph_factory=lambda tid: FakeGraph(w.routes),
                       resolver=tenant_resolver(), auth=w.auth, tenant_lookup=lambda d: None, sleep=lambda s: None,
                       arm_factory=lambda tid: w.arm)
    return w


def test_dashboard_and_azure_page_for_a_connected_company(tmp_path):
    w = azure_world(tmp_path, FakeArm())
    ada = w.client(ADMIN)
    w.connect(ada)
    assert "See Azure spend" in ada.get("/app").text
    page = ada.get("/app/azure").text
    assert "spent so far this month" in page and "Production" in page
    assert "Share your Azure costs" not in page
    assert "spent in" in ada.get("/app").text  # the dashboard now shows the cached total
    assert len(w.arm.posts) == 1
    ada.get("/app/azure")
    assert len(w.arm.posts) == 1  # served from the hour-long cache


def test_viewer_can_see_but_outsiders_cannot(tmp_path):
    w = azure_world(tmp_path, FakeArm())
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.post("/app/team", data={"csrf": csrf(ada), "email": STAFF["email"]})
    assert "spent so far" in w.client(STAFF).get("/app/azure").text
    assert w.client().get("/app/azure", follow_redirects=False).headers["location"].startswith("/login")


def test_not_shared_shows_the_steps(tmp_path):
    w = azure_world(tmp_path, FakeArm(subscriptions=[]))
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.get("/app/azure").text
    assert "Share your Azure costs with Tenvaultix" in page and "Tenvaultix 365 Health Check" in page
    assert "Show me how" in ada.get("/app").text


def test_refresh_needs_csrf_and_waits_five_minutes(tmp_path):
    w = azure_world(tmp_path, FakeArm())
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.get("/app/azure")
    ada.post("/app/azure/refresh", data={"csrf": "wrong"})
    page = ada.post("/app/azure/refresh", data={"csrf": csrf(ada)}, follow_redirects=True).text
    assert "updated a moment ago" in page and len(w.arm.posts) == 1
    cached = w.store.get_costs(TENANT)
    cached["fetched_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    w.store.save_costs(TENANT, cached)  # save_costs stamps now, so backdate directly
    with w.store._conn() as db:
        db.execute("UPDATE azure_costs SET fetched_at = ?", (cached["fetched_at"],))
    ada.post("/app/azure/refresh", data={"csrf": csrf(ada)})
    assert len(w.arm.posts) == 2


def test_failed_refresh_keeps_last_good_figures_and_records_problem(tmp_path):
    w = azure_world(tmp_path, FakeArm())
    ada = w.client(ADMIN)
    w.connect(ada)
    ada.get("/app/azure")
    w.arm = FakeArm(list_error=GraphError(500, "InternalServerError", "Cost service down"))
    with w.store._conn() as db:
        db.execute("UPDATE azure_costs SET fetched_at = ?", ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
    page = ada.get("/app/azure").text
    assert "Couldn't refresh just now" in page and "spent so far this month" in page
    problem = w.store.list_problems(tenant_id=TENANT)[0]
    assert problem["area"] == "azure" and "Cost service down" in problem["detail"]


def test_crashing_azure_client_shows_an_error_not_a_500(tmp_path):
    w = azure_world(tmp_path, None)
    ada = w.client(ADMIN)
    w.connect(ada)
    page = ada.get("/app/azure")
    assert page.status_code == 200 and "We couldn't read Azure costs" in page.text
    assert "Share your Azure costs" not in page.text
    assert w.store.list_problems(tenant_id=TENANT)[0]["code"] == "azure-error"
