"""Extract the quality-attribute vocabulary and relationship graph, mechanically.

Mechanical, not model-read: the vocabulary must be exactly what the reference
says, and a graph paraphrased by an LLM is a graph that can drift from its
source without anyone noticing.
"""
import hashlib
import pathlib
import re

WI = pathlib.Path("c:/Users/maschdev3/Documents/workflow-embedded-systems/workflow-iot")
REF = WI / "Embedded IoT Engineering Quality Attributes Cross-Platform Reference.md"
S5 = WI / "WORKFLOW_SECTION5.md"
OUT = pathlib.Path("c:/Users/maschdev3/Documents/workflow-embedded-systems/"
                   "esp32-embedded-systems-workflow-agents/quality-attributes.yaml")

VERBS = ["DEPENDED ON BY", "CONFLICTS WITH", "DEPENDS ON", "REINFORCES", "ENABLES"]

text = REF.read_text(encoding="utf-8")
lines = text.split("\n")

heads = [(i, m.group(1), m.group(2).strip())
         for i, l in enumerate(lines)
         for m in [re.match(r"^## (\d+)\.\s+(.+)$", l)] if m]
names = [n for _, _, n in heads]
assert len(heads) == 12, len(heads)

# --- SECTION5 measurable criteria, per attribute prefix -----------------------
# SECTION5 organises by RSMR only; the prefixes are R- S- M- RL-.
PREFIX = {"Robust": "R", "Scalable": "S", "Maintainable": "M", "Reliable": "RL"}
s5 = S5.read_text(encoding="utf-8")
crit = {}
for m in re.finditer(r"\*\*([A-Z]+)-(S32|ESP|RPI)-(\d+)\*\*", s5):
    crit.setdefault(m.group(1), set()).add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")

def esc(s):
    s = " ".join(str(s).split())
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

out = []
out.append("# Quality attribute vocabulary and relationship graph.")
out.append("#")
out.append("# EXTRACTED MECHANICALLY from the reference below - not paraphrased.")
out.append("# A graph an LLM restated is a graph that drifts from its source with")
out.append("# nobody noticing. Regenerate with tools/extract_attrs.py rather than")
out.append("# editing by hand; `source_sha256` makes a stale copy detectable.")
out.append("#")
out.append("# WHAT THIS IS FOR")
out.append("# SECTION1's stage bar is expressed in RSMR - four of the twelve")
out.append("# attributes here. SECTION5 supplies measurable criteria for those four")
out.append("# and for no others. SECTION2's requirements table binds to neither.")
out.append("# This file is the vocabulary that lets a requirement name what quality")
out.append("# it is buying, and the conflict graph that shows what it costs.")
out.append("")
out.append(f"source: workflow-iot/{REF.name}")
out.append(f"source_sha256: {sha(REF)}")
out.append(f"criteria_source: workflow-iot/{S5.name}")
out.append(f"criteria_source_sha256: {sha(S5)}")
out.append("")
out.append("# The four SECTION1 stage-bar attributes. The other eight have no")
out.append("# measurable criteria anywhere, which is itself a finding: a requirement")
out.append("# claiming one of them is UNVERIFIABLE by construction.")
out.append("rsmr: [Robust, Scalable, Maintainable, Reliable]")
out.append("")
out.append("attributes:")

for k, (i, num, name) in enumerate(heads):
    end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
    body = lines[i:end]

    defi = ""
    for j, l in enumerate(body):
        if l.startswith("### Definition"):
            for q in body[j + 1:j + 6]:
                if q.strip():
                    defi = q.strip()
                    break
            break

    xi = next((j for j, l in enumerate(body)
               if l.startswith("### Cross-Attribute Relationships")), None)
    rel = {v: [] for v in VERBS}
    if xi is not None:
        for l in body[xi:]:
            m = re.match(r"^- \*\*(.+?):\*\*\s*(.+)$", l)
            if not m:
                continue
            head, why = m.group(1), m.group(2)
            verb = next((v for v in VERBS if head.upper().startswith(v)), None)
            if not verb:
                continue
            tail = head[len(verb):].strip()
            tail = re.sub(r"\([^)]*\)", "", tail)          # drop qualifiers
            targets = [t.strip() for t in tail.split(",") if t.strip() in names]
            for t in targets:
                rel[verb].append((t, why))

    has_crit = name in PREFIX and PREFIX[name] in crit
    out.append(f"  - name: {name}")
    out.append(f"    number: {num}")
    out.append(f"    definition: {esc(defi)}")
    out.append(f"    in_rsmr: {'true' if name in PREFIX else 'false'}")
    if has_crit:
        ids = sorted(crit[PREFIX[name]])
        esp = [c for c in ids if "-ESP-" in c]
        out.append(f"    criteria_prefix: {PREFIX[name]}")
        out.append(f"    criteria_esp32: [{', '.join(esp)}]")
    else:
        out.append("    criteria_prefix: null   # no measurable criteria exist")
        out.append("    criteria_esp32: []")
    for verb in VERBS:
        if rel[verb]:
            key = verb.lower().replace(" ", "_")
            out.append(f"    {key}:")
            for t, why in rel[verb]:
                out.append(f"      - target: {t}")
                out.append(f"        why: {esc(why)}")
    out.append("")

OUT.write_text("\n".join(out) + "\n", encoding="utf-8")

import yaml
d = yaml.safe_load(OUT.read_text(encoding="utf-8"))
n_conf = sum(len(a.get("conflicts_with") or []) for a in d["attributes"])
n_crit = sum(len(a.get("criteria_esp32") or []) for a in d["attributes"])
no_crit = [a["name"] for a in d["attributes"] if not a.get("criteria_esp32")]
print(f"wrote {OUT.name}")
print(f"  attributes      : {len(d['attributes'])}")
print(f"  conflict edges  : {n_conf}")
print(f"  ESP32 criteria  : {n_crit}")
print(f"  no criteria at all ({len(no_crit)}): {', '.join(no_crit)}")
