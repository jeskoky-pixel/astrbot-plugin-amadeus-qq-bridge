import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_amadeus_conversation.runtime.activity import Activity, IncomingMessage
from astrbot_plugin_amadeus_conversation.storage.database import ActivityDatabase


class StorageTests(unittest.TestCase):
    def test_restart_transcript_is_not_multiplied_by_old_activities(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = ActivityDatabase(Path(tmp) / "activities.sqlite3")
            first = Activity("a", "qq:group:1", "qq:group:1", "u")
            second = Activity("b", "qq:group:1", "qq:group:1", "u")
            db.record_activity(first)
            db.record_activity(second)
            db.inbound_once(IncomingMessage("m1", first.umo, "u", "hello"), first.activity_id)
            transcript = db.transcript(first.umo, first.activity_id, 20)
            self.assertEqual(transcript.count("user: hello"), 1)
            db.close()

