import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from paperclerk.api.app import create_app  # noqa: E402
from paperclerk.config import Settings  # noqa: E402


def test_health_and_scan(inbox, tmp_path):
    settings = Settings(watched_folders=[inbox], db_path=tmp_path / "t.db")
    client = TestClient(create_app(settings))

    assert client.get("/api/health").json()["status"] == "ok"
    scan = client.post("/api/scan").json()
    assert (scan["new"], scan["removed"], scan["failed"]) == (2, 0, [])
    assert len(client.get("/api/documents").json()) == 2
