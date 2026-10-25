"""AJ's look: near-black with red. Colours are plain strings and the stylesheet is one
string, so this module needs no Qt and a colour change is a one-line edit here."""

BG = "#0d0d0f"
PANEL = "#16161a"
BORDER = "#2a2a30"
RED = "#c8102e"
RED_BRIGHT = "#ff2a2a"
RED_DARK = "#8e0b20"
TEXT = "#e8e6e3"
MUTED = "#8a8a92"
DIM = "#55555c"
FAINT = "#74747c"     # the latency lines: quieter than the words, still readable on PANEL
OFF_FILL = "#26262b"   # mic button when the mic is off
OFF_RING = "#3c3c44"
FONT = "Segoe UI"

STYLESHEET = f"""
QWidget {{ color: {TEXT}; font-family: "{FONT}"; font-size: 10pt; }}
QWidget#root {{ background: {BG}; }}
QLabel#title {{ color: {RED_BRIGHT}; font-size: 15pt; font-weight: 700; }}
QLabel#status {{ font-size: 12pt; }}
QLabel#caption {{ color: {MUTED}; font-size: 9pt; }}
QLabel#disclaimer {{ color: {FAINT}; font-size: 8pt; padding-top: 2px; }}

QPushButton#close {{
    background: transparent; color: {RED_BRIGHT}; border: 1px solid {RED};
    border-radius: 6px; padding: 5px 16px; font-weight: 600;
}}
QPushButton#close:hover {{ background: {RED}; color: white; }}
QPushButton#close:pressed {{ background: {RED_DARK}; color: white; }}
QPushButton#close:disabled {{ color: {DIM}; border-color: {BORDER}; }}

QTextEdit#transcript {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 8px;
    selection-background-color: {RED};
}}
QLineEdit#entry {{
    background: {PANEL}; border: 1px solid {BORDER}; border-radius: 6px; padding: 7px 10px;
    selection-background-color: {RED};
}}
QLineEdit#entry:focus {{ border-color: {RED}; }}
QLineEdit#entry:disabled {{ color: {DIM}; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px 2px 4px 0; }}
QScrollBar::handle:vertical {{ background: #3a3a42; border-radius: 3px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {RED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {RED}; padding: 4px; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; }}
QMenu::item {{ padding: 4px 18px; }}
QMenu::item:selected {{ background: {RED}; }}
QMenu::item:disabled {{ color: {DIM}; }}
"""
