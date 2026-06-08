import asyncio

from dev_lab.events import EventBus


def test_publish_to_subscribers():
    async def scenario():
        bus = EventBus()
        async with bus.subscribe() as a, bus.subscribe() as b:
            assert bus.subscriber_count == 2
            await bus.publish({"type": "x"})
            assert (await a.get())["type"] == "x"
            assert (await b.get())["type"] == "x"
        assert bus.subscriber_count == 0

    asyncio.run(scenario())


def test_publish_with_no_subscribers_is_noop():
    asyncio.run(EventBus().publish({"type": "x"}))


def test_full_queue_drops_instead_of_blocking():
    async def scenario():
        bus = EventBus(max_queue=1)
        async with bus.subscribe() as q:
            await bus.publish({"n": 1})
            await bus.publish({"n": 2})  # dropped; must not raise/block
            assert (await q.get())["n"] == 1
            assert q.empty()

    asyncio.run(scenario())
