import pytest

from app.graph import GRAPH_ROOT, GraphClient, GraphError


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, content=b""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = content
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append((url, params, headers))
        return self.responses.pop(0)


def client_with(responses, sleeps=None):
    session = FakeSession(responses)
    client = GraphClient(lambda: "token-abc", sleep=(sleeps.append if sleeps is not None else lambda s: None))
    client._session = lambda: session
    return client, session


def test_paging_follows_next_link_and_sends_token():
    client, session = client_with([
        FakeResponse(body={"value": [{"id": 1}, {"id": 2}], "@odata.nextLink": f"{GRAPH_ROOT}/users?$skiptoken=x"}),
        FakeResponse(body={"value": [{"id": 3}]}),
    ])
    assert [u["id"] for u in client.get_all("/users", {"$top": "2"})] == [1, 2, 3]
    assert session.requests[0][0] == f"{GRAPH_ROOT}/users"
    assert session.requests[0][1] == {"$top": "2"}
    assert session.requests[1][1] is None  # the next link carries its own query
    assert session.requests[0][2]["Authorization"] == "Bearer token-abc"


def test_limit_stops_early():
    client, _ = client_with([FakeResponse(body={"value": [{"id": i} for i in range(5)], "@odata.nextLink": "https://x"})])
    assert len(client.get_all("/users", limit=3)) == 3


def test_throttling_is_retried_with_retry_after():
    sleeps = []
    client, _ = client_with([
        FakeResponse(429, {"error": {"code": "TooManyRequests", "message": "slow down"}}, {"Retry-After": "7"}),
        FakeResponse(body={"isEnabled": True}),
    ], sleeps)
    assert client.get("/policies/identitySecurityDefaultsEnforcementPolicy") == {"isEnabled": True}
    assert sleeps == [7.0]


def test_errors_carry_code_and_premium_hint():
    client, _ = client_with([FakeResponse(403, {"error": {
        "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
        "message": "Neither tenant is B2C or tenant doesn't have premium license"}})])
    with pytest.raises(GraphError) as err:
        client.get("/auditLogs/signIns")
    assert err.value.status == 403 and err.value.forbidden and err.value.needs_premium


def test_retries_give_up_eventually():
    client, session = client_with([FakeResponse(503, {"error": {"code": "x", "message": "down"}})] * 5)
    with pytest.raises(GraphError) as err:
        client.get("/users")
    assert err.value.status == 503
    assert len(session.requests) == 5


def test_usage_report_csv_is_parsed():
    csv_bytes = "﻿Report Refresh Date,User Principal Name,Exchange Last Activity Date\n2026-11-04,a@b.example,2026-11-01\n".encode()
    client, _ = client_with([FakeResponse(content=csv_bytes)])
    rows = client.get_report_rows("/reports/getOffice365ActiveUserDetail(period='D180')")
    assert rows == [{"Report Refresh Date": "2026-11-04", "User Principal Name": "a@b.example",
                     "Exchange Last Activity Date": "2026-11-01"}]
