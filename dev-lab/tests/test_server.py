import asyncio
import json

import websockets
from dev_lab.events import EventBus
from dev_lab.queue import FileQueue
from dev_lab.server import handle_client_message, make_handler


def test_submit_enqueues_and_acks(tmp_path):
    q = FileQueue(tmp_path)

    async def scenario():
        reply = await handle_client_message(
            json.dumps({"type": "submit", "instruction": "do x"}), q, default_repo="/r"
        )
        assert reply["type"] == "ack"
        assert reply["job_id"]
        return reply

    asyncio.run(scenario())
    assert q.counts()["pending"] == 1


def test_bad_json_and_unknown_type(tmp_path):
    q = FileQueue(tmp_path)

    async def scenario():
        assert (await handle_client_message("not json", q, default_repo=None))["type"] == "error"
        assert (
            await handle_client_message(json.dumps({"type": "nope"}), q, default_repo=None)
        )["type"] == "error"
        assert (
            await handle_client_message(json.dumps({"type": "submit"}), q, default_repo=None)
        )["type"] == "error"

    asyncio.run(scenario())
    assert q.counts()["pending"] == 0


def test_websocket_round_trip(tmp_path):
    async def scenario():
        q = FileQueue(tmp_path)
        bus = EventBus()
        async with websockets.serve(make_handler(q, bus, default_repo="/r"), "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.send(json.dumps({"type": "submit", "instruction": "do x"}))
                ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert ack["type"] == "ack"
                # an event published on the bus is forwarded to the connected client
                await bus.publish({"type": "job_done", "job_id": ack["job_id"]})
                evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert evt["type"] == "job_done"
        assert q.counts()["pending"] == 1

    asyncio.run(scenario())
