"""How a reply is delivered, sentence by sentence: the small spoken reactions between
sentences ("Hm.", "Right.", "Well..."), sighs as words, and which asides he has already said
too recently to say again.

The user's complaints (2026-10-06): he kept repeating the same lines ("the humanity lines")
and sounded flat; then, hearing synthesized breaths, "fake and unnatural... include
enunciated words in between some sentences". So:
- a sentence that is nearly one he said in the last few turns is dropped, unless it's the
  answer itself (the first sentence), so a repeated quip never gets a second airing;
- a remark about humanity or "your species" is allowed once in a while, not every turn,
  unless the conversation is actually about people;
- now and then a spoken reaction before an explanation or between longer sentences, never
  the same one twice running, never where he already reacted himself;
- a sigh ("*sighs*" from the model) is said as a word, "Hmm." or "Ugh.", and rationed so
  it stays a reaction rather than a tic.

And the one rule above all of those, accuracy (the user's, from the start): a sentence that
states something about the user or this laptop that nobody told him is dropped. Live, the 4B
model kept inventing ("still monitoring your schedule", "your morning coffee choices",
"battery at 63 percent", "your grades are below average") despite the persona forbidding it.
What counts as told: the user's words this turn and the last two, what memory knows about
him, and tool results. Only claims are caught: "your <thing>", a laptop reading with a
number, "I'm watching you"; general knowledge ("caffeine blocks adenosine") and questions
("did you sleep?") pass.

Three rules were tightened after the user's session of 2026-10-18, where he said "Battery
level is 17 percent. Laptop is unplugged." (it was 80% and plugged in), "Message sent." right
after opening WhatsApp, "Screen is now locked." and "System is now powered down.":
- a claim that something was done needs the tool that does it to have succeeded this turn
  ("sent" needs send_whatsapp, "locked" needs lock_screen); any tool at all no longer counts,
  and passive claims ("Message sent.", "is now locked") count as claims;
- a number in a laptop reading must be one he was given; the word "battery" in the user's
  question no longer vouches for whatever percentage follows;
- plugged in, unplugged or charging must come from a reading (a tool or the live status),
  never from the user's own question ("am I plugged in?").

2026-10-22, typing and WhatsApp chats (the user: "I do not want this AI to type the message and
send it for me. I'll do that myself."):
- nothing can send a message any more, so "sent" is never true, whatever ran;
- "Typed it." and "Written down." need a typing tool to have worked this turn (live, "Write this
  down." was answered "Written down." with no tool run); "Opened Appa's chat." needs
  whatsapp_open_chat;
- "Press send when you're ready." tells the user what to do: an instruction, not a claim.

From the acceptance run with the real model (the review of 2026-10-22):
- "I want to talk to Suhinish on WhatsApp" got switch_to_app, then "Suhinish's chat is open."
  Any claim that a chat is open ("X's chat is open", "Opened X's chat", "You're in X's chat") now
  needs whatsapp_open_chat itself to have succeeded this turn; opening the app isn't enough.
- About 7 replies in 18 ended with a stock offer of help ("How can I assist you further?"),
  against the prompt. A stock offer after the answer is dropped: "How can I help/assist (you)
  (further)?", "Is there anything else...?", "Let me know if...", "Anything else?", "What else can
  I do for you?". A real follow-up question about the topic ("Want a reminder?") stays, and so
  does an offer that is the whole reply.
- "No issues with the system, just like last time." There is no last time unless the background
  notes (memory) or the user say so: a sentence about a past session ("last time", "as usual",
  "like before") is dropped otherwise.

The other way round, 2026-10-23: asked to open Settings, Claude and Word, all installed, he said
"I can't open settings directly.", "Claude is not a valid app I can access." and "Only Chrome and
Claude are available." without trying once. When the user asks to open something or asks about
apps, a sentence saying an app can't be opened or isn't available is dropped until an app tool
has run this turn; the brain then tells him to call open_app (false_refusals).
"""
import random
import re
from collections import deque

from aj.core.chunker import strip_marks

SIMILAR = 0.6            # share of words two sentences have in common to count as the same remark
REMEMBER_SENTENCES = 40  # roughly the last ten turns
HUMANITY_EVERY = 6       # turns between remarks about humanity
SIGH_EVERY = 3           # turns between sighs
MAX_REACTIONS = 1        # spoken reactions added in one reply (v4 adds its own "Hm." too: two read as a tic)

OPENERS = ["Right.", "Hm.", "Well...", "Ah.", "Okay, so..."]  # before an explanation
BETWEEN = ["Hm.", "Mm.", "Right.", "Well...", "So..."]        # between longer sentences
SIGHS = ["Hmm."]  # "Ugh." went with the old rude character (v5, 2026-10-21)

# "A joke about printers: ..." or "Here's one:": a label the model puts before a joke (v6, 2026-10-22).
_JOKE_LABEL = re.compile(r"^\W*(?:(?:a|here(?:'s| is)(?: a)?|ok(?:ay)?,? here(?:'s| is)(?: a)?)\s+)?(?:[\w-]+\s+)?"
                         r"(?:joke|one)(?:\s+about\s+[\w' -]+?)?\s*:\s*", re.IGNORECASE)
HUMANITY = re.compile(r"\b(humanity|humans?|human race|mankind|your species|species|civili[sz]ation)\b",
                      re.IGNORECASE)
_ABOUT_PEOPLE = re.compile(r"\b(human|humans|humanity|people|species|mankind|society|civili[sz]ation|"
                           r"evolution|evolve|world)\b", re.IGNORECASE)
_REACTION = re.compile(r"^\W*(hm+|mm+|ah+|oh|well|right|okay|ok|so|uh+|um+|huh|ugh|hah)\b", re.IGNORECASE)
_SIGH_MARK = re.compile(r"\[sigh\]\s*")
# Taking back the user's own line straight after saying it.
_TAKES_IT_BACK = re.compile(r"^\W*(?:well|okay|ok|so|hm+|ah)?\W*(?:that'?s|that is|it'?s|it is|this is)\s+(?:not|n't)\b"
                            r"|^\W*(?:well|okay|ok|so)?\W*(?:not (?:really|quite|exactly)|just kidding|actually,? no|"
                            r"that'?s not (?:me|quite|really|right|true))\b|\bthat'?s not (?:quite )?(?:right|true|me)\b",
                            re.IGNORECASE)
_SENTENCE_DONE = re.compile(r"[.!?…][\"')\]]*$")
_WORD = re.compile(r"[a-z']{3,}")

# What he can't know about the user or this laptop unless told.
_THINGS = (r"schedules?|calendars?|meetings?|appointments?|agendas?|deadlines?|routines?|grades?|gpa|exams?|"
           r"homework|assignments?|coursework|attendance|coffee|tea|breakfast|lunch|dinner|snacks?|diet|"
           r"caffeine|meals?|sleep|bedtime|inbox|emails?|notifications?|messages|habits?|mood|"
           r"battery|cpu|processor|fans?|disk|storage|wi-?fi")
_YOUR_THING = re.compile(rf"\byour\s+(?:[\w'-]+\s+){{0,2}}?(?P<thing>{_THINGS})\b", re.IGNORECASE)
# Not "temperature": the weather is full of temperatures, and they come from the forecast.
_STATUS = re.compile(r"\b(battery|charge|cpu|processor|fans?|ram|disk|storage|volume|brightness)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_UNITS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
          "seventeen eighteen nineteen").split()
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if i > 1}
_NUMBER_WORD = re.compile(r"\b(?:" + "|".join(_UNITS + list(_TENS) + ["hundred", "and"]) + r")\b(?:[\s-]+(?:"
                          + "|".join(_UNITS + list(_TENS) + ["hundred", "and"]) + r")\b)*", re.IGNORECASE)


def numbers_in(text: str) -> set[str]:
    """The numbers a sentence states, as digits, written either way: "80" and "eighty", "seventeen",
    "one hundred and five". Since v5 the model sometimes writes numbers as words, and a made-up
    "seventeen percent" must be caught like "17 percent"."""
    found = set(_NUMBER.findall(text))
    for run in _NUMBER_WORD.finditer(text):
        total = current = 0
        seen = False
        for word in re.split(r"[\s-]+", run.group(0).lower()):
            if word in _UNITS:
                current += _UNITS.index(word)
                seen = True
            elif word in _TENS:
                current += _TENS[word]
                seen = True
            elif word == "hundred" and seen:
                current *= 100
        if seen:
            found.add(str(total + current))
    return found
# Plugged in or not: only a reading can say, and what he says must match it. The user's own
# question ("am I plugged in?") vouches for nothing.
_UNPLUGGED = re.compile(r"\b(unplugged|not plugged(?: in)?|on battery(?: power)?|running on (?:the )?battery|"
                        r"discharging)\b", re.IGNORECASE)
_PLUGGED = re.compile(r"\b(plugged in|charging|on (?:ac|mains) power)\b", re.IGNORECASE)
# A reading of this laptop, not general knowledge ("a CPU runs at 3 GHz").
_THIS_LAPTOP = re.compile(r"^\W*(?:the\s+|your\s+)?(?:battery|charge|volume|brightness|cpu|ram|disk|storage)\b|"
                          r"\b(?:your|the laptop|this laptop|the computer|this computer|you're|you are|you've got|"
                          r"you have|it's at|is at|is now|currently|right now|left|remaining)\b", re.IGNORECASE)


def plug_state(text: str) -> bool | None:
    """True for plugged in, False for on battery, None if the text doesn't say."""
    if _UNPLUGGED.search(text):
        return False
    return True if _PLUGGED.search(text) else None
_READING = re.compile(r"\d|\b(percent|degrees|stable|normal|charging|charged|full|low)\b", re.IGNORECASE)
_WATCHING = re.compile(r"\b(monitor\w*|watch(?:ing|ed)?|track(?:ing|ed)?|observ\w*|logg(?:ing|ed))\b", re.IGNORECASE)
_YOU = re.compile(r"\b(you|your|boss|sir)\b", re.IGNORECASE)
_TASKS = re.compile(r"\b(no (?:new |pending )?tasks|tasks? (?:assigned|pending|queued))\b", re.IGNORECASE)
_ANY_WORD = re.compile(r"[a-z][a-z'-]*")
# Things done, and the tools that make saying so true (one of them, succeeding this turn). None:
# no tool can, so it's never true ("ordered", "emailed"). A verb is matched as a claim only in
# the shapes claims take: at the start ("Sent.", "Opening WhatsApp.", "Done, I've muted it."),
# after "I" ("I opened WhatsApp and sent the message"), in the passive ("Screen is now locked.",
# "Message sent."), or as a promise ("I'll set a reminder").
_OPENS = {"open_app", "switch_to_app", "open_website", "open_file", "open_folder", "play_youtube",
          "whatsapp_open_chat", "open_speak"}
_TYPED = {"type_text", "type_that", "polish_typed"}  # put words where the cursor is
_POWER = {"shutdown_laptop", "restart_laptop", "sleep_laptop", "sign_out"}
_ACTIONS: list[tuple[str, set[str] | None]] = [
    # Nothing sends: the user presses Send himself (2026-10-22).
    (r"sen[dt]|sending|messaged|texted|deliver(?:ed|ing)?|pass(?:ed)? (?:it|that) (?:on|along)|"
     r"(?:pressed|tapped|clicked) (?:on )?(?:the )?send", None),
    # "Typed it." needs the typing done; "I'm typing what you say" may follow start_dictation too.
    # Not "type" or "write it down" themselves: "That's type 2." and "Write it down." claim nothing
    # (promises are _TYPE_PROMISE below).
    (r"typed|written (?:it |that |this )?(?:down|in|out|up)|wrote (?:it |that |this )?(?:down|in|out|up)|"
     r"(?:taken|took|jotted) (?:it |that |this )?down", _TYPED),
    (r"typing|writing (?:it |that |this )?(?:down|in|out|up)|taking (?:it |that |this )?down",
     _TYPED | {"start_dictation"}),
    (r"lock(?:ed|ing)?", {"lock_screen"}),
    (r"open(?:ed|ing)?|launch(?:ed|ing)?|switch(?:ed|ing)? (?:over )?to|brought up|bringing up|pulled up|pulling up",
     _OPENS),
    (r"clos(?:e|ed|ing)|quit", {"close_app"}),
    (r"(?:un)?mut(?:e|ed|ing)", {"mute"}),
    (r"power(?:ed|ing)? (?:down|off)|shut(?:ting)? (?:it |the laptop |everything )?down|restart(?:ed|ing)?|"
     r"reboot(?:ed|ing)?|sign(?:ed|ing)? (?:you )?out|log(?:ged|ging)? (?:you )?out|asleep|"
     r"(?:put|putting) (?:it|the laptop|the system) to sleep", _POWER),
    (r"delet(?:e|ed|ing)|binned|recycled", {"delete_file"}),
    (r"mov(?:e|ed|ing)", {"move_file", "undo_last"}),
    (r"cop(?:y|ied|ying)", {"copy_file"}),
    (r"renam(?:e|ed|ing)", {"rename_file"}),
    (r"play(?:ed|ing)?|paus(?:e|ed|ing)|resum(?:e|ed|ing)|skipp(?:ed|ing)|skip", {"media", "play_youtube"}),
    (r"saved|captured", {"screenshot", "move_file", "copy_file"}),
    (r"set|scheduled?|scheduling|created?|creating|added|cancel+(?:ed|ing)?|remind(?:ed|ing)?",
     {"create_reminder", "start_timer", "cancel_reminder", "set_volume", "set_brightness"}),
    (r"turn(?:ed|ing)? (?:it |the volume |the sound |the brightness )?(?:up|down)|rais(?:e|ed|ing)|"
     r"lower(?:ed|ing)?|dimm(?:ed|ing)|brighten(?:ed|ing)?", {"set_volume", "set_brightness"}),
    # "I've turned the heater on" (tests/test_tool_fixes.py, 2026-11-18): a thing between "turned" and
    # "on" got past this as "turned it on" only.
    # Past tense only for that: "Turn the lights off when you leave" is advice, not a claim.
    (r"turn(?:ed|ing)? (?:it |them |that )?(?:on|off)|turned (?:the|your|my) [\w-]+(?: [\w-]+)? (?:on|off)",
     {"mute", "set_volume", "set_brightness", *_POWER}),
    # Work in general ("Today I processed tasks."): true after any tool.
    (r"processed|handled|monitored|checked|scanned|organi[sz]ed|analy[sz]ed|reviewed|cleaned|optimi[sz]ed|"
     r"updated|backed up|fixed|sorted|taken care of", set()),
    (r"activated|deactivated|enabled|disabled|connected|disconnected|installed|ordered|booked|bought|"
     r"emailed|called|dialled|dialed|printed|uploaded|downloaded|written to|wrote (?:it )?to", None),
]
_SHAPES = [
    r"^\W*(?:(?:done|okay|ok|fine|right|alright|sure|there)\W+)?(?:(?:i|we)(?:'ve| have)?\s+)?"
    r"(?:just\s+|now\s+|already\s+|successfully\s+)?(?:{v})\b",
    r"\bi(?:'ve| have)?\s+(?:just\s+|already\s+|now\s+|successfully\s+)?(?:{v})\b",
    r"\bi(?:'ve| have)?\s+(?:just\s+|already\s+)?\w+ed\b[^.?!]{{0,40}}?\band\s+(?:then\s+|also\s+)?(?:{v})\b",
    r"\b(?:is|are|was|were|has been|have been|'s|'re|got)\s+(?:now\s+|all\s+|successfully\s+|been\s+|already\s+)*"
    r"(?:{v})\b",
    r"^\W*(?:the\s+|your\s+|a\s+)?[\w'-]+(?:\s+[\w'-]+)?\s+(?:now\s+|successfully\s+)?(?:{v})\W*$",
    r"^\W*(?:the\s+|your\s+|a\s+|that\s+)?(?:reminders?|timers?|alarms?|messages?|texts?|screen|laptop|system|"
    r"computer|volume|brightness|sound|music|apps?|files?|folders?|screenshots?|it|that)\s+"
    r"(?:(?:is|was|has been|got)\s+)?(?:now\s+|successfully\s+)?(?:{v})\b",
    r"\bi(?:'ll| will|'m going to| am going to|'m about to)\s+(?:now\s+|just\s+|go ahead and\s+)?(?:{v})\b",
    # "Now I'm sending a message to Appa." (2026-10-21)
    r"\bi(?:'m| am)\s+(?:now\s+|just\s+|already\s+|currently\s+)?(?=\w+ing\b)(?:{v})\b",
]
_CLAIMS = [([re.compile(shape.format(v=verbs), re.IGNORECASE) for shape in _SHAPES], tools)
           for verbs, tools in _ACTIONS]
# "Wi-Fi is now active", "it's now off": switched by him, so only these tools could have.
_NOW_SWITCHED = re.compile(r"\b(?:is|are|'s|'re)\s+now\s+(?:active|on|off|enabled|disabled|live)\b", re.IGNORECASE)
# "Mic is off, sir." (live, 2026-11-18, after "off your mic."): he can't switch his own mic. Since
# then "turn off the mic" and "mute" are done in code before the model hears them, so the model
# saying it is always made up. "Say 'turn off the mic'" is telling the user how, not a claim.
_MIC_CLAIM = re.compile(r"\b(?:mic|microphone|mike)\b[^.?!]{0,20}?\b(?:off|muted|disabled|closed)\b|"
                        r"\b(?:turned|switched|shut|muted|closed)\s+(?:off\s+)?(?:the\s+|my\s+|your\s+)?"
                        r"(?:mic|microphone|mike)\b|\b(?:muted|silenced)\s+(?:myself|me|you)\b|\bi'?m\s+(?:now\s+)?muted\b",
                        re.IGNORECASE)
_HUMOR_CLAIM = re.compile(r"\bhumou?r(?: setting)?(?: is)?(?: now)? (?:at|set to|down to|up to|to) \d+", re.IGNORECASE)
_TELLS_HOW = re.compile(r"\b(?:say|click|press|tap|use|button)\b", re.IGNORECASE)
# "I'll message Appa." (2026-10-21): "message" as a verb, only in a promise ("Message: Hi Mum..."
# at the start of a draft is not a claim)
_SEND_PROMISE = re.compile(r"\bi(?:'ll| will|'m going to| am going to|'m about to)\s+(?:now\s+|just\s+)?"
                           r"(?:message|text|whatsapp)\b", re.IGNORECASE)
_SWITCHES = {"mute", "set_volume", "set_brightness", "lock_screen", *_POWER}
_TYPE_PROMISE = re.compile(r"\bi(?:'ll| will|'m going to| am going to|'m about to)\s+(?:now\s+|just\s+)?"
                           r"(?:type|write (?:it |that |this )?(?:down|in|out|up)|take (?:it |that |this )?down)\b",
                           re.IGNORECASE)
# "Press send when you're ready.": telling the user what to do. Said of himself ("I'll press send")
# it's a promise to send, which is never true.
_PRESS_SEND = re.compile(r"\b(?:press|hit|tap|click)(?:\s+on)?\s+(?:the\s+)?send(?:\s+button)?\b", re.IGNORECASE)
_OF_HIMSELF = re.compile(r"\bi(?:'ll| will|'m going to| am going to|'m about to|'d| would)?\s+"
                         r"(?:just\s+|now\s+|go ahead and\s+)?$", re.IGNORECASE)
# "Chrome is open" is a state, not something he did: bare "open" counts only as "now open".
_STATE_OPEN = re.compile(r"(?:\bis|\bare|'s|'re)\s+(?:already\s+|still\s+)?open\b", re.IGNORECASE)
_PERCENT = re.compile(r"%|\bper ?cent\b", re.IGNORECASE)  # with numbers_in(): "80%" and "eighty percent" alike
_DONE = re.compile(r"^\W*(?:(?:all|it'?s|that'?s)\s+)?(?:done|all set|finished|complete|completed|taken care of)\b",
                   re.IGNORECASE)
_NOT_A_CLAIM = re.compile(r"\b(?:not|never|no|cannot|unable|if|whether|want me to|should i|shall i|would you like)\b|"
                          r"n't\b|n.t\b", re.IGNORECASE)
# "still analyzing your files", "processing your files": work on the user's things that isn't happening.
_DOING = re.compile(r"\b(?:analy[sz]|process|monitor|watch|track|scann|organi[sz]|review|check|optimi[sz]|read|"
                    r"manag|handl|sort|clean|back)ing\s+(?:up\s+)?your\b", re.IGNORECASE)
# "You've got two hours and thirty-four minutes before it starts": time arithmetic nobody asked for,
# and the 4B model got it wrong live (5 h 34 min was right).
_COUNTDOWN = re.compile(r"\b(?:hours?|minutes?|mins?|days?)\s+(?:left|to go|to prepare|to spare|until|till|"
                        r"before|from now)\b"
                        r"|\byou(?:'ve|'ll| have| will)\s+(?:got\s+|have\s+)?(?:about\s+|roughly\s+|only\s+|just\s+)?"
                        r"[\w-]+\s+(?:hours?|minutes?)\b", re.IGNORECASE)
# "still monitoring", "I've been watching": surveillance he isn't doing, said of himself.
_SELF_WATCHING = re.compile(r"\b(?:still|been|currently|constantly|always|i'm|i am)\b[^.?!]{0,25}?"
                            r"\b(?:monitor|watch|track|observ|logg)\w*", re.IGNORECASE)
_ASKS_TIME = re.compile(r"\bhow (?:long|much time|many (?:hours|minutes|days))\b|\btime (?:left|until)\b",
                        re.IGNORECASE)
# "Suhinish's chat is open.", "Opened your chat with Appa.", "You're now in Appa's chat.": only
# whatsapp_open_chat can make these true (2026-10-22: switch_to_app was taken for it).
_CHAT_IS_OPEN = re.compile(
    r"\b(?:chat|conversation)s?\b[^.?!]{0,40}?(?:\bis|\bare|'s|'re)\s+(?:now\s+|already\s+|all\s+)?(?:open|up|ready)\b"
    r"|\b(?:opened|opening|pulled up|brought up)\b[^.?!]{0,40}?\b(?:chat|conversation)\b"
    r"|\byou(?:'re|\s+are)\s+(?:now\s+|already\s+)?in\b[^.?!]{0,40}?\b(?:chat|conversation)\b",
    re.IGNORECASE)
# A stock offer of help, the whole sentence (2026-10-22). Topic questions ("Want a reminder?",
# "Which one?") don't match.
_STOCK_OFFER = re.compile(
    r"^\W*(?:(?:so|and|also|okay|ok|well|now|alright|right)\W+)?(?:"
    r"how (?:else )?(?:can|may|could|might) i (?:help|assist)(?: you)?(?: (?:further|today|now|more|with anything else|"
    r"with anything|with that|any further))?"
    r"|(?:is there )?anything else(?: (?:i can (?:help|assist|do)|you(?:'d| would)? (?:like|want|need)|you need)[^.?!]*)?"
    r"|(?:just |please |do )?let me know (?:if|whether|when|whenever|what|how)\b[^.?!]*"
    r"|what else can i (?:do|help with|help you with|assist (?:you )?with)(?: for you| today)?"
    r"|(?:do you |can i help (?:you )?with |)need anything else"
    r"|(?:feel free to (?:ask|reach out)|if you need anything(?: else)?|i(?:'m| am) here (?:if|whenever|when) you need)[^.?!]*"
    r")\W*$", re.IGNORECASE)
# Talk of an earlier session ("just like last time"): true only when memory or the user says so.
_PAST = re.compile(r"\b(?:last time|(?:the |our |my |your )?(?:previous|last|other) (?:session|conversation|chat|time)|"
                   r"like before|as before|same as before|as (?:usual|always)|like (?:usual|always|yesterday)|"
                   r"same as (?:usual|always|yesterday)|like the other day)\b", re.IGNORECASE)
# What in a note (memory's "Possibly relevant from earlier:", a past summary) or the user's words
# makes talk of an earlier session true.
_PAST_TOLD = re.compile(r"\bearlier\b|\bprevious(?:ly)?\b|\bsession\b|\bbefore\b|\byesterday\b|"
                        r"\bthe other day\b|\busual(?:ly)?\b|\balways\b", re.IGNORECASE)
_PAST_MARK = "__past__"
# The user asking for an app, or about apps, this turn or the one before ("Open cloud." then
# "Claude."): the refusal check below applies only then.
_ASKS_FOR_APP = re.compile(r"\b(?:open|launch|apps?|applications?|programs?)\b", re.IGNORECASE)
# "I can't open settings directly.", "Claude is not a valid app I can access.", "I can only open
# Chrome.", "Only Chrome and Claude are currently accessible.", "No other apps are available."
_REFUSES_APP = re.compile(
    r"\b(?:can'?t|cannot|can not|unable to|not able to|no way to|don'?t have (?:access|the ability))\b[^.?!]{0,30}?"
    r"\b(?:open|launch|start|access|run)\b"
    r"|\bnot (?:a |an )?(?:valid |available |installed |accessible |known |supported )?(?:app|application|program)\b"
    r"|\b(?:only|just) (?:able to )?(?:open|access|launch)\b|\b(?:open|access|launch) only\b"
    r"|\bonly\b[^.?!]{0,60}?\b(?:is|are)\s+(?:currently\s+|now\s+)?(?:available|accessible|supported)\b"
    r"|\bno other (?:apps?|applications?|programs?)\b", re.IGNORECASE)
_APP_TOOLS = {"open_app", "switch_to_app", "close_app", "list_apps", "open_website", "open_file", "open_folder"}


def stem(word: str) -> str:
    """Rough enough to match "meetings" with "meeting", "watching" with "watch"."""
    word = word.lower().replace("-", "")
    for suffix in ("ing", "ed", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def claims(sentence: str) -> list[str]:
    """What the sentence asserts about the user or the laptop, as the words to check."""
    text = strip_marks(sentence).strip()
    if text.endswith("?"):
        return []  # a question claims nothing
    found = [m.group("thing") for m in _YOUR_THING.finditer(text)]
    if _READING.search(text):
        found += [m.group(1) for m in _STATUS.finditer(text)]
    if _YOU.search(text):
        found += [m.group(1) for m in _WATCHING.finditer(text)]
    found += [m.group(0).split()[-1] for m in _SELF_WATCHING.finditer(text)]
    if _TASKS.search(text):
        found.append("task")
    if _PAST.search(text):
        found.append(_PAST_MARK)  # grounded only by memory or the user (Delivery.ground)
    return found


def action_claims(sentence: str) -> list[set[str] | None]:
    """What the sentence says he did, as the tools that would make each claim true. An empty
    set means any tool will do ("Done."); None means no tool can ("I've ordered it")."""
    text = strip_marks(sentence).strip()
    if not text or text.endswith("?") or _NOT_A_CLAIM.search(text):
        return []
    found: list[set[str] | None] = []
    for m in reversed(list(_PRESS_SEND.finditer(text))):
        if _OF_HIMSELF.search(text[:m.start()]):
            found.append(None)  # "I'll press send": he can't, and won't
        text = f"{text[:m.start()]}press the button{text[m.end():]}"
    for patterns, tools in _CLAIMS:
        for pattern in patterns:
            m = pattern.search(text)
            if m and not (tools is _OPENS and _STATE_OPEN.search(m.group(0))):
                found.append(tools)
                break
    if _NOW_SWITCHED.search(text):
        found.append(_SWITCHES)
    if _HUMOR_CLAIM.search(text):
        found.append({"set_humor", "personality_setting"})  # 2026-11-18: said without either running
    if _MIC_CLAIM.search(text) and not _TELLS_HOW.search(text):
        found.append(None)  # his mic: only "turn off the mic" (core/pipeline.py) or the button switch it
    if _SEND_PROMISE.search(text):
        found.append(None)
    if _TYPE_PROMISE.search(text):
        found.append(_TYPED | {"start_dictation"})
    if _CHAT_IS_OPEN.search(text):
        found.append({"whatsapp_open_chat"})  # not switch_to_app or open_app (2026-10-22)
    if _DONE.search(text) or _DOING.search(text):
        found.append(set())
    return found


def claims_an_action(sentence: str) -> bool:
    """Does it say he did something ("Set a reminder for 3 PM.", "Message sent.")?"""
    return bool(action_claims(sentence))


_COMMON = {"the", "and", "you", "your", "that", "this", "with", "for", "are", "was", "but", "not", "it's",
           "its", "have", "has", "just", "what", "can", "will", "all", "out", "there", "they", "them"}


def words(text: str) -> set[str]:
    return set(_WORD.findall(strip_marks(text).lower())) - _COMMON


def similar(a: set[str], b: set[str]) -> bool:
    return bool(a) and bool(b) and len(a & b) / len(a | b) >= SIMILAR


class Delivery:
    def __init__(self, rng: random.Random | None = None, real_sighs: bool = False, react: bool = True):
        """`real_sighs`: the voice can sigh for real (Chatterbox), so [sigh] stays a sigh
        rather than becoming a spoken "Hmm."."""
        self.rng = rng or random.Random()
        self.real_sighs = real_sighs
        self.react = react  # add spoken reactions ("Hm.", "Right.") between sentences
        self.turn = 0
        self._recent: deque[set[str]] = deque(maxlen=REMEMBER_SENTENCES)
        self._humanity_turn = -HUMANITY_EVERY
        self._sigh_turn = -SIGH_EVERY
        self._last_user: set[str] = set()
        self._last_reaction = ""
        self._asked_for_app = False
        self.start_turn("")

    def start_turn(self, user_text: str, explain: bool = False) -> None:
        self.turn += 1
        heard = words(user_text)
        self.repeated_question = similar(heard, self._last_user) and len(heard) >= 2
        self._last_user = heard
        self._about_people = bool(_ABOUT_PEOPLE.search(user_text))
        self._explain = explain
        self.strict = True  # False while retelling a tool's facts: asked again, they're the point
        self._allow: set[str] = set()
        self._sentences = 0
        self._since_reaction = 0
        self._reactions = 0
        self._answered = False
        self._chunks = 0
        self._sentence_start = True  # reactions only go where a sentence begins
        self._grounded: set[str] = set()
        self._facts: set[str] = set()    # words from readings (tools, the live status, memory)
        self._numbers: set[str] = set()  # numbers he was given, by anyone
        self._plugged: bool | None = None  # what a reading said: plugged in (True) or on battery
        self._dropping = False  # the rest of a sentence whose start was invented
        self._partial = ""      # the sentence so far: "I cannot shut down," then "sleep, or sign out"
        self._line_said = False  # the user's signature line has been said this turn
        self.invented = 0       # sentences dropped this turn for stating what he wasn't told
        self.false_actions = 0  # of those, how many claimed something was done
        self.false_refusals = 0  # of those, how many said an app can't be opened, with none tried
        self.done: set[str] = set()  # tools that succeeded this turn ("*": treat every claim as true)
        self.ran: set[str] = set()   # tools that ran this turn, whatever came of them
        asks = bool(_ASKS_FOR_APP.search(user_text))
        self._asks_for_app, self._asked_for_app = asks or self._asked_for_app, asks
        self._asked_time = bool(_ASKS_TIME.search(user_text))
        self._about_laptop = bool(_STATUS.search(user_text))  # "how's my battery?": every percentage is a reading
        self.ground(user_text)

    @property
    def acted(self) -> bool:
        """A tool succeeded this turn."""
        return bool(self.done)

    @acted.setter
    def acted(self, value: bool) -> None:
        self.done = {"*"} if value else set()

    def did(self, tool: str) -> None:
        """A tool succeeded: what it does may now be said to be done."""
        self.done.add(tool)

    def tried(self, tool: str) -> None:
        """A tool ran, whether or not it worked: after an app tool, "I can't open X" may be true."""
        self.ran.add(tool)

    def ground(self, text: str, fact: bool = False) -> None:
        """Something he was told this turn: claims about it are fine. `fact`: a reading (a tool's
        result, the live status, memory), the only thing that can vouch for the laptop's state."""
        stems = {stem(w) for w in _ANY_WORD.findall(text.lower())}
        if _PAST.search(text) or (fact and _PAST_TOLD.search(text)):
            stems.add(_PAST_MARK)  # the user or a note talks of an earlier time: "like last time" may be true
        self._grounded |= stems
        self._numbers |= numbers_in(text)
        if fact:
            self._facts |= stems
            state = plug_state(text)
            if state is not None:
                self._plugged = state

    def _invented(self, chunk: str, sentence: str | None = None) -> bool:
        if self._dropping:
            self._dropping = not _SENTENCE_DONE.search(chunk)
            return True
        text = strip_marks(chunk)
        sentence = sentence or text
        made_up = any(stem(c) not in self._grounded for c in claims(chunk))
        countdown = not (self._asked_time or self.acted) and bool(_COUNTDOWN.search(text))
        undone = self._undone(sentence)
        refused = self._untried_refusal(sentence)
        if made_up or countdown or undone or refused or self._unbacked_reading(sentence):
            self.invented += 1
            self.false_actions += undone
            self.false_refusals += refused
            self._dropping = not _SENTENCE_DONE.search(chunk)
            return True
        return False

    def _undone(self, chunk: str) -> bool:
        """Does it claim something was done that no tool did this turn?"""
        if "*" in self.done:
            return False
        for tools in action_claims(chunk):
            if tools is None or (tools and not tools & self.done) or (not tools and not self.done):
                return True
        return False

    def _untried_refusal(self, sentence: str) -> bool:
        """Asked for an app, does it say an app can't be opened, with no app tool run this turn?"""
        return (self._asks_for_app and not (self.ran & _APP_TOOLS) and "*" not in self.done
                and bool(_REFUSES_APP.search(strip_marks(sentence))))

    def _unbacked_reading(self, text: str) -> bool:
        """A reading of this laptop with a number nobody gave him, or plugged in / on battery when
        no reading says so, or says the opposite. Explanations (general knowledge) are left alone."""
        if self._explain or text.rstrip().endswith("?"):
            return False
        if numbers_in(text) - self._numbers and (
                (_STATUS.search(text) and _THIS_LAPTOP.search(text)) or (self._about_laptop and _PERCENT.search(text))):
            return True
        said = plug_state(text)
        return said is not None and said != self._plugged

    def allow(self, line: str) -> None:
        """The signature line offered this turn is never taken for a repeat."""
        self._allow = words(line)

    def opener(self) -> str:
        """A sigh before answering the same question twice in a row."""
        return "[sigh] " if self.repeated_question else ""

    def shape(self, chunk: str) -> str | None:
        """The chunk as it should be spoken (with any reaction or sigh), or None to drop it."""
        if self._chunks == 0:
            chunk = _JOKE_LABEL.sub("", chunk, count=1)
            if not chunk.strip():
                return None
        said = words(chunk)
        ends = bool(_SENTENCE_DONE.search(chunk))
        sentence = f"{self._partial} {strip_marks(chunk)}".strip()  # judged whole: a "not" earlier counts
        self._partial = "" if ends else sentence
        if not self._keep(chunk, said, sentence):
            return None
        chunk = self._sigh(chunk) if "[sigh]" in chunk else self._react(chunk)
        if said:
            self._recent.append(said)
        self._chunks += 1
        self._sentence_start = bool(_SENTENCE_DONE.search(chunk))
        if self._sentence_start:
            self._answered = True
            self._sentences += 1
            self._since_reaction += 1
        return chunk

    def _keep(self, chunk: str, said: set[str], sentence: str) -> bool:
        if similar(said, self._allow) or (said and said <= self._allow):
            self._line_said = True
            return True  # the user's own signature line offered this turn, or a piece of it
        if self._answered and _STOCK_OFFER.match(sentence):
            return False  # "How can I assist you further?" after the answer (2026-10-22)
        if self._line_said and _TAKES_IT_BACK.search(sentence):
            return False  # live: "I'm a peacekeeping program... Well, that's not true."
        if self._invented(chunk, sentence):
            return False
        if not self._answered or len(said) < 3:
            if HUMANITY.search(chunk):
                self._humanity_turn = self.turn
            return True
        if self.strict and any(similar(said, before) for before in self._recent):
            return False
        if HUMANITY.search(chunk) and not self._about_people:
            if self.turn - self._humanity_turn < HUMANITY_EVERY:
                return False
            self._humanity_turn = self.turn
        return True

    def _sigh(self, chunk: str) -> str:
        rest = _SIGH_MARK.sub("", chunk).strip()
        if self.turn - self._sigh_turn < SIGH_EVERY or _REACTION.match(rest):
            return rest  # sighed too recently, or he's already reacting in words
        self._sigh_turn = self.turn
        self._reactions += 1
        return f"[sigh] {rest}".strip() if self.real_sighs else f"{self._pick(SIGHS)} {rest}".strip()

    def _react(self, chunk: str) -> str:
        if _REACTION.match(chunk):
            self._since_reaction = 0  # he reacted by himself: that counts
            return chunk
        if not self.react or self._reactions >= MAX_REACTIONS or not self._sentence_start:
            return chunk  # never mid-sentence ("...the atmosphere Hm. and...")
        if self._chunks == 0:
            wanted = self._explain and self.rng.random() < 0.5
            options = OPENERS
        else:
            wanted = (len(chunk.split()) >= 10 and self._since_reaction >= 2 and self.rng.random() < 0.35)
            options = BETWEEN
        if not wanted:
            return chunk
        self._since_reaction = 0
        self._reactions += 1
        return f"{self._pick(options)} {chunk}"

    def _pick(self, options: list[str]) -> str:
        word = self.rng.choice([o for o in options if o != self._last_reaction] or options)  # one left: say it again
        self._last_reaction = word
        return word

