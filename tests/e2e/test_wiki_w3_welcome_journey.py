"""Real users retain role welcome state across the CI bringup server restart.

The live lane owns execution. This uses the existing restart helper and never
starts or restarts dogfood; collection alone performs no network/process work.
"""

import httpx

from tests._support.restart import restart_server, under_bringup


async def test_welcome_survives_restart_and_role_changes_without_sharing_state(client, base_url, unique_suffix):
    assert under_bringup(), "this persistence journey requires the CI bringup-managed server"
    people = []
    ids = []
    try:
        for number in (1, 2):
            username = f"welcome-{unique_suffix}-{number}"
            created = await client.post("/v1/admin/users", json={"username": username, "password": "welcome-temporary-123", "role": "user"})
            assert created.status_code == 201, created.text
            ids.append(created.json()["id"])
            person = httpx.AsyncClient(base_url=base_url, timeout=60)
            people.append(person)
            login = await person.post("/v1/auth/login", json={"username": username, "password": "welcome-temporary-123"})
            assert login.status_code == 200, login.text
            rotated = await person.post("/v1/auth/change-password", json={"current_password": "welcome-temporary-123", "new_password": "welcome-personal-123"})
            assert rotated.status_code == 200, rotated.text
            status = (await person.get("/v1/auth/status")).json()
            assert status["welcome"] == {"variant": "user", "show": True}
        one, two = people
        acknowledged = await one.post("/v1/auth/welcome/acknowledge", json={"role": "user"})
        assert acknowledged.status_code == 200, acknowledged.text
        restart_server(base_url)
        # Existing cookies still work: welcome writes did not bump session_epoch.
        for person, show in ((one, False), (two, True)):
            status = (await person.get("/v1/auth/status")).json()
            assert status["authenticated"] is True
            assert status["welcome"] == {"variant": "user", "show": show}
        for role, show in (("admin", True), ("user", False)):
            changed = await client.patch(f"/v1/admin/users/{ids[0]}", json={"role": role})
            assert changed.status_code == 200, changed.text
            login = await one.post("/v1/auth/login", json={"username": f"welcome-{unique_suffix}-1", "password": "welcome-personal-123"})
            assert login.status_code == 200, login.text
            assert (await one.get("/v1/auth/status")).json()["welcome"] == {"variant": role, "show": show}
            acknowledged = await one.post("/v1/auth/welcome/acknowledge", json={"role": role})
            assert acknowledged.status_code == 200, acknowledged.text
        reopened = await one.post("/v1/auth/welcome/reopen", json={"role": "user"})
        assert reopened.status_code == 200, reopened.text
        assert reopened.json() == {"variant": "user", "show": True}
        assert (await two.get("/v1/auth/status")).json()["welcome"] == {"variant": "user", "show": True}
    finally:
        for person in people:
            await person.aclose()
        for user_id in ids:
            deleted = await client.delete(f"/v1/admin/users/{user_id}")
            assert deleted.status_code == 204, deleted.text
