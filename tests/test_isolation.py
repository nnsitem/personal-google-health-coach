"""The suite must never touch the real database.

This exists because it already happened once: tests/__init__ used
os.environ.setdefault("COACH_DATA_DIR", ...), which is a no-op inside the
image, because the Dockerfile ships COACH_DATA_DIR=/app/data — the real
bind-mounted database. The run wrote 47 test users into production data and
executed the retention prune against it.
"""

import unittest

from coach.config import DATA_DIR, DB_PATH


class Isolation(unittest.TestCase):
    def test_database_is_not_the_production_one(self):
        self.assertNotIn("/app/data", str(DB_PATH))
        self.assertNotEqual(str(DATA_DIR), "/app/data")

    def test_database_lives_in_a_temp_directory(self):
        self.assertIn("coach-tests-", str(DB_PATH))


if __name__ == "__main__":
    unittest.main()
