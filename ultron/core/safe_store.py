"""Crash-safe JSON files (ported from SivaSafe's core/safe_store.py).

Path.write_text() truncates the file and then writes it, so a crash or power cut in
between leaves half a file and Ultron forgets what it was keeping. Writing a temporary
file in the same folder, fsyncing it and renaming it over the original avoids that:
os.replace() is atomic, so a reader sees the whole old file or the whole new one.

Unlike SivaSafe's version, a failed write raises. A reminder that silently wasn't saved
is worse than an error Ultron can say out loud.
"""
import json
import os
from pathlib import Path
from typing import Any


def write_json(path: Path, data: Any) -> None:
    """Replace `path` with `data` as JSON, atomically. Raises OSError on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")  # per process: two writers never share one
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())  # on disk before it becomes the real file
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any) -> Any:
    """The file's contents, or `default` if it doesn't exist.

    A file that isn't valid JSON is moved aside to `<name>.bad` (never deleted, so
    nothing is lost for good) and `default` is returned, so one bad write can't
    break the feature forever.
    """
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (ValueError, UnicodeDecodeError) as e:
        bad = path.with_name(path.name + ".bad")
        print(f"[store] {path.name} is unreadable ({e}); moved it to {bad.name}", flush=True)
        os.replace(path, bad)
        return default
