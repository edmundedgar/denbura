#!/usr/bin/env python3
"""
Stream through the OSM GPS planet dump and extract Japan trackpoints.
Output: planet_gps/japan_gps.csv.gz with columns: track_id, lat, lon, time
"""

import tarfile
import gzip
import csv
import re
import time

# Japan bounding box
MIN_LAT, MAX_LAT = 31.0, 45.5
MIN_LON, MAX_LON = 130.0, 146.0

# Match a trkpt block including its children
TRKPT_RE = re.compile(
    rb'<trkpt\s+lat="([^"]+)"\s+lon="([^"]+)"[^>]*>|'
    rb'<trkpt\s+lon="([^"]+)"\s+lat="([^"]+)"[^>]*>|'
    rb'</trkpt>|'
    rb'<time>([^<]+)</time>'
)

def parse_gpx(content, track_id, writer):
    count = 0
    in_trkpt = False
    cur_lat = cur_lon = cur_time = None
    for m in TRKPT_RE.finditer(content):
        g = m.groups()
        if g[0] is not None:          # lat= first
            in_trkpt = True
            cur_lat, cur_lon, cur_time = g[0].decode(), g[1].decode(), ""
        elif g[2] is not None:        # lon= first
            in_trkpt = True
            cur_lat, cur_lon, cur_time = g[3].decode(), g[2].decode(), ""
        elif m.group(0) == b"</trkpt>":
            if in_trkpt and cur_lat:
                lat = float(cur_lat)
                lon = float(cur_lon)
                if MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON:
                    writer.writerow([track_id, f"{lat:.7f}", f"{lon:.7f}", cur_time])
                    count += 1
            in_trkpt = False
            cur_lat = cur_lon = cur_time = None
        elif in_trkpt and g[4] is not None:  # <time>
            cur_time = g[4].decode()
    return count

def main():
    input_path = "planet_gps/gpx-planet-2013-04-09.tar.xz"
    output_path = "planet_gps/japan_gps.csv.gz"

    total_tracks = 0
    total_points = 0
    files_seen = 0
    t0 = time.time()

    with gzip.open(output_path, "wt", newline="", encoding="utf-8") as gf:
        writer = csv.writer(gf)
        writer.writerow(["track_id", "lat", "lon", "time"])

        with tarfile.open(input_path, "r:xz") as tf:
            for member in tf:
                if not member.name.endswith(".gpx"):
                    continue
                files_seen += 1
                f = tf.extractfile(member)
                if f is None:
                    continue
                content = f.read()
                track_id = member.name.rsplit("/", 1)[-1].replace(".gpx", "")
                n = parse_gpx(content, track_id, writer)
                if n:
                    total_tracks += 1
                    total_points += n

                if files_seen % 10000 == 0:
                    elapsed = time.time() - t0
                    print(
                        f"[{elapsed:.0f}s] {files_seen:,} files scanned, "
                        f"{total_tracks:,} Japan tracks, "
                        f"{total_points:,} points",
                        flush=True,
                    )

    elapsed = time.time() - t0
    print(
        f"Done in {elapsed:.0f}s. "
        f"{files_seen:,} files scanned, "
        f"{total_tracks:,} tracks with Japan points, "
        f"{total_points:,} total points -> {output_path}"
    )

if __name__ == "__main__":
    main()
