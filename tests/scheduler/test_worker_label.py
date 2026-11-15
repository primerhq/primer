"""Worker labels survive registration and appear on the membership API."""

from primer.scheduler.in_memory import InMemoryScheduler


async def test_worker_label_is_updated_on_registration():
    sched = InMemoryScheduler()
    await sched.initialize()
    try:
        await sched.register_worker(worker_id="w1", host="h", pid=1, capacity=4, label="first")
        assert (await sched.list_workers())[0].label == "first"
        await sched.register_worker(worker_id="w1", host="h", pid=2, capacity=4, label="second")
        assert (await sched.list_workers())[0].label == "second"
    finally:
        await sched.aclose()
