#!/usr/bin/env python3
"""Fetch hot spring locations in Japan from Overpass API."""
import json
import urllib.parse
import urllib.request

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

QUERY = """
[out:json][timeout:120];
(
  node["amenity"="public_bath"](31,129,46,146);
  way["amenity"="public_bath"](31,129,46,146);
  relation["amenity"="public_bath"](31,129,46,146);
  node["leisure"="hot_spring"](31,129,46,146);
  way["leisure"="hot_spring"](31,129,46,146);
  node["tourism"="hot_spring"](31,129,46,146);
  way["tourism"="hot_spring"](31,129,46,146);
);
out center tags;
"""

def main():
    print("Querying Overpass API...")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": QUERY}).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "denbura-ev-planner/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=150) as resp:
        data = json.load(resp)

    results = []
    seen = set()
    for el in data["elements"]:
        if el["type"] == "node":
            lat, lon = el["lat"], el["lon"]
        else:
            c = el.get("center")
            if not c:
                continue
            lat, lon = c["lat"], c["lon"]

        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:en") or tags.get("name:ja")
        if not name:
            continue

        key = (round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)

        results.append({
            "lat":     lat,
            "lon":     lon,
            "name":    name,
            "website": tags.get("website") or tags.get("contact:website") or "",
            "fee":     tags.get("fee", ""),
            "opening_hours": tags.get("opening_hours", ""),
        })

    results.sort(key=lambda x: x["name"])
    out = "frontend/hot_springs.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(results)} hot springs → {out}")

if __name__ == "__main__":
    main()
