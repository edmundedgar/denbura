#!/usr/bin/env python3
"""
Aggregate OSM GPS trace speeds by road classification around a location.
Uses nearest-node assignment (not full map-matching) as a first approximation.
"""

import math
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import json
from collections import defaultdict
from datetime import datetime

import numpy as np
from scipy.spatial import KDTree

LAT, LON = 36.476, 140.089
RADIUS    = 0.05   # ~5km box
SPEED_MIN =  5     # km/h — filter stationary
SPEED_MAX = 130    # km/h — filter GPS glitches

S, W, N, E = LAT-RADIUS, LON-RADIUS, LAT+RADIUS, LON+RADIUS


# ── 1. Fetch road geometries from Overpass ─────────────────────────────────

print("Fetching road data from Overpass…")

QUERY = f"""
[out:json][timeout:60];
way["highway"]({S},{W},{N},{E});
out geom;
"""

req = urllib.request.Request(
    "https://overpass-api.de/api/interpreter",
    data=urllib.parse.urlencode({"data": QUERY}).encode(),
    headers={"User-Agent": "denbura-ev-planner/research"},
)
with urllib.request.urlopen(req, timeout=90) as r:
    road_data = json.load(r)

# Build list of (lat, lon, highway_class) for every node in every road way
road_points  = []   # (lat, lon)
road_classes = []   # highway classification for each point

for way in road_data["elements"]:
    hw = way.get("tags", {}).get("highway", "unknown")
    for node in way.get("geometry", []):
        road_points.append((node["lat"], node["lon"]))
        road_classes.append(hw)

road_points  = np.array(road_points)
road_classes = np.array(road_classes)
tree = KDTree(road_points)
print(f"  {len(road_data['elements'])} road ways, {len(road_points)} nodes indexed\n")


# ── 2. Fetch GPS traces and compute speeds ─────────────────────────────────

print("Fetching GPS traces…")

OSM_API = "https://api.openstreetmap.org/api/0.6/trackpoints"
BBOX    = f"{LON-RADIUS},{LAT-RADIUS},{LON+RADIUS},{LAT+RADIUS}"

speed_observations = []  # (lat, lon, speed_kmh)

for page in range(20):
    time.sleep(0.5)  # polite delay
    req = urllib.request.Request(
        f"{OSM_API}?bbox={BBOX}&page={page}",
        headers={"User-Agent": "denbura-ev-planner/research"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        xml = r.read()

    root = ET.fromstring(xml)
    ns   = {"gpx": "http://www.topografix.com/GPX/1/0"}
    segs = root.findall(".//gpx:trkseg", ns)
    if not segs:
        break

    page_obs = 0
    for seg in segs:
        pts = seg.findall("gpx:trkpt", ns)
        prev_lat = prev_lon = prev_time = None

        for pt in pts:
            lat  = float(pt.attrib["lat"])
            lon  = float(pt.attrib["lon"])
            t    = pt.findtext("gpx:time", default=None, namespaces=ns)

            if t and prev_time:
                try:
                    dt = (datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ") -
                          datetime.strptime(prev_time, "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
                    if dt > 0:
                        dlat = math.radians(lat - prev_lat)
                        dlon = math.radians(lon - prev_lon)
                        a    = (math.sin(dlat/2)**2 +
                                math.cos(math.radians(prev_lat)) *
                                math.cos(math.radians(lat)) *
                                math.sin(dlon/2)**2)
                        dist_m  = 6371000 * 2 * math.asin(math.sqrt(a))
                        speed   = (dist_m / dt) * 3.6
                        if SPEED_MIN <= speed <= SPEED_MAX:
                            speed_observations.append((lat, lon, speed))
                            page_obs += 1
                except ValueError:
                    pass

            if t:
                prev_lat, prev_lon, prev_time = lat, lon, t
            else:
                prev_lat = prev_lon = prev_time = None  # break chain on missing timestamp

    total_pts = sum(len(s.findall("gpx:trkpt", ns)) for s in segs)
    print(f"  Page {page}: {total_pts} points → {page_obs} speed observations")
    if total_pts < 5000:
        break

print(f"\nTotal speed observations: {len(speed_observations)}\n")


# ── 3. Assign each observation to nearest road class ──────────────────────

obs_coords = np.array([(lat, lon) for lat, lon, _ in speed_observations])
_, indices  = tree.query(obs_coords)
obs_classes = road_classes[indices]


# ── 4. Aggregate ───────────────────────────────────────────────────────────

by_class = defaultdict(list)
for (_, _, speed), cls in zip(speed_observations, obs_classes):
    by_class[cls].append(speed)

CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary",
           "unclassified", "residential", "service", "track"]

print(f"{'Road class':<18} {'N':>5}  {'median':>7}  {'p25':>5}  {'p75':>5}  {'p85':>5}")
print("─" * 55)
for cls in CLASSES:
    speeds = by_class.get(cls)
    if not speeds:
        continue
    arr = np.array(speeds)
    print(f"{cls:<18} {len(arr):>5}  "
          f"{np.median(arr):>6.0f}   "
          f"{np.percentile(arr,25):>5.0f}  "
          f"{np.percentile(arr,75):>5.0f}  "
          f"{np.percentile(arr,85):>5.0f}")
