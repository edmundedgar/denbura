#!/usr/bin/env python3
"""Check density of public OSM GPS traces for a set of locations."""

import urllib.request
import xml.etree.ElementTree as ET

OSM_API = "https://api.openstreetmap.org/api/0.6/trackpoints"
RADIUS  = 0.15  # degrees (~15km box)

LOCATIONS = [
    ("Mashiko",           36.476, 140.089),
    ("Utsunomiya",        36.555, 139.882),
    ("Koriyama",          37.400, 140.367),
    ("Saku (expressway)", 36.248, 138.476),
    ("Nagoya",            35.181, 136.907),
]


def fetch_page(lat, lon, page=0):
    bbox = f"{lon-RADIUS},{lat-RADIUS},{lon+RADIUS},{lat+RADIUS}"
    url  = f"{OSM_API}?bbox={bbox}&page={page}"
    req  = urllib.request.Request(url, headers={"User-Agent": "denbura-ev-planner/research"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def count_points(xml_bytes):
    root = ET.fromstring(xml_bytes)
    ns   = {"gpx": "http://www.topografix.com/GPX/1/0"}
    return len(root.findall(".//gpx:trkpt", ns))


def count_all_pages(lat, lon):
    total = 0
    for page in range(20):  # cap at 100k points
        xml   = fetch_page(lat, lon, page)
        n     = count_points(xml)
        total += n
        if n < 5000:
            break
    return total


def check_location(name, lat, lon):
    try:
        total = count_all_pages(lat, lon)
        print(f"  {name:<25} {total:>7,} points")
    except Exception as e:
        print(f"  {name:<25} error: {e}")


print(f"OSM public GPS trace density (±{RADIUS}° box)\n")
for name, lat, lon in LOCATIONS:
    check_location(name, lat, lon)
