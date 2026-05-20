#!/usr/bin/env python3
"""Build a database of IC positions for non-NEXCO toll roads from OSM PBF.

Strategy:
  Pass 1 – scan ways (toll=yes motorway*) to collect road→node-ID sets;
           scan nodes to collect barrier=toll_booth operator tags.
  Cross-reference: roads whose toll-booth nodes carry non-NEXCO operators are kept.
  Pass 2 – scan with locations=True to get coordinates of motorway_junction nodes
           belonging to kept roads.

Output:
  data/toll_ics_auto.json      – auto-generated from OSM
  data/toll_ics_overrides.json – created if absent; hand-edit to fix/add ICs

Usage:
  python3 tools/build_toll_ic_db.py [kanto|japan]   (default: kanto)
"""

import json
import math
import os
import re
import sys
import osmium
from collections import defaultdict
from datetime import datetime, timezone

REGION = sys.argv[1] if len(sys.argv) > 1 else "kanto"
PBF = {
    "kanto": "data/osm/kanto-latest.osm.pbf",
    "japan": "data/osm/japan-latest.osm.pbf",
}[REGION]

OUT_AUTO      = "data/toll_ics_auto.json"
OUT_OVERRIDES = "data/toll_ics_overrides.json"

# Operators to exclude: NEXCO companies and major urban expressway authorities.
# We match by substring so partial names / abbreviations are caught.
# Plain "NEXCO" is listed first so it matches any NEXCO variant.
SKIP_OPERATORS = [
    "NEXCO",
    "東日本高速道路",
    "中日本高速道路",
    "西日本高速道路",
    "首都高速道路", "首都高速",
    "阪神高速道路", "阪神高速",
    "本州四国連絡高速道路", "本州四国連絡橋",
    "名古屋高速道路",
    "福岡北九州高速道路",
    "広島高速道路",
    "仙台都市高速",
]

# Road name substrings that identify urban expressways regardless of operator tag.
SKIP_ROAD_NAMES = [
    "首都高速", "阪神高速", "名古屋高速", "福岡北九州高速",
    "広島高速", "仙台都市高速",
]

def _is_skip(operator: str) -> bool:
    return any(s in operator for s in SKIP_OPERATORS)

def _is_skip_road_name(name: str) -> bool:
    return any(s in name for s in SKIP_ROAD_NAMES)

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))

def road_key(tags: dict) -> str:
    """Prefer bare E-number ref; fall back to road name."""
    ref  = tags.get("ref",  "").split(";")[0].strip()
    name = tags.get("name", "").strip()
    return ref or name


# ---------------------------------------------------------------------------
# Pass 1: collect roads and toll-booth operators (no location index needed)
# ---------------------------------------------------------------------------

class Pass1Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        # key → {name, ref, way_op, node_ids, booth_ops}
        self.roads: dict = {}
        # node_id → set of road keys (for reverse lookup)
        self.node_to_roads: dict = defaultdict(set)
        # barrier=toll_booth node_id → operator string
        self.booths: dict = {}

    def way(self, w):
        tags = dict(w.tags)
        if tags.get("highway") not in ("motorway", "motorway_link", "trunk", "trunk_link"):
            return
        if tags.get("toll") != "yes":
            return

        key = road_key(tags)
        if not key:
            return

        ref  = tags.get("ref",  "").split(";")[0].strip()
        name = tags.get("name", "")
        op   = tags.get("operator", "")

        if key not in self.roads:
            self.roads[key] = {
                "name": name, "ref": ref, "way_op": op,
                "node_ids": set(), "booth_ops": set(),
            }
        else:
            r = self.roads[key]
            if name and not r["name"]:   r["name"]   = name
            if ref  and not r["ref"]:    r["ref"]    = ref
            if op   and not r["way_op"]: r["way_op"] = op

        node_ids = self.roads[key]["node_ids"]
        for n in w.nodes:
            node_ids.add(n.ref)
            self.node_to_roads[n.ref].add(key)

    def node(self, n):
        tags = dict(n.tags)
        if tags.get("barrier") == "toll_booth":
            op = tags.get("operator", "")
            if op:
                self.booths[n.id] = op


# ---------------------------------------------------------------------------
# Pass 2: collect junction coordinates (location index required)
# ---------------------------------------------------------------------------

class Pass2Handler(osmium.SimpleHandler):
    def __init__(self, target_ids: set):
        super().__init__()
        self.target = target_ids
        # node_id → {lat, lon, name, ref}
        self.junctions: dict = {}

    def node(self, n):
        if n.id not in self.target:
            return
        if not n.location.valid():
            return
        tags = dict(n.tags)
        if tags.get("highway") != "motorway_junction":
            return
        self.junctions[n.id] = {
            "lat": round(n.location.lat, 6),
            "lon": round(n.location.lon, 6),
            "name": tags.get("name", ""),
            "ref":  tags.get("ref",  ""),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_pa(name: str) -> bool:
    """Return True for Parking Areas / Service Areas — not toll interchange points."""
    return "ＰＡ" in name or "PA" in name or "ＳＡ" in name or "SA" in name

def dedupe(ics: list, threshold_m: float = 200) -> list:
    """Deduplicate by name first (same name = same IC, different carriageway),
    then by proximity for any unnamed nodes."""
    out: list = []
    seen_names: set = set()
    for ic in ics:
        name = ic["name"]
        # Filter out Parking Areas / Service Areas
        if _is_pa(name):
            continue
        # Deduplicate by name (non-empty names only)
        if name:
            if name in seen_names:
                continue
            seen_names.add(name)
        else:
            # Unnamed nodes: deduplicate by proximity
            if any(haversine_m(ic["lat"], ic["lon"], d["lat"], d["lon"]) < threshold_m
                   for d in out):
                continue
        out.append(ic)
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

print(f"Pass 1: scanning {PBF}...", flush=True)
p1 = Pass1Handler()
p1.apply_file(PBF)
print(f"  Candidate roads   : {len(p1.roads)}", flush=True)
print(f"  Toll-booth nodes  : {len(p1.booths)}", flush=True)

# Propagate toll-booth operators to roads
for nid, booth_op in p1.booths.items():
    for key in p1.node_to_roads.get(nid, set()):
        p1.roads[key]["booth_ops"].add(booth_op)

# Filter: include roads unless there is evidence they are NEXCO/urban.
# Roads without any operator tag are included unless they carry an E-number ref,
# which is the national numbering system used exclusively by NEXCO expressways.
keep: dict = {}
for key, road in p1.roads.items():
    way_op    = road["way_op"]
    booth_ops = road["booth_ops"]
    ref       = road["ref"]

    all_ops = ([way_op] if way_op else []) + list(booth_ops)

    # Skip if the road name itself looks like an urban expressway
    if _is_skip_road_name(road["name"]):
        continue

    # Skip if any operator is NEXCO/urban
    if any(_is_skip(op) for op in all_ops if op):
        continue

    # Skip E-number refs (E1, E4, E4A …) with no confirmed non-NEXCO operator —
    # all NEXCO motorways carry an E-number; prefectural roads do not.
    non_skip_ops = [op for op in all_ops if op and not _is_skip(op)]
    if re.match(r'^E\d', ref) and not non_skip_ops:
        continue

    keep[key] = {
        "name":     road["name"],
        "ref":      road["ref"],
        "operator": non_skip_ops[0] if non_skip_ops else "",
        "node_ids": road["node_ids"],
    }

print(f"  Roads after filter: {len(keep)}", flush=True)
for key, r in sorted(keep.items()):
    print(f"    {key}: {r['name']!r}  operator={r['operator']!r}", flush=True)

if not keep:
    print("No qualifying roads found — nothing to write.", flush=True)
    sys.exit(0)

# Collect all node IDs we need locations for
target_ids = set()
for r in keep.values():
    target_ids.update(r["node_ids"])

print(f"\nPass 2: resolving coordinates for {len(target_ids)} nodes...", flush=True)
p2 = Pass2Handler(target_ids)
p2.apply_file(PBF, locations=True)
print(f"  Junction nodes found: {len(p2.junctions)}", flush=True)

# Assemble output
output_roads: dict = {}
for key, road in sorted(keep.items()):
    raw = [p2.junctions[nid] for nid in road["node_ids"] if nid in p2.junctions]
    ics = dedupe(raw)
    ics.sort(key=lambda x: x["lat"])  # south→north; good enough for initial ordering

    if not ics:
        print(f"  WARNING: no junction nodes found for {key!r} — skipping", flush=True)
        continue

    output_roads[key] = {
        "name":     road["name"],
        "ref":      road["ref"],
        "operator": road["operator"],
        "ics":      ics,
    }

result = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "source_pbf":   PBF,
    "roads":        output_roads,
}

with open(OUT_AUTO, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"\nWrote {OUT_AUTO}  ({len(output_roads)} roads)")

for key, road in sorted(output_roads.items()):
    print(f"  {key}: {road['name']}  ({len(road['ics'])} ICs)")
    for ic in road["ics"]:
        print(f"    {ic['name']!r:20s}  {ic['lat']:.5f},{ic['lon']:.5f}  ref={ic['ref']!r}")

# Create override template if it doesn't exist yet
if not os.path.exists(OUT_OVERRIDES):
    template = {
        "_doc": (
            "Manual overrides for toll IC positions. "
            "If a road key exists here, its 'ics' list REPLACES the auto-generated one. "
            "You can also add roads that OSM didn't detect."
        ),
        "roads": {},
    }
    with open(OUT_OVERRIDES, "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    print(f"Created empty {OUT_OVERRIDES}")
