import datetime
import http.client
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import duckdb
import video_browser as browser


class BulkRequestLimitTests(unittest.TestCase):
    def test_update_endpoints_reject_oversized_requests_without_changes(self):
        server = browser.ThreadingHTTPServer(("127.0.0.1", 0), browser.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(browser, "mark_files") as mark, patch.object(browser, "update_clips") as update:
                mark.return_value = update.return_value = {"ok": True, "updated": 1}
                for endpoint in ("/api/mark-deletion", "/api/favorite", "/api/update"):
                    for count in (browser.MAX_UPDATE_FILES, browser.MAX_UPDATE_FILES + 1):
                        with self.subTest(endpoint=endpoint, count=count):
                            mark.reset_mock()
                            update.reset_mock()
                            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                            body = json.dumps({"rels": ["F:/clip.mp4"] * count, "marked": True, "location": "Home"})
                            conn.request("POST", endpoint, body, {"Content-Type": "application/json", "X-Video-Browser": "1"})
                            response = conn.getresponse()
                            result = json.loads(response.read())
                            conn.close()
                            if count > browser.MAX_UPDATE_FILES:
                                self.assertEqual(response.status, 400)
                                self.assertFalse(result["ok"])
                                self.assertIn("no changes were made", result["error"])
                                mark.assert_not_called()
                                update.assert_not_called()
                            else:
                                self.assertEqual(response.status, 200)
                                self.assertTrue(result["ok"])
                                called = update if endpoint == "/api/update" else mark
                                self.assertEqual(len(called.call_args.args[0]), count)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class DuplicateRelativePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db, self.old_map = browser.DB_PATH, browser.PATH_MAP
        browser.DB_PATH = os.path.join(self.tmp.name, "catalog.duckdb")
        browser.PATH_MAP = []
        with duckdb.connect(browser.DB_PATH) as con:
            con.execute("""CREATE TABLE videos (
                filename VARCHAR, ext VARCHAR, kind VARCHAR, device VARCHAR,
                capture_date DATE, size_bytes BIGINT, modified TIMESTAMP,
                rel_path VARCHAR, full_path VARCHAR, location VARCHAR, project VARCHAR,
                collection VARCHAR, storage_root VARCHAR, nas_path VARCHAR, dup_of VARCHAR
            )""")
            for root in ("F:/", "H:/"):
                con.execute("INSERT INTO videos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            ["clip.mp4", ".mp4", "video", "camera", datetime.date(2024, 1, 1),
                             10, None, "DCIM/clip.mp4", root + "DCIM/clip.mp4", None, None,
                             "Raw", root, None, None])

    def tearDown(self):
        browser.DB_PATH, browser.PATH_MAP = self.old_db, self.old_map
        self.tmp.cleanup()

    def test_clip_identity_and_updates_are_scoped_to_full_path(self):
        clips = browser.load_data()["groups"][0]["clips"]
        self.assertEqual({c["rel"] for c in clips}, {"F:/DCIM/clip.mp4", "H:/DCIM/clip.mp4"})
        for clip in clips:
            self.assertEqual(clip["rels"], [clip["path"]])
            self.assertEqual(clip["pv"], [clip["path"]])
            self.assertEqual(clip["file_details"][0]["rel"], clip["path"])
        target = "F:/DCIM/clip.mp4"
        self.assertEqual(browser.lookup_path(target), target)
        self.assertEqual(browser.lookup_paths([target]), {target: target})
        self.assertEqual(browser.mark_files([target], True)["updated"], 1)
        self.assertEqual(browser.mark_files([target], True, "favorite")["updated"], 1)
        self.assertEqual(browser.update_clips([target], {"location": "Home"})["updated"], 1)
        with duckdb.connect(browser.DB_PATH) as con:
            rows = con.execute("SELECT full_path, marked_for_deletion, favorite, location FROM videos ORDER BY full_path").fetchall()
        self.assertEqual(rows, [(target, True, True, "Home"), ("H:/DCIM/clip.mp4", False, False, None)])


if __name__ == "__main__":
    unittest.main()
