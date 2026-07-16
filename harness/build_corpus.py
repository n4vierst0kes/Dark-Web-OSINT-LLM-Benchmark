#!/usr/bin/env python3
"""Build the ground-truth corpus from the Di Tizio APT bundle.

The rule here: every label has to actually appear in the excerpt we show the
model, so someone could re-check it with Ctrl-F. (The earlier version pulled
cves/techniques from the knowledge graph while only showing the model a short
actor blurb - those never appeared in the text, so recall was stuck at zero.)

So we read the real report PDFs and take:
  apt        - actor names as written (canonical or alias, whatever's there)
  cves       - CVE ids, regex
  techniques - MITRE technique names from the master list
  goals      - the actor's KG goal, but only if a matching word shows up here
  country    - the actor's KG country, but only if its name/demonym shows up
               (keeps victim/target countries in the prose from sneaking in)

goals and country come from the KG (that's the authority on origin) and are
then gated on actually being mentioned. A record is kept only if the excerpt
has at least one actor and one CVE; the other fields fill in when present.
"""
import csv, json, re, sys, glob, os
from pathlib import Path
from collections import defaultdict

BUNDLE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "_extract/giorgioditizio-APTs-database-b8d3e36")
RAW = BUNDLE / "neo4j_db/raw_data"
REPORTS = BUNDLE / "report_sources"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("harness/corpus.jsonl")

EXCERPT_CHARS = 900           # short enough that a CPU model stays under 180s
MAX_RECORDS = 40              # 4 models x k=3 over this is an evening, not a week

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)

# country -> names + demonyms to look for (only the ones in the bundle)
COUNTRY_FORMS = {
    "China": ["china", "chinese"],
    "Russia": ["russia", "russian"],
    "Iran": ["iran", "iranian"],
    "North Korea": ["north korea", "north korean", "dprk"],
    "Vietnam": ["vietnam", "vietnamese"],
    "Ukraine": ["ukraine", "ukrainian"],
    "Lebanon": ["lebanon", "lebanese"],
    "South Korea": ["south korea", "south korean"],
    "USA": ["united states", "u.s.", "american"],
    "Pakistan": ["pakistan", "pakistani"],
}
# goal -> surface forms
GOAL_FORMS = {
    "espionage": ["espionage", "cyberespionage", "cyber espionage", "spying"],
    "financial gain": ["financial gain", "financially motivated", "cybercrime",
                        "ransomware", "monetary"],
    "sabotage": ["sabotage", "destructive", "wiper", "disruptive"],
}


def read_csv(name):
    with open(RAW / name, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def load_actors():
    """Return (surface->canonical, canonical->country, canonical->[goals])."""
    rows = read_csv("ThreatActors.csv")
    h = rows[0]
    ni, ci, gi = h.index("name"), h.index("country"), h.index("goals")
    names, kg_country, kg_goals = [], {}, {}
    for r in rows[1:]:
        if len(r) <= max(ni, ci, gi) or not r[ni].strip():
            continue
        n = r[ni].strip()
        names.append(n)
        c = r[ci].strip()
        kg_country[n] = c if c and c.lower() != "unknown" else None
        kg_goals[n] = [g.strip() for g in r[gi].split(",") if g.strip()]
    al = read_csv("Aliases.csv")
    ah = al[0]
    aki, avi = ah.index("name"), ah.index("alias")
    aliases = defaultdict(list)
    for r in al[1:]:
        if len(r) <= max(aki, avi):
            continue
        k, v = r[aki].strip(), r[avi].strip()
        if k and v:
            aliases[k].append(v)
    # map every name/alias back to its canonical actor; skip short ones (noise)
    surface = {}
    for n in names:
        for s in [n] + aliases.get(n, []):
            if len(s) >= 4:
                surface[s] = n
    return surface, kg_country, kg_goals


def load_techniques():
    rows = read_csv("Techniques.csv")
    h = rows[0]
    ti = h.index("name")
    techs = []
    for r in rows[1:]:
        if len(r) <= ti:
            continue
        t = r[ti].strip()
        # only keep names specific enough to not match by accident
        if t and (" " in t or len(t) >= 8):
            techs.append(t)
    return sorted(set(techs), key=len, reverse=True)


def present(surf, low):
    return re.search(r"\b" + re.escape(surf.lower()) + r"\b", low) is not None


def find_surfaces(text, surface_map):
    """Which actor names show up in the text, and their canonical forms.
    We keep the surface form since that's what a human would highlight."""
    low = text.lower()
    surfaces, canon = set(), set()
    for surf, c in surface_map.items():
        if present(surf, low):
            surfaces.add(surf)
            canon.add(c)
    return sorted(surfaces), canon


def find_terms(text, term_to_label):
    """Labels whose surface form appears in the text."""
    low = text.lower()
    return {label for surf, label in term_to_label.items() if present(surf, low)}


def excerpt_around_cves(text):
    """Grab a window centred on the first CVE mention."""
    text = re.sub(r"\s+", " ", text).strip()
    m = CVE_RE.search(text)
    if not m:
        return text[:EXCERPT_CHARS]
    start = max(0, m.start() - 350)
    return text[start:start + EXCERPT_CHARS]


def main():
    from pypdf import PdfReader
    actor_surface, kg_country, kg_goals = load_actors()
    technique_surface = {t: t for t in load_techniques()}

    records = []
    seen_ids = set()
    for pdf in sorted(glob.glob(str(REPORTS / "*.pdf"))):
        try:
            reader = PdfReader(pdf)
            raw = "".join((p.extract_text() or "") for p in reader.pages[:4])
        except Exception:
            continue
        if len(raw) < 500:
            continue
        if sum(1 for c in raw if ord(c) < 128) / len(raw) < 0.85:
            continue  # looks scanned or non-English, skip it
        ex = excerpt_around_cves(raw)

        cves = sorted({c.upper() for c in CVE_RE.findall(ex)})
        apts, canon = find_surfaces(ex, actor_surface)
        if not (cves and apts):
            continue
        techs = sorted(find_terms(ex, technique_surface))
        low = ex.lower()
        # country/goals come from the KG but only if they're mentioned here too
        countries = sorted({
            kg_country[c] for c in canon if kg_country.get(c)
            and any(present(s, low)
                    for s in COUNTRY_FORMS.get(kg_country[c], [kg_country[c]]))
        })
        goals = sorted({
            g for c in canon for g in kg_goals.get(c, [])
            if g in GOAL_FORMS and any(present(s, low) for s in GOAL_FORMS[g])
        })

        rid = os.path.splitext(os.path.basename(pdf))[0][:60]
        if rid in seen_ids:
            rid = rid + f"_{len(records)}"
        seen_ids.add(rid)
        records.append({
            "id": rid,
            "text": ex,
            "ground_truth": {
                "apt": apts, "cves": cves, "techniques": techs,
                "goals": goals, "country": countries,
            },
        })

    # trim to MAX_RECORDS, favouring the ones that fill the sparser fields
    # (techniques/goals/country) so those don't get starved at small N
    def richness(r):
        g = r["ground_truth"]
        return sum(1 for e in ("techniques", "goals", "country") if g[e])
    order = sorted(range(len(records)), key=lambda i: (-richness(records[i]), i))
    records = [records[i] for i in sorted(order[:MAX_RECORDS])]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} records to {OUT}")
    # how many records ended up with each field, and total labels
    for e in ["apt", "cves", "techniques", "goals", "country"]:
        n = sum(1 for r in records if r["ground_truth"][e])
        tot = sum(len(r["ground_truth"][e]) for r in records)
        print(f"  {e:11s}: {n:3d}/{len(records)} records, {tot} labels")


if __name__ == "__main__":
    main()
