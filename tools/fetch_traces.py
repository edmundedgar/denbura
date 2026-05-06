#!/usr/bin/env python3
"""Fetch and summarise OSM GPS traces for a tight bounding box."""

import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

OSM_API = "https://api.openstreetmap.org/api/0.6/trackpoints"

# Tight box around Mashiko town centre
LAT, LON = 36.476, 140.089
RADIUS = 0.02  # ~2km

BBOX = f"{LON-RADIUS},{LAT-RADIUS},{LON+RADIUS},{LAT+RADIUS}"
print(f"Bounding box: {BBOX}  (~{RADIUS*111:.1f}km radius)\n")

all_points = []

for page in range(50):
    url = f"{OSM_API}?bbox={BBOX}&page={page}"
    req = urllib.request.Request(url, headers={"User-Agent": "denbura-ev-planner/research"})
    with urllib.request.urlopen(req, timeout=60) as r:
        xml = r.read()

    root = ET.fromstring(xml)
    ns = {"gpx": "http://www.topografix.com/GPX/1/0"}
    points = root.findall(".//gpx:trkpt", ns)
    if not points:
        break

    for pt in points:
        lat = float(pt.attrib["lat"])
        lon = float(pt.attrib["lon"])
        ts  = pt.findtext("gpx:time", default="", namespaces=ns)
        ele = pt.findtext("gpx:ele",  default="", namespaces=ns)
        all_points.append((lat, lon, ts, ele))

    print(f"  Page {page}: {len(points)} points (total so far: {len(all_points):,})")
    if len(points) < 5000:
        break

print(f"\nTotal points: {len(all_points):,}")

# How many have timestamps?
with_time = sum(1 for p in all_points if p[2])
with_ele  = sum(1 for p in all_points if p[3])
print(f"With timestamp: {with_time:,} ({100*with_time//len(all_points)}%)")
print(f"With elevation: {with_ele:,}  ({100*with_ele//len(all_points)}%)")

# Sample 5 points
print("\nSample points:")
step = max(1, len(all_points) // 5)
for p in all_points[::step][:5]:
    print(f"  lat={p[0]:.5f}  lon={p[1]:.5f}  time={p[2] or '—'}  ele={p[3] or '—'}")
