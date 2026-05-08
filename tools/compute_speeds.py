#!/usr/bin/env python3
"""
Compute GPS speeds from japan_gps.csv.gz and save as tiled numpy files.

Input:  planet_gps/japan_gps.csv.gz  (track_id, lat, lon, time)
Output: planet_gps/tiles/{lat}_{lon}.npz  per 1°×1° tile
        each tile has arrays: lat (f32), lon (f32), hour (u8), speed (f16)

Speed computation:
  - Consecutive trackpoints from the same track, sorted by timestamp
  - Midpoint stored with hour-of-day and km/h speed
  - Filtered: 2–180 km/h, gap ≤ 60 s, step distance ≤ 3 km
"""

import array
import gzip
import csv
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

GPS_FILE  = "planet_gps/japan_gps.csv.gz"
TILE_DIR  = "planet_gps/tiles"
MIN_SPEED =   2.0  # km/h
MAX_SPEED = 180.0  # km/h
MAX_GAP_S =  60    # seconds between consecutive points
MAX_DIST_M = 3000  # metres per step (catches teleports)

def parse_time(s):
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp(), dt.hour
    except Exception:
        return None, None

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def process_track(points):
    """points: list of [lat, lon, timestamp, hour] — returns speed-sample tuples."""
    if len(points) < 2:
        return
    points.sort(key=lambda p: p[2])
    for i in range(len(points) - 1):
        lat1, lon1, t1, h1 = points[i]
        lat2, lon2, t2, _  = points[i + 1]
        dt = t2 - t1
        if dt <= 0 or dt > MAX_GAP_S:
            continue
        dist = haversine_m(lat1, lon1, lat2, lon2)
        if dist > MAX_DIST_M:
            continue
        speed = (dist / dt) * 3.6
        if not (MIN_SPEED <= speed <= MAX_SPEED):
            continue
        mid_lat = (lat1 + lat2) * 0.5
        mid_lon = (lon1 + lon2) * 0.5
        tk = (int(math.floor(mid_lat)), int(math.floor(mid_lon)))
        yield tk, mid_lat, mid_lon, h1, speed

def main():
    os.makedirs(TILE_DIR, exist_ok=True)

    # Per-tile accumulators using array.array for memory efficiency
    # 'f' = float32, 'B' = uint8
    tiles = defaultdict(lambda: {
        "lat":   array.array("f"),
        "lon":   array.array("f"),
        "hour":  array.array("B"),
        "speed": array.array("f"),
    })

    total_tracks = 0
    total_in     = 0
    total_out    = 0
    t0           = time.time()

    current_id  = None
    current_pts = []

    def flush():
        nonlocal total_tracks, total_out
        if not current_pts:
            return
        total_tracks += 1
        for tk, mlat, mlon, hour, speed in process_track(current_pts):
            t = tiles[tk]
            t["lat"].append(mlat)
            t["lon"].append(mlon)
            t["hour"].append(hour)
            t["speed"].append(speed)
            total_out += 1

    print(f"Reading {GPS_FILE} ...")
    with gzip.open(GPS_FILE, "rt", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) < 4:
                continue
            tid, lat_s, lon_s, time_s = row[0], row[1], row[2], row[3]
            total_in += 1

            if tid != current_id:
                flush()
                current_id  = tid
                current_pts = []

            ts, hour = parse_time(time_s)
            if ts is None:
                continue
            current_pts.append([float(lat_s), float(lon_s), ts, hour])

            if total_in % 5_000_000 == 0:
                elapsed = time.time() - t0
                print(
                    f"  [{elapsed:5.0f}s] {total_in/1e6:.1f}M in, "
                    f"{total_out/1e6:.2f}M out, {len(tiles)} tiles"
                )

    flush()  # last track

    print(f"\nWriting {len(tiles)} tile files to {TILE_DIR}/ ...")
    for (tlat, tlon), arrs in sorted(tiles.items()):
        fname = os.path.join(TILE_DIR, f"{tlat:+04d}_{tlon:+04d}.npz")
        np.savez_compressed(
            fname,
            lat   = np.frombuffer(arrs["lat"],   dtype=np.float32),
            lon   = np.frombuffer(arrs["lon"],    dtype=np.float32),
            hour  = np.frombuffer(arrs["hour"],   dtype=np.uint8),
            speed = np.frombuffer(arrs["speed"],  dtype=np.float32).astype(np.float16),
        )
        n = len(arrs["lat"])
        print(f"  ({tlat:+d},{tlon:+d}) → {fname}: {n:,} samples")

    elapsed = time.time() - t0
    print(
        f"\nDone in {elapsed:.0f}s. "
        f"{total_tracks:,} tracks, {total_in:,} input points, "
        f"{total_out:,} speed samples → {len(tiles)} tiles"
    )

if __name__ == "__main__":
    main()
