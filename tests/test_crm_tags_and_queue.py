import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from server import app, _make_session_token, SESSION_COOKIE_NAME


class FakeCrmRepo:
    def __init__(self):
        self.tags = set()
        self.statuses = [
            {"id": "status-1", "name": "New Lead", "color": "#3B82F6"},
            {"id": "status-2", "name": "Contacted", "color": "#F59E0B"},
        ]

    async def upsert_crm_tag(self, payload):
        self.tags.add(payload["name"])
        return payload

    async def list_crm_tags(self, limit=500):
        return [{"name": t} for t in self.tags]

    async def delete_crm_tag(self, name: str):
        if name in self.tags:
            self.tags.remove(name)
            return True
        return False

    async def upsert_lead_status(self, payload):
        for s in self.statuses:
            if s["name"] == payload["name"]:
                s.update(payload)
                return s
        new_row = {"id": f"status-{len(self.statuses)+1}", **payload}
        self.statuses.append(new_row)
        return new_row

    async def list_lead_statuses(self):
        return list(self.statuses)

    async def delete_lead_status(self, status_id: str):
        initial_len = len(self.statuses)
        self.statuses = [s for s in self.statuses if s.get("id") != status_id and s.get("name") != status_id]
        return len(self.statuses) < initial_len


@pytest.mark.asyncio
async def test_crm_tags_create_and_delete(monkeypatch):
    fake_repo = FakeCrmRepo()
    monkeypatch.setattr("server.get_repository", lambda: fake_repo)
    monkeypatch.setattr("server.get_crm_contacts", AsyncMock(return_value=[]))

    transport = ASGITransport(app=app)
    cookies = {SESSION_COOKIE_NAME: _make_session_token("admin")}
    async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
        # Create tag
        res = await client.post("/api/crm/tags", json={"tag": "TestQueueTag_123"})
        assert res.status_code == 200
        data = res.json()
        assert data.get("success") is True
        assert data.get("tag") == "TestQueueTag_123"

        # List tags
        res_list = await client.get("/api/crm/tags")
        assert res_list.status_code == 200
        tags = res_list.json().get("tags", [])
        assert "TestQueueTag_123" in tags

        # Delete tag
        res_del = await client.delete("/api/crm/tags/TestQueueTag_123")
        assert res_del.status_code == 200
        del_data = res_del.json()
        assert del_data.get("success") is True
        assert del_data.get("tag") == "TestQueueTag_123"
        assert del_data.get("deleted") is True

        # Verify deleted from list
        res_list_after = await client.get("/api/crm/tags")
        assert "TestQueueTag_123" not in res_list_after.json().get("tags", [])


@pytest.mark.asyncio
async def test_crm_statuses_workflow(monkeypatch):
    fake_repo = FakeCrmRepo()
    monkeypatch.setattr("server.get_repository", lambda: fake_repo)
    monkeypatch.setattr("db.get_repository", lambda: fake_repo)

    transport = ASGITransport(app=app)
    cookies = {SESSION_COOKIE_NAME: _make_session_token("admin")}
    async with AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
        # Add new status
        res = await client.post("/api/crm/statuses", json={"name": "QueueTestingStage", "color": "#123456"})
        assert res.status_code == 200
        created = res.json()
        assert created.get("success") is True

        # Get statuses
        res_get = await client.get("/api/crm/statuses")
        assert res_get.status_code == 200
        statuses = res_get.json().get("data", [])
        matching = [s for s in statuses if s.get("name") == "QueueTestingStage"]
        assert len(matching) > 0

        # Delete status
        status_id = matching[0].get("id") or "QueueTestingStage"
        res_del = await client.delete(f"/api/crm/statuses/{status_id}")
        assert res_del.status_code == 200
        assert res_del.json().get("success") is True

        # Verify deleted from list
        res_get_after = await client.get("/api/crm/statuses")
        assert all(s.get("name") != "QueueTestingStage" for s in res_get_after.json().get("data", []))
