import unittest
import tempfile
from pathlib import Path

from astrbot_plugin_amadeus_conversation.runtime.activity import Activity, IncomingMessage
from astrbot_plugin_amadeus_conversation.runtime.coordinator import Coordinator
from astrbot_plugin_amadeus_conversation.storage.database import ActivityDatabase


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.stopped = False

    def follow_up(self, *, message_text):
        self.calls.append(message_text)
        return object()

    def request_stop(self):
        self.stopped = True


class FakeEvent:
    def __init__(self):
        self.sent = []
    def plain_result(self, text):
        return text
    async def send(self, value):
        self.sent.append(value)


class FakeDB:
    def __init__(self):
        self.ins = set(); self.out = {}; self.rows = []
    def record_activity(self, a): self.rows.append(a)
    def update_activity(self, a, **kwargs): pass
    def inbound_once(self, m):
        if m.message_id in self.ins: return False
        self.ins.add(m.message_id); return True
    def outbound_status(self, x): return self.out.get(x)
    def reserve_outbound(self, x, a, t):
        if x in self.out: return False
        self.out[x] = "reserved"; return True
    def mark_outbound(self, x, s, error=None): self.out[x] = s


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_follow_up_reuses_same_runner(self):
        c = Coordinator(FakeDB(), {"max_follow_up_messages": 20})
        first = IncomingMessage("1", "qq:friend:u", "u", "hello")
        a = c.create(object(), first); a.phase = "running"; a.runner = FakeRunner()
        self.assertEqual(await c.route_follow_up(a, IncomingMessage("2", a.umo, "u", "next")), "follow_up")
        self.assertEqual(a.runner.calls, ["next"])
        self.assertEqual(await c.route_follow_up(a, IncomingMessage("2", a.umo, "u", "next")), "duplicate")

    async def test_wait_message_is_queued_without_follow_up(self):
        c = Coordinator(FakeDB()); a = c.create(object(), IncomingMessage("1", "q", "u", "hello")); a.phase = "waiting_user"; a.runner = FakeRunner()
        self.assertEqual(await c.route_follow_up(a, IncomingMessage("2", "q", "u", "reply")), "queued_for_wait")
        self.assertEqual(a.runner.calls, [])
        self.assertEqual((await a.wait_queue.get()).text, "reply")

    def test_same_umo_does_not_create_second_activity(self):
        c = Coordinator(FakeDB()); a = c.create(object(), IncomingMessage("1", "q", "u", "hello"))
        self.assertIs(c.get("q"), a)

    async def test_outbound_message_is_sent_once(self):
        c = Coordinator(FakeDB()); event = FakeEvent()
        a = c.create(event, IncomingMessage("1", "q", "u", "hello"))
        self.assertEqual((await c.send_once(a, "progress", "intermediate", action_id="a1"))["status"], "sent")
        self.assertEqual((await c.send_once(a, "progress", "intermediate", action_id="a1"))["status"], "duplicate")
        self.assertEqual(event.sent, ["progress"])

    async def test_stop_requests_runner_and_marks_activity(self):
        c = Coordinator(FakeDB()); a = c.create(object(), IncomingMessage("1", "q", "u", "hello")); a.runner = FakeRunner()
        await c.stop(a, "command")
        self.assertEqual(a.phase, "stopping")
        self.assertTrue(a.cancel_event.is_set())
        self.assertTrue(a.runner.stopped)

    async def test_wait_is_cancelled_without_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ActivityDatabase(Path(tmp) / "activities.sqlite3")
            c = Coordinator(db, {"wait_timeout_seconds": 0})
            first = IncomingMessage("1", "q", "u", "hello")
            a = c.create(object(), first)
            c.mark_consumed(first, a)
            task = __import__("asyncio").create_task(c.wait_for_user(a, 0))
            await __import__("asyncio").sleep(0)
            await c.stop(a, "test")
            self.assertEqual(await task, [])
            db.close()
