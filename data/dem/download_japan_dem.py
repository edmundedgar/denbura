#!/usr/bin/env python3
"""Download Copernicus DEM GLO-30 tiles covering Japan."""

import os
import subprocess
import sys

# Bounding box for Japan (mainland + Ryukyu islands)
LAT_MIN, LAT_MAX = 24, 45
LON_MIN, LON_MAX = 122, 146

BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def tile_url(lat, lon):
    name = f"Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_DEM"
    return f"{BASE_URL}/{name}/{name}.tif"


def download_tile(lat, lon):
    url = tile_url(lat, lon)
    out_path = os.path.join(OUT_DIR, f"N{lat:02d}E{lon:03d}.tif")

    if os.path.exists(out_path):
        return "skip"

    result = subprocess.run(
        ["wget", "-q", "--server-response", "-O", out_path, url],
        capture_output=True, text=True
    )

    # Check HTTP status from wget's server-response output
    for line in result.stderr.splitlines():
        if "HTTP/" in line and ("404" in line or "403" in line):
            os.remove(out_path)
            return "miss"

    if result.returncode != 0 or os.path.getsize(out_path) == 0:
        if os.path.exists(out_path):
            os.remove(out_path)
        return "error"

    return "ok"


total = (LAT_MAX - LAT_MIN + 1) * (LON_MAX - LON_MIN + 1)
done, downloaded, skipped, missed, errors = 0, 0, 0, 0, 0

for lat in range(LAT_MIN, LAT_MAX + 1):
    for lon in range(LON_MIN, LON_MAX + 1):
        result = download_tile(lat, lon)
        done += 1
        if result == "ok":
            downloaded += 1
        elif result == "skip":
            skipped += 1
        elif result == "miss":
            missed += 1
        else:
            errors += 1

        print(
            f"[{done}/{total}] N{lat:02d}E{lon:03d} {result} "
            f"| got={downloaded} skip={skipped} ocean={missed} err={errors}",
            flush=True
        )

print(f"\nDone. Downloaded {downloaded} tiles to {OUT_DIR}")
