"""The test suite never touches the user's real data.

aj.config reads %APPDATA% once, when it is first imported, and every module that writes there
(the app index, the audit log, the memory database, the humor setting, the logs) takes its path
from it. Until 2026-10-22 the suite ran against his real %APPDATA%\\AJ: it rewrote apps_index.json
there (the review of 2026-10-22). So APPDATA is pointed at a fresh temporary folder here, before
anything imports aj.

`python -m unittest discover tests` imports the test files as top-level modules and never this
package, so tests/test__isolation.py (first in discovery's sorted order) imports it, and checks
the paths. Running one file (`python -m unittest tests.test_keyboard`) imports it as the package.
"""
import os
import sys
import tempfile

REAL_APPDATA = os.environ.get("_AJ_TESTS_REAL_APPDATA") or os.environ.get("APPDATA", "")

if "aj.config" in sys.modules and not os.environ.get("_AJ_TESTS_APPDATA"):
    # Too late: the real paths are already bound. Refuse rather than run against his data.
    raise RuntimeError("aj was imported before tests/__init__.py could point APPDATA at a temporary "
                       "folder; run the suite with: python -m unittest discover tests")

if not os.environ.get("_AJ_TESTS_APPDATA"):
    os.environ["_AJ_TESTS_REAL_APPDATA"] = REAL_APPDATA
    os.environ["_AJ_TESTS_APPDATA"] = tempfile.mkdtemp(prefix="aj-tests-")
    os.environ["APPDATA"] = os.environ["_AJ_TESTS_APPDATA"]  # child processes inherit it too

TEMP_APPDATA = os.environ["_AJ_TESTS_APPDATA"]

# The named events core/mic_share.py raises get names of the suite's own, so a test can never make
# the user's running assistant or AJ (Speak) think the other has his words.
os.environ.setdefault("AJ_SHARE_NAMESPACE", f"AJTest{os.getpid()}")
