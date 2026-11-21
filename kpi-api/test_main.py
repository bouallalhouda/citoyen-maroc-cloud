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
