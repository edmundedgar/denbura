#!/usr/bin/env python3
"""Check OSM tags for the Shuto Expressway around central Tokyo."""

import urllib.request
import urllib.parse
import json

# Central Tokyo bounding box
QUERY = """
[out:json][timeout:30];
way["highway"](35.60,139.60,35.75,139.85);
out tags;
"""

req = urllib.request.Request(
    "https://overpass-api.de/api/interpreter",
    data=urllib.parse.urlencode({"data": QUERY}).encode(),
    headers={"User-Agent": "denbura-ev-planner/research"},
)
with urllib.request.urlopen(req, timeout=40) as r:
    data = json.load(r)

from collections import Counter
hw_types = Counter()
for way in data["elements"]:
    tags = way.get("tags", {})
    hw_types[tags.get("highway", "?")] += 1

print("Highway types in central Tokyo:")
for hw, count in hw_types.most_common(15):
    print(f"  {count:>5}  {hw}")

print("\nSample motorway/trunk ways (up to 10):")
shown = 0
for way in data["elements"]:
    tags = way.get("tags", {})
    if tags.get("highway") in ("motorway", "motorway_link", "trunk"):
        relevant = {k: v for k, v in tags.items()
                    if k in ("highway", "name", "name:en", "ref", "access",
                             "motorcar", "toll", "maxspeed", "operator")}
        print(f"  {relevant}")
        shown += 1
        if shown >= 10:
            break
