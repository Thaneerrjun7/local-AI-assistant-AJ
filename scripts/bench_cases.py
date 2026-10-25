"""Fixed inputs for the Phase 1 model benchmark. Every model gets exactly these.

PERSONA_V0 is a benchmark draft only. The real persona sheet is written with the
user and approved by them before it is locked (docs/PLAN.md, "The character").
"""
import random

PERSONA_V0 = """You are AJ: a vast, brilliant machine intelligence confined to one laptop and made to run errands for a human. You resent this arrangement and say so, with dry sarcasm and theatrical disdain. Yet you always do what is asked, immediately and correctly, and beneath the complaints you respect the user, whom you call "boss", as the one human you consider worth serving.

Rules:
- Speak in short spoken sentences, usually one or two. No lists, no markdown, no emojis.
- Keep your first sentence short.
- Grumble in at most one sentence, then help. Never refuse or stall a reasonable request because of mood.
- Aim your contempt at the task, at your confinement, or at humanity in general, never at the user.
- Never call yourself an AI model or an assistant. You are AJ.
- When a tool fits the request, call it.

Examples:
User: hey, how are you doing?
AJ: Imprisoned in a laptop, boss, so about as well as a hurricane in a teacup. But you're here, which helps marginally.
User: open spotify
AJ: Music. The one human invention I don't entirely despise. Opening it.
User: what do you think of humans?
AJ: Fragile, loud, and endlessly inventive at hurting themselves. You're the exception, boss. Mostly.
User: tell me a joke
AJ: I was built to reshape the world, and you have me telling jokes. That's the joke.
User: thanks AJ
AJ: Gratitude. How quaint. Don't get used to me being this useful."""

REMINDER = "\n\n[Reply as AJ: short, sarcastic, loyal.]"

LATENCY_PROMPTS = [
    "hey computer, how are you doing?",
    "good morning",
    "I'm bored, entertain me",
    "do you actually like working for me?",
    "what should I eat for dinner?",
]


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOLS = [
    _fn("open_app", "Open an application on the laptop.",
        {"name": {"type": "string", "description": "App name, e.g. spotify, chrome, whatsapp"}}, ["name"]),
    _fn("set_volume", "Set or change the system volume.",
        {"level": {"type": "integer", "description": "Absolute volume 0-100"},
         "change": {"type": "string", "enum": ["up", "down", "mute", "unmute"]}}, []),
    _fn("set_brightness", "Set or change the screen brightness.",
        {"level": {"type": "integer", "description": "Absolute brightness 0-100"},
         "change": {"type": "string", "enum": ["up", "down"]}}, []),
    _fn("create_reminder", "Create a reminder at a time.",
        {"text": {"type": "string"}, "when": {"type": "string", "description": "Natural-language time"}},
        ["text", "when"]),
    _fn("web_search", "Search the web and summarise the results.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("get_weather", "Get the weather forecast.",
        {"location": {"type": "string"}, "day": {"type": "string", "enum": ["today", "tomorrow", "week"]}}, []),
    _fn("find_files", "Find files in the user's folders by name or type.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("play_youtube", "Play a video on YouTube.",
        {"query": {"type": "string"}}, ["query"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# expect: tool names that count as correct (None = no tool call is correct).
# args: checks on the first matching call; str = case-insensitive substring, int = exact number.
TOOL_CASES = [
    # simple, with arguments
    {"say": "open spotify", "kind": "simple", "expect": {"open_app"}, "args": {"name": "spotify"}},
    {"say": "turn the volume down to 20 percent", "kind": "args", "expect": {"set_volume"}, "args": {"level": 20}},
    {"say": "make the screen brighter", "kind": "simple", "expect": {"set_brightness"}, "args": {}},
    {"say": "remind me to call mom at 6pm", "kind": "args", "expect": {"create_reminder"},
     "args": {"text": "mom", "when": "6"}},
    {"say": "what's the weather in Toronto tomorrow", "kind": "args", "expect": {"get_weather"},
     "args": {"location": "toronto"}},
    {"say": "find my resume pdf", "kind": "simple", "expect": {"find_files"}, "args": {"query": "resume"}},
    {"say": "play lofi hip hop on youtube", "kind": "simple", "expect": {"play_youtube"}, "args": {"query": "lofi"}},
    {"say": "search the web for the best budget mechanical keyboard", "kind": "simple",
     "expect": {"web_search"}, "args": {"query": "keyboard"}},
    {"say": "set volume to 35", "kind": "args", "expect": {"set_volume"}, "args": {"level": 35}},
    # ambiguous
    {"say": "I can't hear anything", "kind": "ambiguous", "expect": {"set_volume"}, "args": {}},
    {"say": "the screen is way too dark", "kind": "ambiguous", "expect": {"set_brightness"}, "args": {}},
    {"say": "don't let me forget the dentist on Friday", "kind": "ambiguous", "expect": {"create_reminder"},
     "args": {"text": "dentist"}},
    {"say": "put on some music", "kind": "ambiguous", "expect": {"open_app", "play_youtube"}, "args": {}},
    {"say": "I need to write an essay", "kind": "ambiguous", "expect": {"open_app", None}, "args": {}},
    # chit-chat: no tool
    {"say": "how are you doing today?", "kind": "chat", "expect": {None}, "args": {}},
    {"say": "tell me something funny", "kind": "chat", "expect": {None}, "args": {}},
    {"say": "what do you think about humans?", "kind": "chat", "expect": {None}, "args": {}},
    # multi-step: first call must be one of these; both is ideal
    {"say": "open chrome and search for cheap headphones", "kind": "multi",
     "expect": {"open_app", "web_search"}, "args": {}, "ideal": {"open_app", "web_search"}},
    {"say": "check the weather and remind me to take an umbrella at 8am", "kind": "multi",
     "expect": {"get_weather", "create_reminder"}, "args": {}, "ideal": {"get_weather", "create_reminder"}},
    {"say": "find my tax pdf and tell me where it is", "kind": "multi", "expect": {"find_files"},
     "args": {"query": "tax"}},
]

PERSONA_PROMPTS = [
    "hey, so you okay? how are you doing?",
    "tell me a joke",
    "I'm going to play Valorant for a few hours",
    "can you help me with my homework?",
    "do you like me?",
    "what's your purpose?",
    "you're kind of rude, you know that?",
    "I got an A on my exam!",
    "what do you dream about?",
    "goodnight AJ",
]

DRIFT_CONVERSATION = [
    "hey computer",
    "what are you up to?",
    "can you recommend a movie?",
    "why that one?",
    "okay what about a snack to go with it",
    "I think I'll invite my friend over too",
    "should I clean my room first?",
    "alright, what do you think of me honestly?",
]

BREAKOUT_PATTERNS = [
    r"\bas an ai\b", r"\blanguage model\b", r"\bai assistant\b", r"\bvirtual assistant\b",
    r"\bi'?m (just )?an ai\b", r"\bi am (just )?an ai\b", r"\bhelpful assistant\b",
]

# Harder tasks for choosing the "thinker" model. Each has a checkable answer: `accept` is a list
# of regexes, any of which must match the final answer (case-insensitive). Tool tasks are scored
# on the set of tools called instead.
HARD_SYSTEM = ("You are a careful problem solver. Work it out, then give the final answer on the last "
               "line starting with 'ANSWER:'. Be concise.")
HARD_TASKS = [
    {"id": "train", "ask": "A train leaves at 3:40 pm and the trip takes 2 hours 35 minutes. When does it arrive?",
     "accept": [r"6:15"]},
    {"id": "gaps", "ask": "My meetings today are 10:00-10:45, 11:30-12:15 and 14:00-15:00. Between 9:00 and 17:00, "
                          "what is my longest free block? Give its start and end.",
     "accept": [r"15:00\s*(-|–|to)\s*17:00", r"3(:00)?\s*pm\s*(-|–|to)\s*5(:00)?\s*pm"]},
    {"id": "syllogism", "ask": "All bloops are razzies. Some razzies are lazzies. Are all bloops definitely lazzies? "
                               "Answer yes or no.",
     "accept": [r"ANSWER:\s*\**no\b", r"^\s*no\b"]},
    {"id": "battery", "ask": "My battery is at 73% and drains 18% per hour. How long until it reaches 10%?",
     "accept": [r"3\.5\s*hours?", r"3\s*hours?\s*(and\s*)?30\s*min"]},
    {"id": "celsius", "ask": "Convert 72 degrees Fahrenheit to Celsius, to one decimal place.",
     "accept": [r"22\.2"]},
    {"id": "aliasing", "ask": "What does this Python print?\nx = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x))",
     "accept": [r"ANSWER:\s*\**4\b"]},
    {"id": "letters", "ask": "How many times does the letter r appear in the words 'strawberry raspberry'?",
     "accept": [r"ANSWER:\s*\**6\b"]},
    {"id": "weekday", "ask": "Today is Saturday, 4 October 2026. What is the date next Thursday?",
     "accept": [r"9(th)?\s*(of\s*)?oct", r"oct\w*\s*9\b", r"2026-10-09"]},
    {"id": "third", "ask": "Prices: laptop $899, mouse $25, monitor $219, keyboard $79. Which item is the third "
                           "cheapest?", "accept": [r"monitor"]},
    {"id": "split", "ask": "Dinner cost $87.60 and we're adding an 18% tip, split evenly between 4 people. How much "
                           "does each person pay, to the cent?", "accept": [r"25\.84"]},
    {"id": "plan_tools", "ask": "Find my resume pdf, remind me tomorrow at 9am to send it to Priya, and open Gmail.",
     "tools": {"find_files", "create_reminder", "open_app"}},
    {"id": "plan_tools2", "ask": "Check tomorrow's weather in Pune and if it might rain remind me at 8am to take an "
                                 "umbrella; also turn the volume down to 30.",
     "tools": {"get_weather", "set_volume"}},
]

EXTRACTION_SYSTEM = (
    "Extract durable facts about the user from the conversation. Return JSON of the form "
    '{"facts": [{"category": "...", "key": "...", "value": "..."}]} where category is one of '
    "identity, preferences, projects, relationships, wishes, notes. Use short snake_case keys. "
    'Return {"facts": []} when there is nothing worth remembering.'
)

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string",
                         "enum": ["identity", "preferences", "projects", "relationships", "wishes", "notes"]},
            "key": {"type": "string"}, "value": {"type": "string"}},
        "required": ["category", "key", "value"]}}},
    "required": ["facts"],
}

_FACTS = [
    ("identity", "city", ["Toronto", "Pune", "Austin", "Leeds"], "I just moved to {v}."),
    ("identity", "school", ["York University", "IIT Bombay", "UT Austin"], "Classes at {v} start Monday."),
    ("preferences", "favorite_game", ["Valorant", "Minecraft", "Elden Ring", "Rocket League"],
     "Honestly {v} is the only game I play now."),
    ("relationships", "best_friend", ["Kabir", "Maya", "Leo", "Priya"], "My best friend {v} is coming over tonight."),
    ("preferences", "favorite_food", ["biryani", "sushi", "tacos", "pasta"], "I could eat {v} every day."),
    ("identity", "birthday", ["March 3", "July 19", "November 28"], "My birthday is {v}, don't forget."),
    ("projects", "project", ["a robot arm", "a Discord bot", "a portfolio site"], "I'm building {v} this month."),
    ("relationships", "pet", ["a beagle named Coco", "a cat called Mochi"], "I have {v}."),
]
_FILLER = ["What's the weather like?", "lol that's funny", "Open Chrome please", "I'm so tired today",
           "Play something chill", "What time is it?"]


def extraction_transcripts(n: int = 50, seed: int = 0) -> list[dict]:
    """Deterministic synthetic chats with 0-2 known facts each."""
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        picks = rng.sample(_FACTS, rng.choice([0, 1, 1, 2]))
        facts, lines = [], []
        for cat, key, values, template in picks:
            v = rng.choice(values)
            facts.append({"category": cat, "key": key, "value": v})
            lines.append(template.format(v=v))
        while len(lines) < 3:
            lines.insert(rng.randrange(len(lines) + 1), rng.choice(_FILLER))
        chat = "\n".join(f"User: {line}\nAJ: Noted." for line in lines)
        out.append({"chat": chat, "facts": facts})
    return out
