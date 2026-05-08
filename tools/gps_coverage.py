#!/usr/bin/env python3
"""
Check GPS trace density along a route.
Fetches a route from GraphHopper, then scans japan_gps.csv.gz for
nearby points and reports coverage statistics.
"""

import gzip
import csv
import json
import math
import sys
import urllib.request
from collections import defaultdict

import numpy as np
from scipy.spatial import cKDTree

GH = "http://localhost:2027"
GPS_FILE = "planet_gps/japan_gps.csv.gz"
SEARCH_RADIUS_M = 50  # metres around each route point to look for GPS points

def fetch_route(from_lonlat, to_lonlat, profile="car_motorway"):
    body = json.dumps({
        "points": [from_lonlat, to_lonlat],
        "profile": profile,
        "ch.disable": True,
        "points_encoded": False,
    }).encode()
    req = urllib.request.Request(
        f"{GH}/route",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def deg_to_m(lat):
    """Approximate metres per degree at given latitude."""
    lat_m = 111_320
    lon_m = 111_320 * math.cos(math.radians(lat))
    return lat_m, lon_m

def load_gps_in_bbox(min_lat, max_lat, min_lon, max_lon, pad=0.05):
    """Load GPS points from CSV within a padded bounding box."""
    min_lat -= pad; max_lat += pad
    min_lon -= pad; max_lon += pad
    lats, lons = [], []
    with gzip.open(GPS_FILE, "rt", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) < 3:
                continue
            lat = float(row[1])
            lon = float(row[2])
            if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                lats.append(lat)
                lons.append(lon)
    return np.array(lats, dtype=np.float32), np.array(lons, dtype=np.float32)

def analyse(route_coords, gps_lats, gps_lons, radius_m):
    if len(gps_lats) == 0:
        print("No GPS points in bounding box!")
        return

    mid_lat = np.mean(gps_lats)
    lat_m, lon_m = deg_to_m(mid_lat)

    # Scale GPS points to metres for KDTree
    gps_y = gps_lats * lat_m
    gps_x = gps_lons * lon_m
    tree = cKDTree(np.column_stack([gps_y, gps_x]))

    counts = []
    for lon, lat in route_coords:
        py = lat * lat_m
        px = lon * lon_m
        n = tree.query_ball_point([py, px], r=radius_m, return_length=True)
        counts.append(n)

    counts = np.array(counts)
    covered = counts > 0
    print(f"\nRoute points:          {len(counts)}")
    print(f"GPS points in bbox:    {len(gps_lats):,}")
    print(f"Search radius:         {radius_m}m")
    print(f"Points with ≥1 match:  {covered.sum()} / {len(counts)} ({100*covered.mean():.1f}%)")
    print(f"Points with ≥5 match:  {(counts>=5).sum()} / {len(counts)} ({100*(counts>=5).mean():.1f}%)")
    print(f"Points with ≥20 match: {(counts>=20).sum()} / {len(counts)} ({100*(counts>=20).mean():.1f}%)")
    print(f"Median count:          {np.median(counts):.0f}")
    print(f"Mean count:            {counts.mean():.1f}")
    print(f"Max count:             {counts.max()}")

    # Distribution
    buckets = [0, 1, 5, 10, 20, 50, 100]
    print("\nDistribution:")
    for lo, hi in zip(buckets, buckets[1:] + [999999]):
        n = ((counts >= lo) & (counts < hi)).sum()
        bar = "#" * (n * 40 // len(counts))
        print(f"  {lo:4d}–{hi if hi < 999999 else '∞':>6}: {n:5d}  {bar}")

def main():
    # Mashiko → Shibuya expressway (our main test route)
    from_ll = [140.089, 36.476]   # Mashiko
    to_ll   = [139.702, 35.658]   # Shibuya

    print(f"Fetching route {from_ll} → {to_ll} ...")
    data = fetch_route(from_ll, to_ll, profile="car_motorway")
    path = data["paths"][0]
    coords = path["points"]["coordinates"]  # [lon, lat]

    lats = [c[1] for c in coords]
    lons = [c[0] for c in coords]
    bbox = (min(lats), max(lats), min(lons), max(lons))
    print(f"Route: {path['distance']/1000:.1f} km, {len(coords)} points")
    print(f"Bbox: lat {bbox[0]:.3f}–{bbox[1]:.3f}, lon {bbox[2]:.3f}–{bbox[3]:.3f}")

    print(f"\nLoading GPS points in bbox from {GPS_FILE} ...")
    gps_lats, gps_lons = load_gps_in_bbox(*bbox)
    print(f"Loaded {len(gps_lats):,} GPS points")

    analyse(coords, gps_lats, gps_lons, SEARCH_RADIUS_M)

if __name__ == "__main__":
    main()
