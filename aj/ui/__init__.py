"""AJ's window (PyQt6). Small files on purpose: SivaSafe's single 11k-line ui.py was
impossible to change safely, so each piece of the window lives in its own module.

  theme       colours and the stylesheet
  status      what each engine Status looks like on the status line
  mic_button  the big round mic button (and the app icon)
  transcript  the scrolling conversation
  window      the window itself
  bridge      engine thread -> Qt thread, as signals
  tray        the notification-area icon: sleep mode's way back (wake up, sleep, turn off)
  app         starts Qt, the window and the engine thread; makes closing really exit
  demo        a pretend engine for trying the window without loading any model
"""
