"""First in discovery's order (the double underscore sorts before every other test file), so the
temporary APPDATA of tests/__init__.py is in place before any test file imports aj. Then it checks
that every path AJ writes to is inside that folder, never the user's real %APPDATA%\\AJ (the review
of 2026-10-22: the suite used to rewrite his apps_index.json)."""
import tests  # noqa: F401  (must stay the first import: it points APPDATA at a temporary folder)

import os
import unittest
from pathlib import Path


class IsolationTests(unittest.TestCase):
    def test_every_data_path_is_in_the_temporary_folder(self):
        from aj import config
        from aj.memory import db
        from aj.tools import apps, dispatch, personality
        temp = Path(tests.TEMP_APPDATA).resolve()
        paths = [config.DATA_DIR, config.DB_PATH, config.LOG_DIR, config.SETTINGS_PATH, db.DB_PATH,
                 dispatch.AUDIT_PATH, apps.CACHE_PATH]
        paths += [v for v in vars(personality).values() if isinstance(v, Path)]
        for path in paths:
            with self.subTest(path=str(path)):
                self.assertTrue(path.resolve().is_relative_to(temp), f"{path} is outside {temp}")
        if tests.REAL_APPDATA:
            real = (Path(tests.REAL_APPDATA) / "AJ").resolve()
            for path in paths:
                self.assertFalse(path.resolve().is_relative_to(real))

    def test_child_processes_inherit_it(self):
        self.assertEqual(os.environ["APPDATA"], tests.TEMP_APPDATA)


if __name__ == "__main__":
    unittest.main()
