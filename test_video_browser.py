import datetime
import os
import tempfile
import unittest

import duckdb
import video_browser as browser


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
