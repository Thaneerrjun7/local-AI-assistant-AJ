"""AJ's window (PyQt6 + Qt Quick). Small files on purpose: SivaSafe's single 11k-line ui.py was
impossible to change safely, so each piece of the window lives in its own module.

  window        the window as the rest of AJ sees it (same methods and signals as ever)
  qml/          what it draws: Main.qml (the window), Face.qml (its content), the orb, the glass,
                the background warp, the springs and the clock (docs/UI_REDESIGN_PLAN.md)
  shaders/      the GPU programs behind the orb, the glass and the background, compiled ahead of
                time to .qsb (scripts/build_shaders.py)
  conversation  the conversation, as the list model the QML shows
  status        what each engine Status looks like: its words, the orb's look, an alarm colour
  native        Inter, the title bar, Windows' accessibility settings, the no-GPU check
  theme         colours and the stylesheet, for the parts still drawn as widgets (the yes-or-no box,
                the tray menu, AJ (Speak))
  mic_button    the microphone drawn with QPainter, and the app icon made from it
  bridge        engine thread -> Qt thread, as signals
  tray          the notification-area icon: sleep mode's way back (wake up, sleep, turn off)
  app           starts Qt, the window and the engine thread; makes closing really exit
  demo          a pretend engine for trying the window without loading any model
  lab           the orb and glass lab: every status, the tuning, the benches (Phase 0)
"""
