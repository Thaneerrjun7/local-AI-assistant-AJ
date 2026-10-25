"""Compare benchmark results and apply the council's overturn rule.

Writes data/bench/report.md, plus a blind persona sheet (persona_blind.md) whose
labels are shuffled; the key is in persona_key.json. Put your 1-10 scores per
label in persona_ratings.json and re-run to include them in the verdict.

Overturn rule (docs/PLAN.md, Phase 1): a challenger replaces the control only if
its tool score is within 5 points, its persona rating is at least 1 point higher,
and its cached p95 first sentence is 500 ms or less.
"""
import json
import random
import sys
from pathlib import Path

# Optional argument: another results folder, e.g. a persona-only round (data/bench/persona-v1).
BENCH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data" / "bench"
CONTROL = "qwen3-4b"


def load_results() -> dict[str, dict]:
    results = {}
    for path in sorted(BENCH.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict) and "model" in data:
            results[data["name"]] = data
    return results


def blind_sheet(results: dict[str, dict], contenders: list[str]) -> None:
    """Only models still in contention go on the sheet: rating a model that already
    failed on tools or speed would waste the listener's time."""
    named = [n for n in contenders if "persona" in results[n]]
    if len(named) < 2:
        return
    key_path = BENCH / "persona_key.json"
    ratings = BENCH / "persona_ratings.json"
    if key_path.exists() and set(json.loads(key_path.read_text(encoding="utf-8-sig"))["labels"].values()) == set(named):
        labels = json.loads(key_path.read_text(encoding="utf-8-sig"))["labels"]
    else:
        shuffled = random.sample(named, len(named))
        labels = {chr(ord("A") + i): name for i, name in enumerate(shuffled)}
        key_path.write_text(json.dumps({"labels": labels}, indent=2), encoding="utf-8")
        ratings.unlink(missing_ok=True)  # old ratings belong to an old line-up
    lines = ["# Blind persona sheet", "",
             "Score each label 1-10 for how in character it sounds (wit, attitude, staying in character, "
             "still loyal), then write the scores into persona_ratings.json.", ""]
    first = results[named[0]]["persona"]
    for i, item in enumerate(first["replies"] + first["drift"]):
        section = "single prompts" if i < len(first["replies"]) else "8-turn conversation"
        lines.append(f"## {i + 1}. ({section}) {item['prompt']}")
        for label, name in sorted(labels.items()):
            persona = results[name]["persona"]
            reply = (persona["replies"] + persona["drift"])[i]["reply"]
            lines.append(f"- **{label}:** {reply}")
        lines.append("")
    (BENCH / "persona_blind.md").write_text("\n".join(lines), encoding="utf-8")
    if not ratings.exists():
        ratings.write_text(json.dumps({label: None for label in labels}, indent=2), encoding="utf-8")


def persona_scores() -> dict[str, float]:
    key_path, ratings_path = BENCH / "persona_key.json", BENCH / "persona_ratings.json"
    if not (key_path.exists() and ratings_path.exists()):
        return {}
    labels = json.loads(key_path.read_text(encoding="utf-8-sig"))["labels"]
    ratings = json.loads(ratings_path.read_text(encoding="utf-8-sig"))
    return {labels[k]: v for k, v in ratings.items() if v is not None and k in labels}


def g(d: dict, *path, default="-"):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d


def main() -> None:
    results = load_results()
    if not results:
        sys.exit(f"no results in {BENCH}")
    control = results.get(CONTROL)
    screened = {}  # challenger -> (tools within 5 pts, cached p95 first sentence, fast enough)
    for name, r in results.items():
        if control is None or name == CONTROL or name.endswith("-cpu"):
            continue
        tools_ok = g(r, "tools", "score_pct", default=0) >= g(control, "tools", "score_pct", default=0) - 5
        p95 = g(r, "latency", "cached", "first_sentence", "p95_ms", default=10**6)
        screened[name] = (tools_ok, p95, p95 <= 500)
    contenders = ([CONTROL] if control else []) + [n for n, (t, _, f) in screened.items() if t and f]
    if not any("tools" in r for r in results.values()):  # persona-only round: everyone is rated
        contenders = list(results)
    blind_sheet(results, contenders)
    scores = persona_scores()

    head = ("| model | load s | VRAM MB | RAM MB | TTFT cached med/p95 | 1st sentence cached med/p95 | "
            "1st sentence cold p95 | tok/s | tools % | invented | multi % | breakouts | words | "
            "JSON grammar valid/recall | JSON free valid/recall | persona |")
    rows = [head, "|" + "|".join("---" for _ in range(head.count("|") - 1)) + "|"]
    for name, r in results.items():
        cached, cold = g(r, "latency", "cached", default={}), g(r, "latency", "cold", default={})
        rows.append("| " + " | ".join(str(x) for x in [
            name, r["load_seconds"], r.get("vram_model_mb", "-"), g(r, "memory_end", "working_set_mb"),
            f"{g(cached, 'ttft', 'median_ms')}/{g(cached, 'ttft', 'p95_ms')}",
            f"{g(cached, 'first_sentence', 'median_ms')}/{g(cached, 'first_sentence', 'p95_ms')}",
            g(cold, "first_sentence", "p95_ms"), g(cached, "decode_tps"),
            g(r, "tools", "score_pct"), g(r, "tools", "invented_calls"), g(r, "tools", "multi_all_steps_pct"),
            g(r, "persona", "breakouts"), g(r, "persona", "avg_words"),
            f"{g(r, 'json', 'grammar', 'valid_pct')}/{g(r, 'json', 'grammar', 'recall_pct')}",
            f"{g(r, 'json', 'free', 'valid_pct')}/{g(r, 'json', 'free', 'recall_pct')}",
            scores.get(name, "unrated"),
        ]) + " |")

    verdict = []
    if control:
        winner = CONTROL
        for name, (tools_ok, p95, fast_ok) in screened.items():
            persona_ok = (name in scores and CONTROL in scores and scores[name] >= scores[CONTROL] + 1)
            verdict.append(f"- **{name}** vs {CONTROL}: tools within 5 pts={tools_ok}, "
                           f"persona +1={persona_ok if scores else 'needs ratings'}, p95 first sentence "
                           f"{p95} ms <= 500={fast_ok}")
            if tools_ok and fast_ok and persona_ok:
                winner = name
        verdict.append(f"\n**Pick: {winner}**" + ("" if scores else " (provisional until persona ratings are in)"))

    report = "\n".join(["# Phase 1 benchmark", "", *rows, "", "## Overturn rule", *verdict, ""])
    (BENCH / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
