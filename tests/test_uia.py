"""core/uia.py: fails soft without comtypes, judges "editable" right, and reads elements once each.

The fakes stand in for comtypes' COM objects; only RealDesktopTests reads the real desktop, and
only to look (the focused element), never to change anything.
Run: .venv\\Scripts\\python.exe -m unittest tests.test_uia
"""
import threading
import unittest
from unittest import mock

from aj.core import uia
from aj.core.uia import Node


class WithoutComtypesTests(unittest.TestCase):
    def setUp(self):
        self.saved = (uia._module, uia._local)
        uia._module, uia._local = None, threading.local()

        def no_comtypes():
            raise ImportError("No module named 'comtypes'")

        patch = mock.patch.object(uia, "_import_comtypes", no_comtypes)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        uia._module, uia._local = self.saved

    def test_everything_says_it_cannot_tell(self):
        with self.assertLogs(uia.log, "WARNING") as logged:
            self.assertFalse(uia.available())
            self.assertIsNone(uia.focused_editable())
            self.assertIsNone(uia.focused_value())
            self.assertIsNone(uia.focused_node())
            self.assertIsNone(uia.scan(12345))
            self.assertIsNone(uia.node(12345))
            self.assertIsNone(uia.find(12345, control_type="Edit"))
            self.assertEqual(uia.find_all(12345, control_type="Edit"), [])
            self.assertFalse(uia.set_focus(Node(element=object())))
            self.assertIsNone(uia.value(Node(element=object())))
            self.assertIsNone(uia.caret_at_end(Node(element=object())))
        self.assertEqual(len(logged.records), 1, "the reason should be logged once, not per call")
        self.assertIn("comtypes", logged.output[0])


class FakeRange:
    """A text range as a position: (start, end) in characters."""

    def __init__(self, start: int, end: int):
        self.start, self.end = start, end

    def CompareEndpoints(self, mine, other, theirs):  # noqa: N802 - COM's name
        a = self.start if mine == 0 else self.end
        b = other.start if theirs == 0 else other.end
        return (a > b) - (a < b)


class FakeTextPattern:
    def __init__(self, length: int, selection):
        self.DocumentRange = FakeRange(0, length)
        self.selection = selection

    def QueryInterface(self, interface):  # noqa: N802
        return self

    def GetSelection(self):  # noqa: N802
        return self.selection


class FakeSelection:
    def __init__(self, *ranges):
        self.ranges = ranges
        self.Length = len(ranges)

    def GetElement(self, i):  # noqa: N802
        return self.ranges[i]


class FakeTextElement:
    def __init__(self, pattern):
        self.pattern = pattern

    def GetCurrentPattern(self, pattern_id):  # noqa: N802
        return self.pattern if pattern_id == uia._TEXT_PATTERN else None


class CaretTests(unittest.TestCase):
    """caret_at_end: "make it nicer" deletes only when the caret is at the very end, nothing
    selected (the review of 2026-10-22)."""

    def setUp(self):
        patch = mock.patch.object(uia, "_load", lambda: mock.Mock(IUIAutomationTextPattern=object()))
        patch.start()
        self.addCleanup(patch.stop)

    def caret(self, length, *ranges):
        selection = FakeSelection(*ranges) if ranges else FakeSelection()
        return uia.caret_at_end(Node(element=FakeTextElement(FakeTextPattern(length, selection))))

    def test_at_the_end(self):
        self.assertTrue(self.caret(12, FakeRange(12, 12)))

    def test_in_the_middle(self):
        self.assertFalse(self.caret(12, FakeRange(5, 5)))

    def test_text_selected_up_to_the_end(self):
        self.assertFalse(self.caret(12, FakeRange(3, 12)))  # a Backspace would delete the selection

    def test_it_cannot_be_told(self):
        self.assertIsNone(self.caret(12))  # no selection reported
        self.assertIsNone(self.caret(12, FakeRange(12, 12), FakeRange(1, 1)))
        self.assertIsNone(uia.caret_at_end(Node(element=FakeTextElement(None))))  # no text pattern
        self.assertIsNone(uia.caret_at_end(None))
        self.assertIsNone(uia.caret_at_end(Node(element=FakeTextElement(FakeTextPattern(3, FakeSelection(
            FakeRange(3, 3)))), password=True)))


class MultilineTests(unittest.TestCase):
    """Where a newline may be Shift+Enter: only in a box known to take line breaks (2026-10-22)."""

    def test_boxes_that_take_line_breaks(self):
        self.assertTrue(uia.multiline(Node(control_type="Document", has_text=True)))
        self.assertTrue(uia.multiline(Node(control_type="Edit", aria_properties="multiline=true;readonly=false")))
        self.assertTrue(uia.multiline(Node(control_type="Group", aria_properties="readonly=false;Multiline=true")))
        with mock.patch.object(uia._user32, "GetWindowLongW", return_value=0x50010004):  # ES_MULTILINE set
            self.assertTrue(uia.multiline(Node(control_type="Edit", class_name="Edit", native_hwnd=77)))

    def test_boxes_that_dont(self):
        self.assertFalse(uia.multiline(None))
        self.assertFalse(uia.multiline(Node(control_type="Edit")))  # a Chrome input: no "multiline=true"
        self.assertFalse(uia.multiline(Node(control_type="Edit", aria_properties="multiline=false")))
        self.assertFalse(uia.multiline(Node(control_type="ComboBox", class_name="Edit")))  # no window of its own
        with mock.patch.object(uia._user32, "GetWindowLongW", return_value=0x50010000):  # the Run box's edit
            self.assertFalse(uia.multiline(Node(control_type="Edit", class_name="Edit", native_hwnd=77)))
        with mock.patch.object(uia._user32, "GetWindowLongW", return_value=0x4) as style:  # not an edit control
            self.assertFalse(uia.multiline(Node(control_type="Pane", class_name="Button", native_hwnd=77)))
        style.assert_not_called()


class EditableTests(unittest.TestCase):
    def test_what_typing_goes_into(self):
        yes = {
            "an edit box": Node(control_type="Edit", has_value=True, focusable=True),
            "a writable document (Notepad)": Node(control_type="Document", has_value=True, has_text=True),
            "a combo box you can type in": Node(control_type="ComboBox", has_value=True),
            "WhatsApp's message box": Node(control_type="Edit", aria_role="textbox", has_value=True, has_text=True,
                                           focusable=True),
            "a contenteditable group (Google Docs)": Node(control_type="Group", has_value=True, has_text=True,
                                                          focusable=True),
            "a rich editor with only a text pattern (Word)": Node(control_type="Document", has_text=True,
                                                                  focusable=True),
            "a password box": Node(control_type="Edit", has_value=True, password=True),
        }
        no = {
            "a web page itself": Node(control_type="Document", has_value=True, read_only=True, has_text=True,
                                      focusable=True),
            "a read-only edit": Node(control_type="Edit", has_value=True, read_only=True),
            "a button": Node(control_type="Button", focusable=True),
            "a link with a URL value": Node(control_type="Hyperlink", has_value=True, read_only=True),
            "a read-only rich text view": Node(control_type="Document", has_text=True, focusable=True,
                                              legacy_state=0x40),
            "text that can't take the focus": Node(control_type="Document", has_text=True),
            "a list item": Node(control_type="ListItem", focusable=True),
            "a pane": Node(control_type="Pane", focusable=True),
        }
        for label, found in yes.items():
            with self.subTest(label):
                self.assertTrue(uia.editable(found))
        for label, found in no.items():
            with self.subTest(label):
                self.assertFalse(uia.editable(found))


class FakeElement:
    def __init__(self, props: dict | None = None):
        self.props = props or {}

    def GetCachedPropertyValue(self, prop):
        return self.props.get(prop, _NOT_SUPPORTED)


_NOT_SUPPORTED = object()  # UIA hands back a COM object for "not supported"


class FakeArray:
    def __init__(self, elements):
        self.elements = elements
        self.Length = len(elements)

    def GetElement(self, i):
        return self.elements[i]


class ReadingTests(unittest.TestCase):
    def test_cached_properties_become_a_node(self):
        element = FakeElement({uia._P_CONTROL_TYPE: 50004, uia._P_NAME: "Search or start a new chat",
                               uia._P_HAS_VALUE: True, uia._P_RANGE_VALUE: _NOT_SUPPORTED,
                               uia._P_BOUNDS: (1.0, 2.0, 3.0, 4.0), uia._P_RUNTIME_ID: (42, 7, 4, 1)})
        node = uia._node(element)
        self.assertEqual((node.control_type, node.name, node.has_value), ("Edit", "Search or start a new chat", True))
        self.assertIsNone(node.range_value)       # "not supported" is not a number
        self.assertEqual(node.automation_id, "")  # missing: the default
        self.assertEqual(node.bounds, (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(node.runtime_id, (42, 7, 4, 1))
        self.assertIs(node.element, element)

    def test_each_element_once(self):
        """Chromium's page can be reached twice; FindAll then lists every element many times."""
        a = FakeElement({uia._P_RUNTIME_ID: (42, 1), uia._P_CONTROL_TYPE: 50000})
        b = FakeElement({uia._P_RUNTIME_ID: (42, 2), uia._P_CONTROL_TYPE: 50004})
        nameless = FakeElement()
        found = uia._unique(FakeArray([a, b, a, b, a, nameless, nameless]))
        self.assertEqual([n.control_type for n in found], ["Button", "Edit", "", ""])
        self.assertEqual(uia._unique(None), [])

    def test_unknown_control_type_names_are_refused(self):
        with self.assertRaises(ValueError):
            uia._condition(mock.Mock(), "Textbox", None, None, None)


@unittest.skipUnless(uia.available(), "UI Automation isn't available here")
class RealDesktopTests(unittest.TestCase):
    """Read-only: what has the focus right now, asked from a worker thread as tools do."""

    def test_focus_questions_answer_from_a_worker_thread(self):
        answers = {}

        def ask():
            answers["editable"] = uia.focused_editable()
            answers["node"] = uia.focused_node()

        worker = threading.Thread(target=ask)
        worker.start()
        worker.join(10)
        self.assertIn(answers.get("editable"), (True, False, None))
        if answers.get("node") is not None:
            self.assertIsInstance(answers["node"].control_type, str)


if __name__ == "__main__":
    unittest.main()
