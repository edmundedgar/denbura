#!/usr/bin/env python3
"""Show raw GPS trace data for Mashiko."""

import urllib.request
import xml.etree.ElementTree as ET

OSM_API = "https://api.openstreetmap.org/api/0.6/trackpoints"

LAT, LON = 36.476, 140.089
RADIUS = 0.02
BBOX = f"{LON-RADIUS},{LAT-RADIUS},{LON+RADIUS},{LAT+RADIUS}"

req = urllib.request.Request(
    f"{OSM_API}?bbox={BBOX}&page=0",
    headers={"User-Agent": "denbura-ev-planner/research"},
)
with urllib.request.urlopen(req, timeout=30) as r:
    xml = r.read()

root = ET.fromstring(xml)
ns = {"gpx": "http://www.topografix.com/GPX/1/0"}

# Traces are grouped into <trkseg> segments — each segment is one continuous recording
segments = root.findall(".//gpx:trkseg", ns)
print(f"Segments in page: {len(segments)}\n")

for i, seg in enumerate(segments[:6]):
    points = seg.findall("gpx:trkpt", ns)
    has_time = any(p.findtext("gpx:time", namespaces=ns) for p in points)

    print(f"Segment {i+1}  ({len(points)} points, {'timestamped' if has_time else 'no timestamps'})")

    prev_lat = prev_lon = prev_time = None
    for pt in points[:8]:
        lat  = float(pt.attrib["lat"])
        lon  = float(pt.attrib["lon"])
        time = pt.findtext("gpx:time", default=None, namespaces=ns)

        speed_str = ""
        if time and prev_time:
            from datetime import datetime
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            try:
                dt = (datetime.strptime(time, fmt) - datetime.strptime(prev_time, fmt)).total_seconds()
                if dt > 0:
                    import math
                    dlat = math.radians(lat - prev_lat)
                    dlon = math.radians(lon - prev_lon)
                    a = math.sin(dlat/2)**2 + math.cos(math.radians(prev_lat)) * math.cos(math.radians(lat)) * math.sin(dlon/2)**2
                    dist_m = 6371000 * 2 * math.asin(math.sqrt(a))
                    speed_kmh = (dist_m / dt) * 3.6
                    speed_str = f"  → {speed_kmh:.0f} km/h"
            except ValueError:
                pass

        print(f"  {lat:.6f}, {lon:.6f}  {time or '(no time)'}{speed_str}")

        prev_lat, prev_lon, prev_time = lat, lon, time

    if len(points) > 8:
        print(f"  ... ({len(points) - 8} more points)")
    print()
