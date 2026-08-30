"""Webhook dedup and retention pruning."""

import unittest
from datetime import datetime, timedelta, timezone

from coach import db
from tests import support


class ClaimMessage(unittest.TestCase):
    """LINE can deliver the same event twice without flagging it as a redelivery;
    one such duplicate logged the same meal twice on 2026-08-19."""

    def test_second_claim_is_refused(self):
        uid = support.new_user()
        self.assertTrue(db.claim_message("MID-1", uid))
        self.assertFalse(db.claim_message("MID-1", uid))

    def test_distinct_ids_both_claimed(self):
        uid = support.new_user()
        self.assertTrue(db.claim_message("MID-A", uid))
        self.assertTrue(db.claim_message("MID-B", uid))

    def test_missing_id_does_not_block_the_message(self):
        # Fails open: losing a message is worse than a rare duplicate.
        self.assertTrue(db.claim_message("", support.new_user()))


class Prune(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.uid = support.new_user()

    def _sync_rows(self, days_ago, n=1):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            for _ in range(n):
                conn.execute("INSERT INTO sync_log (user_id, ts, data_type, ok) VALUES (?, ?, 'steps', 1)",
                             (self.uid, ts))

    def _count(self, table, where=""):
        with db.connect() as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ? {where}", (self.uid,)
            ).fetchone()[0]

    def test_old_sync_log_goes_recent_stays(self):
        self._sync_rows(days_ago=30, n=5)
        self._sync_rows(days_ago=1, n=3)
        db.prune_old_rows()
        self.assertEqual(self._count("sync_log"), 3)

    def test_food_log_history_is_never_pruned(self):
        # It is the nutrition history weekly reports read and deletes resolve
        # against — losing it would be silent data loss.
        old = (datetime.now(timezone.utc) - timedelta(days=900)).strftime("%Y-%m-%d %H:%M:%S")
        support.add_food_log(self.uid, kcal=500, ts=old)
        db.prune_old_rows()
        self.assertEqual(self._count("insights", "AND kind = 'food_log'"), 1)

    def test_old_nudges_are_pruned(self):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            conn.execute("INSERT INTO insights (user_id, ts, kind, content) VALUES (?, ?, 'nudge', '{}')",
                         (self.uid, old))
        db.prune_old_rows()
        self.assertEqual(self._count("insights", "AND kind = 'nudge'"), 0)

    def test_chat_history_trimmed_to_the_cap(self):
        keep = db.CHAT_HISTORY_KEEP_PER_USER
        with db.connect() as conn:
            for i in range(keep + 25):
                conn.execute("INSERT INTO chat_messages (user_id, ts, role, text) "
                             "VALUES (?, datetime('now', ?), 'user', ?)",
                             (self.uid, f"-{keep + 25 - i} minutes", f"m{i}"))
        db.prune_old_rows()
        self.assertEqual(self._count("chat_messages"), keep)
        with db.connect() as conn:
            newest = conn.execute("SELECT text FROM chat_messages WHERE user_id = ? "
                                  "ORDER BY ts DESC LIMIT 1", (self.uid,)).fetchone()[0]
        self.assertEqual(newest, f"m{keep + 24}")   # the tail kept, not the head

    def test_expired_dedup_keys_are_pruned(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        with db.connect() as conn:
            conn.execute("INSERT INTO processed_events (message_id, user_id, created_at) "
                         "VALUES ('OLD-MID', ?, ?)", (self.uid, old))
        db.claim_message("FRESH-MID", self.uid)
        db.prune_old_rows()
        with db.connect() as conn:
            rows = {r[0] for r in conn.execute(
                "SELECT message_id FROM processed_events WHERE user_id = ?", (self.uid,))}
        self.assertEqual(rows, {"FRESH-MID"})


class CoachMemory(unittest.TestCase):
    """db.get_coach_memory — the shared read used by chat.py (context + the
    'language' lookup), ai.py (daily brief), and food.py (diet facts), so the
    category column has one query to keep in sync instead of three."""

    def setUp(self):
        self.uid = support.new_user()

    def _save(self, name, content, category=None):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO coach_memory (user_id, name, content, category, updated_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (self.uid, name, content, category),
            )

    def test_returns_name_content_and_category(self):
        self._save("allergy", "peanuts", "diet")
        rows = db.get_coach_memory(self.uid)
        self.assertEqual(rows, [{"name": "allergy", "content": "peanuts", "category": "diet"}])

    def test_uncategorized_entries_have_none_category(self):
        self._save("favorite_snack", "mango")
        rows = db.get_coach_memory(self.uid)
        self.assertIsNone(rows[0]["category"])

    def test_category_filter_is_case_insensitive_and_excludes_others(self):
        self._save("allergy", "peanuts", "diet")
        self._save("target_weight", "70kg", "goal")
        rows = db.get_coach_memory(self.uid, category="DIET")
        self.assertEqual([r["name"] for r in rows], ["allergy"])

    def test_uncategorized_entries_are_excluded_when_filtering(self):
        self._save("favorite_snack", "mango")
        self.assertEqual(db.get_coach_memory(self.uid, category="diet"), [])

    def test_another_users_memory_is_never_returned(self):
        other = support.new_user()
        self._save("allergy", "peanuts", "diet")
        self.assertEqual(db.get_coach_memory(other), [])

    def test_limit_is_honored(self):
        for i in range(5):
            self._save(f"fact{i}", str(i))
        self.assertEqual(len(db.get_coach_memory(self.uid, limit=2)), 2)


if __name__ == "__main__":
    unittest.main()
