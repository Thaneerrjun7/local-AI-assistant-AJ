"""Make the public copy of Ultron (for GitHub) from this private one.

The private repo keeps its full history, and some of what's in it stays private: the owner's
signature lines (adapted from a film's dialogue), personal paths in the design plan, a one-off
migration script. The public repo gets a fresh history built from the current files minus those:

    .venv\\Scripts\\python.exe scripts\\export_public.py           # into ..\\Ultron-public
    .venv\\Scripts\\python.exe scripts\\export_public.py D:\\other  # somewhere else

It stops, with the copy left for inspection, if any copied file still contains the Windows
user name (which is in every personal path too) or one of the owner's lines from
data/signature_lines.json. Example paths like C:\\Users\\me in tests are fine.
Committing and pushing there are left to the owner (the first push asks them to sign in).
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = {"docs/PLAN.md", "docs/ULTRON_RESUME_NOTES.md", "scripts/import_sivasafe_memory.py"}


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if f and f not in PRIVATE]


def forbidden() -> list[str]:
    """Strings that must not appear in the public copy (compared case-insensitively)."""
    name = os.environ.get("USERNAME", "")
    found = [name] + [part for part in name.split() if len(part) >= 4]
    own = ROOT / "data" / "signature_lines.json"
    if own.exists():
        for line in json.loads(own.read_text(encoding="utf-8")).values():
            for piece in re.split(r"[.!?…]+", line["text"]):
                piece = " ".join(re.sub(r"\[[^\]]*\]", "", piece).split())
                if len(piece) >= 12:  # long enough to be the line, not a common phrase
                    found.append(piece)
    return [f.lower() for f in found if f]


MARK = Path(".git") / "ultron-public-export"  # inside .git, so it's never published


def prepare(target: Path) -> None:
    """A fresh export folder (a git repo marked as ours), or a previous export emptied of
    everything but its git history. Never some other folder."""
    target.mkdir(parents=True, exist_ok=True)
    if not any(target.iterdir()):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
        (target / MARK).write_text("made by scripts/export_public.py\n", encoding="utf-8")
        return
    if not (target / MARK).exists():
        sys.exit(f"{target} isn't empty and isn't a previous export: not touching it.")
    for item in target.iterdir():
        if item.name != ".git":
            shutil.rmtree(item) if item.is_dir() else item.unlink()


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "Ultron-public"
    prepare(target)
    files = tracked()
    for rel in files:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)

    bad = forbidden()
    problems = []
    for rel in files:
        try:
            text = (target / rel).read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue  # not text
        problems += [f"{rel}: contains {b[:20]!r}..." for b in bad if b in text]
    if problems:
        print("Not safe to publish yet:\n  " + "\n  ".join(problems))
        return 1

    subprocess.run(["git", "add", "-A"], cwd=target, check=True)
    print(f"Exported {len(files)} files to {target}; left out: {', '.join(sorted(PRIVATE))}.")
    print("Checked: no user name (so no personal paths) and none of the owner's own lines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
