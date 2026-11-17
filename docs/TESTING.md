# Testing AJ on real hardware

Unit tests run against fakes (no model, microphone, speakers, windows or network). On top of those,
two scripts check AJ end to end on the laptop it runs on: an RTX 4050 Laptop GPU (6 GB), a Ryzen 7
7840HS and 16 GB of RAM, under Windows 11. Last run: 18 November 2026.

| | What it checks | Command |
|---|---|---|
| Unit tests | 1,099 tests across every module | `.venv\Scripts\python.exe -m unittest discover tests` |
| Every tool, for real | each of the 49 tools run back to back on the laptop, with its effect read back | `.venv\Scripts\python.exe scripts\check_tools.py` |
| Tool use by the model | 30 everyday requests through the real brain and model, with a fake tool runner | `.venv\Scripts\python.exe scripts\check_tool_picks.py` |
| Voices | the same lines in each candidate voice: pitch steadiness, clarity, first-sound time | `.venv\Scripts\python.exe scripts\voice_samples.py` |

## 1. Every tool, run for real

`check_tools.py` runs each tool through the same permission dispatcher AJ uses and checks the result
against the laptop itself: the volume and brightness are read back, files are looked for on disk,
windows and Chrome tabs are counted before and after. It leaves the laptop as it found it (volume,
brightness, settings, tabs, a test folder that is removed) and never runs anything that would lock
the screen or switch the laptop off.

| Result | Tools | Count |
|---|---|---|
| Works, and the effect was checked | calculate, current_time, battery_status, disk_space, laptop_status, list_apps, get_weather, web_search, what_do_you_know, personality_setting, set_humor, forget_fact, set_volume, mute, set_brightness, screenshot, open_app, switch_to_app, snap_window, close_app, find_files, read_text_file, list_folder, copy_file, rename_file, move_file, delete_file, undo_last, open_folder, create_reminder, list_reminders, start_timer, cancel_reminder, open_website (7 tab actions), whatsapp_open_chat, open_speak, cancel_shutdown | 37 |
| Works, effect can't be read back | media (play/pause is a key press) | 1 |
| Refused without a yes, as designed | sleep_laptop, restart_laptop, shutdown_laptop, sign_out | 4 |
| Bug found and fixed (unit-tested; live re-run pending) | type_text, play_youtube | 2 |
| Not run live | lock_screen (would lock the screen mid-test); start_dictation, type_that, polish_typed (need speech); open_file | 5 |

### Bugs the live run found, and the fixes

| Found | Fix |
|---|---|
| `calculate` refused "18% of 87.60", although the prompt sends every percentage to it | "X% of Y" is a share, not modulo; spoken sums ("17 times 23") are accepted too |
| `type_text` said "Typed it." when an app that was still starting dropped most of the keystrokes | the text box is read back after typing; if the words aren't there, AJ says only part went in |
| `play_youtube` sometimes couldn't pick a video: YouTube serves more than one page layout | a second parser for the newer layout |
| `whatsapp_open_chat` took 119 s on a cold start while WhatsApp synced | not a code fault; noted |
| `undo_last` can't bring a file back out of the Recycle Bin | by design: AJ says so and points to the Recycle Bin |

## 2. Does the model use its tools, or just talk?

A 4B model sometimes answers "Opening YouTube." without calling anything. Three layers deal with it,
in order: instant commands that never reach the model, the model's own tool calls, and, when a
reply claims an action no tool performed, a second request under a JSON grammar that can only name
a tool (or "none"). This llama.cpp build ignores `tool_choice: "required"`, which was measured first.

| Route | Requests (of 30) |
|---|---|
| Instant command, no model | 18 |
| The model called the right tool | 8 |
| Rescued under the grammar | 2 |
| Answered correctly with no tool (nothing could do it) | 1 |
| Missed | 1 ("open the app I use for spreadsheets": it looked up the app list, then asked which app) |

Fixes from this run: the app list no longer ends an "open ..." request; "tone down the jokes" and
"pull up my Gmail" became instant commands; and "Humor at 45 percent" or "I've turned the heater on",
said with no tool behind them, are now caught by the honesty check and never spoken.

## 3. Features added and checked in the same pass

- **Chrome tabs** through the existing `open_website` tool, read through Windows UI Automation (about
  0.1 s for 15 tabs): go to the tab that has a site open, go to the fifth tab, open a new tab, close a
  tab by name or the current one, close one and open a new one, and list what's open. Each action is
  read back from the tab strip before it is reported as done.
- **Two commands in one sentence**, e.g. "lower the brightness by 50% and increase the volume to
  100%", run in order; volume and brightness now take a "by" amount.
- **Room for longer conversations:** llama-server's two slots share one KV pool (`-kvu`), so a turn can
  use all 16,384 tokens of context in the same graphics memory; a prompt that is still too long is
  retried with less history.
- **Voice control handled before the model:** "turn off the mic" or "mute" stops AJ at once and turns
  the mic off; "mute yourself" only stops him speaking.

## 4. Voice choice

Seventeen British voices were rendered on the same five lines, chunked exactly as AJ speaks them, and
scored for pitch jumps between pieces (the "different speaker" effect) and for how many words
speech-to-text understood. The default moved from Piper's northern English male (3.0 semitones,
53/58 words) to Piper's "Obadiah" (0.4 semitones, 58/58), which still starts speaking in about 0.1 s
on the CPU.
