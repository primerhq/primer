"""Worker membership exposes the registered label."""


async def test_worker_label_in_api(client, app):
    await app.state.scheduler.register_worker(worker_id="w1", host="h", pid=1, capacity=4, label="worker-a")
    response = await client.get("/v1/workers")
    assert response.status_code == 200
    assert response.json()["items"][0]["label"] == "worker-a"
