"""A stand-in for aj/voice_server.py in tests: same pipe protocol, no model. Each line
comes back as 100 samples of 0.5 per character, then a little silence."""
import json
import os
import sys

channel = os.fdopen(os.dup(1), "wb")
os.dup2(2, 1)
config = json.loads(sys.argv[1])
if config.get("hf_home") == "FAIL":
    channel.write(b'{"error": "no graphics card"}\n')
    channel.flush()
    sys.exit(1)
channel.write(b'{"ready": true, "rate": 24000}\n')
channel.flush()
for line in sys.stdin:
    text = json.loads(line)["text"]
    data = (b"\x00\x00\x00\x3f" * (100 * len(text))) + b"\x00\x00\x00\x00" * 2400
    channel.write((json.dumps({"samples": len(data) // 4, "text": text}) + "\n").encode() + data)
    channel.flush()
