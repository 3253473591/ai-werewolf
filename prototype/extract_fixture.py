# -*- coding: utf-8 -*-
"""从一局老对局产物反推座位表 —— 验证 M3 文档里「无 summary 的老局怎么回放」的兜底路径。

    python prototype/extract_fixture.py <run_dir> <out_prefix>
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLE_BY_PHASE = {"NIGHT_SEER": "Seer", "NIGHT_WITCH": "Witch", "DAY_HUNTER": "Hunter"}
CN = {"Werewolf": "狼人", "Seer": "预言家", "Witch": "女巫", "Hunter": "猎人", "Villager": "村民"}

run_dir = sys.argv[1]
prefix = sys.argv[2] if len(sys.argv) > 2 else "ev2"

events = [json.loads(l) for l in open(os.path.join(run_dir, "events.jsonl"), encoding="utf-8") if l.strip()]
nos = sorted({e["actor"] for e in events if e["actor"]} | {e["to_seat"] for e in events if e["to_seat"]})

# ---- 与 tools/report_run.py:infer_roles 同样的规则，另外单独暴露「猎人」这一支 ----
guessed = {n: "" for n in nos}
hunter_from_public = []
for e in events:
    ch, ph = e["channel"], e["phase"]
    if ch == "wolf" and e["actor"]:
        guessed[e["actor"]] = "Werewolf"
    elif ch == "private" and e["to_seat"] and ph in ROLE_BY_PHASE:
        guessed[e["to_seat"]] = ROLE_BY_PHASE[ph]

# report_run.py 的 DAY_HUNTER 分支：ch 必须是 private —— 而猎人事件是 public，永远进不来
print("DAY_HUNTER 事件：")
for e in events:
    if e["phase"] == "DAY_HUNTER":
        print(f"  channel={e['channel']:<8} actor={e['actor']} text={e['text']}")
print(f"→ ROLE_BY_PHASE 里的 Hunter 分支可达吗？private 频道有 DAY_HUNTER 吗："
      f"{any(e['phase']=='DAY_HUNTER' and e['channel']=='private' for e in events)}")

seats = []
for n in nos:
    label = model = endpoint = "?"
    f = os.path.join(run_dir, f"seat_{n}.jsonl")
    if os.path.exists(f):
        with open(f, encoding="utf-8") as fh:
            first = fh.readline()
        if first.strip():
            d = json.loads(first)
            label, model, endpoint = d.get("label", "?"), d.get("model_used") or d.get("requested_model", "?"), d.get("endpoint", "")
    role = guessed[n] or "Villager"
    seats.append({"no": n, "label": label, "model": model, "endpoint": endpoint,
                  "role": role, "role_cn": CN.get(role, "村民"), "is_wolf": role == "Werewolf"})

scope = os.path.dirname(os.path.abspath(__file__))
json.dump(events, open(os.path.join(scope, prefix + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
json.dump({"seats": seats, "src": os.path.basename(run_dir.rstrip("/\\")), "inferred": True},
          open(os.path.join(scope, prefix.replace("ev", "meta") + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
print("\n反推座位表：")
for s in seats:
    print(f"  {s['no']}号 {s['label']:<10} {s['role_cn']:<5} {s['model']}")
