"""Tests de l'API KPI (base MongoDB simulee avec mongomock).

Installation : pip install pytest mongomock httpx
Lancement    : pytest -v
"""

import json
from pathlib import Path

import pytest

mongomock = pytest.importorskip("mongomock")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

KEY = {"X-API-Key": "test-key"}
SAMPLE = json.loads((Path(__file__).parent / "sample_batch.json").read_text(encoding="utf-8"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    db = mongomock.MongoClient()["test"]
    main.app.dependency_overrides[main.get_db] = lambda: db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ingest_requires_key(client):
    assert client.post("/kpi/ingest", json=SAMPLE).status_code == 401
    bad = client.post("/kpi/ingest", json=SAMPLE, headers={"X-API-Key": "mauvaise"})
    assert bad.status_code == 401


def test_ingest_then_read(client):
    r = client.post("/kpi/ingest", json=SAMPLE, headers=KEY)
    assert r.status_code == 200
    assert r.json() == {"batch_id": "2026-W47", "received": 4, "inserted": 4, "updated": 0}

    lst = client.get("/kpi").json()
    assert lst["count"] == 4
    assert lst["last_update"] is not None

    health = client.get("/kpi", params={"category": "health"}).json()
    assert health["count"] == 2
    assert all(i["category"] == "health" for i in health["items"])


def test_ingest_is_idempotent(client):
    client.post("/kpi/ingest", json=SAMPLE, headers=KEY)
    r = client.post("/kpi/ingest", json=SAMPLE, headers=KEY)
    assert r.json()["inserted"] == 0
    assert r.json()["updated"] == 4
    assert client.get("/kpi").json()["count"] == 4


def test_get_one_and_404(client):
    client.post("/kpi/ingest", json=SAMPLE, headers=KEY)
    one = client.get("/kpi/SP.DYN.LE00.IN")
    assert one.status_code == 200
    assert one.json()["rankings"]["world"]["rank"] == 100
    assert client.get("/kpi/INCONNU").status_code == 404


def test_last_update(client):
    assert client.get("/kpi/last-update").status_code == 404
    client.post("/kpi/ingest", json=SAMPLE, headers=KEY)
    r = client.get("/kpi/last-update")
    assert r.status_code == 200
    assert r.json()["batch_id"] == "2026-W47"
    assert r.json()["count"] == 4


def test_rank_above_total_rejected(client):
    bad = json.loads(json.dumps(SAMPLE))
    bad["indicators"][0]["rankings"]["world"] = {"rank": 200, "total": 190}
    assert client.post("/kpi/ingest", json=bad, headers=KEY).status_code == 422


def test_duplicate_codes_rejected(client):
    bad = json.loads(json.dumps(SAMPLE))
    bad["indicators"].append(bad["indicators"][0])
    assert client.post("/kpi/ingest", json=bad, headers=KEY).status_code == 422


def test_unknown_category_rejected(client):
    bad = json.loads(json.dumps(SAMPLE))
    bad["indicators"][0]["category"] = "sport"
    assert client.post("/kpi/ingest", json=bad, headers=KEY).status_code == 422


REAL = json.loads(
    (Path(__file__).parent / "tests" / "lot_reel_2026-W48.json").read_text(encoding="utf-8")
)
AZURE = "https://citoyencloud-maroc-hfcrexacd5djfdaa.italynorth-01.azurewebsites.net"


def test_real_batch_with_economy_accepted(client):
    """Lot reel de Houda (10 indicateurs dont 3 'economy') : avant correction -> 422."""
    r = client.post("/kpi/ingest", json=REAL, headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json() == {"batch_id": "2026-W48", "received": 10, "inserted": 10, "updated": 0}
    eco = client.get("/kpi", params={"category": "economy"}).json()
    assert eco["count"] == 3
    assert {i["code"] for i in eco["items"]} == {
        "SL.UEM.TOTL.NE.ZS", "GGXWDG_NGDP", "GC.XPN.INTP.RV.ZS"
    }
    assert client.get("/kpi").json()["count"] == 10


def test_cors_preflight_azure_site(client):
    r = client.options(
        "/kpi/ingest",
        headers={
            "Origin": AZURE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == AZURE


def test_cors_get_localhost_8080(client):
    r = client.get("/health", headers={"Origin": "http://localhost:8080"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:8080"


def test_cors_unknown_origin_refused(client):
    r = client.get("/health", headers={"Origin": "https://autre-site.example"})
    assert "access-control-allow-origin" not in r.headers


def _history_batch():
    """Un indicateur sur 3 annees + un second sur 1 annee (format Houda : un objet par annee)."""
    base = json.loads(json.dumps(REAL["indicators"][0]))
    rows = []
    for year, value in [(2022, 20.0), (2023, 21.5), (2024, 23.28)]:
        row = json.loads(json.dumps(base))
        row.update({"year": year, "value": value})
        rows.append(row)
    other = json.loads(json.dumps(REAL["indicators"][4]))
    return {"batch_id": "2026-W49", "indicators": rows + [other]}


def test_same_code_several_years_accepted(client):
    r = client.post("/kpi/ingest", json=_history_batch(), headers=KEY)
    assert r.status_code == 200, r.text
    assert r.json()["received"] == 4 and r.json()["inserted"] == 4


def test_same_code_same_year_rejected(client):
    bad = _history_batch()
    bad["indicators"].append(bad["indicators"][0])
    assert client.post("/kpi/ingest", json=bad, headers=KEY).status_code == 422


def test_history_endpoint_sorted(client):
    client.post("/kpi/ingest", json=_history_batch(), headers=KEY)
    r = client.get("/kpi/SE.XPD.TOTL.GB.ZS/history")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert [p["year"] for p in body["series"]] == [2022, 2023, 2024]
    assert body["series"][-1]["value"] == 23.28
    assert client.get("/kpi/INCONNU/history").status_code == 404


def test_list_and_get_return_latest_year_only(client):
    client.post("/kpi/ingest", json=_history_batch(), headers=KEY)
    lst = client.get("/kpi").json()
    assert lst["count"] == 2  # 2 indicateurs, pas 4 lignes
    one = client.get("/kpi/SE.XPD.TOTL.GB.ZS").json()
    assert one["year"] == 2024 and one["value"] == 23.28


def test_history_reingest_idempotent(client):
    client.post("/kpi/ingest", json=_history_batch(), headers=KEY)
    r = client.post("/kpi/ingest", json=_history_batch(), headers=KEY)
    assert r.json()["inserted"] == 0 and r.json()["updated"] == 4
    assert client.get("/kpi/SE.XPD.TOTL.GB.ZS/history").json()["count"] == 3


def test_legacy_document_replaced(client):
    """Un document de l'ancien format (_id = code) ne doit pas doubler la ligne."""
    db = main.app.dependency_overrides[main.get_db]()
    legacy = dict(REAL["indicators"][0])
    db.indicators.insert_one({**legacy, "_id": legacy["code"]})
    client.post("/kpi/ingest", json=REAL, headers=KEY)
    assert client.get("/kpi/SE.XPD.TOTL.GB.ZS/history").json()["count"] == 1
