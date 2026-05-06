#!/usr/bin/env python3
"""Show example OSM features around Mashiko."""

import urllib.request
import urllib.parse
import json

LAT, LON = 36.476, 140.089
RADIUS = 0.08

S, W, N, E = LAT-RADIUS, LON-RADIUS, LAT+RADIUS, LON+RADIUS

QUERY = f"""
[out:json][timeout:30];
(
  node["amenity"]({S},{W},{N},{E});
  node["shop"]({S},{W},{N},{E});
  way["highway"]["name"]({S},{W},{N},{E});
);
out body;
"""

url = "https://overpass-api.de/api/interpreter"
req = urllib.request.Request(
    url,
    data=urllib.parse.urlencode({"data": QUERY}).encode(),
    headers={"User-Agent": "denbura-ev-planner/research"},
)
with urllib.request.urlopen(req, timeout=40) as r:
    data = json.load(r)

nodes = [e for e in data["elements"] if e["type"] == "node"]
ways  = [e for e in data["elements"] if e["type"] == "way"]


def show_examples(title, items, key, values, n=5):
    print(f"── {title} ──")
    shown = 0
    for el in items:
        tags = el.get("tags", {})
        if tags.get(key) in values:
            lat = el.get("lat", "")
            lon = el.get("lon", "")
            print(f"  {lat}, {lon}")
            for k, v in sorted(tags.items()):
                print(f"    {k}: {v}")
            print()
            shown += 1
            if shown >= n:
                break


show_examples("Convenience stores", nodes, "shop", {"convenience"})
show_examples("Restaurants / fast food", nodes, "amenity", {"restaurant", "fast_food", "cafe"})
show_examples("Fuel stations", nodes, "amenity", {"fuel"})

print("── Named roads (sample) ──")
for way in ways[:8]:
    tags = way.get("tags", {})
    name = tags.get("name", "")
    name_en = tags.get("name:en", "")
    hw   = tags.get("highway", "")
    spd  = tags.get("maxspeed", "—")
    ref  = tags.get("ref", "")
    lit  = tags.get("lit", "—")
    print(f"  [{hw}] {name}  {name_en}  ref={ref}  maxspeed={spd}  lit={lit}")
print()
