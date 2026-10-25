"""`python -m aj.speak` (the "AJ (Speak)" desktop shortcut runs it with pythonw)."""
import sys

from aj.speak.app import main

if __name__ == "__main__":
    sys.exit(main())
