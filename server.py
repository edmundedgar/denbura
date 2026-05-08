#!/usr/bin/env python3
import json
import math
import os

import httpx
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import Response
from scipy.spatial import cKDTree

GH       = "http://localhost:2027"
TILE_DIR = "planet_gps/tiles"
RADIUS_M = 50   # metres to search around each segment midpoint
MIN_SAMPLES = 5  # minimum GPS points needed to trust the estimate

app = FastAPI()

# ---------------------------------------------------------------------------
# GPS tile loading
# ---------------------------------------------------------------------------

_tile_cache: dict = {}

def _tile_path(tlat: int, tlon: int) -> str:
    return os.path.join(TILE_DIR, f"{tlat:+04d}_{tlon:+04d}.npz")

def _load_tile(tlat: int, tlon: int):
    key = (tlat, tlon)
    if key not in _tile_cache:
        path = _tile_path(tlat, tlon)
        if os.path.exists(path):
            d = np.load(path)
            _tile_cache[key] = (
                d["lat"].astype(np.float32),
                d["lon"].astype(np.float32),
                d["speed"].astype(np.float32),
            )
        else:
            _tile_cache[key] = None
    return _tile_cache[key]

def _load_tiles_for_bbox(min_lat, max_lat, min_lon, max_lon):
    lats, lons, speeds = [], [], []
    for tlat in range(int(math.floor(min_lat)), int(math.floor(max_lat)) + 1):
        for tlon in range(int(math.floor(min_lon)), int(math.floor(max_lon)) + 1):
            tile = _load_tile(tlat, tlon)
            if tile is not None:
                lats.append(tile[0])
                lons.append(tile[1])
                speeds.append(tile[2])
    if not lats:
        return None
    return (np.concatenate(lats), np.concatenate(lons), np.concatenate(speeds))

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_path(path: dict, gps_lats, gps_lons, gps_speeds) -> int | None:
    details_time = path.get("details", {}).get("time", [])
    details_dist = path.get("details", {}).get("distance", [])
    if not details_time or not details_dist:
        return None

    coords = path["points"]["coordinates"]  # [lon, lat]
    mid_lat = sum(c[1] for c in coords) / len(coords)

    lat_m = 111_320.0
    lon_m = 111_320.0 * math.cos(math.radians(mid_lat))

    tree = cKDTree(np.column_stack([gps_lats * lat_m, gps_lons * lon_m]))

    total_ms = 0.0
    for (fi, ti, gh_ms), (_, _, dist_m) in zip(details_time, details_dist):
        seg = coords[fi:ti + 1]
        if not seg:
            total_ms += gh_ms
            continue
        mlat = (seg[0][1] + seg[-1][1]) * 0.5
        mlon = (seg[0][0] + seg[-1][0]) * 0.5
        idx = tree.query_ball_point([mlat * lat_m, mlon * lon_m], r=RADIUS_M)
        if len(idx) >= MIN_SAMPLES and dist_m > 0:
            speed_kmh = float(np.median(gps_speeds[idx]))
            total_ms += (dist_m / (speed_kmh / 3.6)) * 1000
        else:
            total_ms += gh_ms

    return round(total_ms)

# ---------------------------------------------------------------------------
# Route endpoint
# ---------------------------------------------------------------------------

@app.post("/route")
async def route(request: Request):
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except Exception:
        # Can't parse — just proxy as-is
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{GH}/route", content=body_bytes,
                                  headers={"Content-Type": "application/json"})
        return Response(content=r.content, status_code=r.status_code,
                        media_type="application/json")

    # Ask GH for per-segment time and distance details
    body.setdefault("details", [])
    for d in ("time", "distance"):
        if d not in body["details"]:
            body["details"].append(d)
    body["points_encoded"] = False

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{GH}/route",
                              content=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})

    if r.status_code != 200:
        return Response(content=r.content, status_code=r.status_code,
                        media_type="application/json")

    data = r.json()
    paths = data.get("paths", [])
    if not paths:
        return Response(content=r.content, status_code=200,
                        media_type="application/json")

    # Determine bbox across all paths
    all_coords = [c for p in paths for c in p["points"]["coordinates"]]
    min_lat = min(c[1] for c in all_coords)
    max_lat = max(c[1] for c in all_coords)
    min_lon = min(c[0] for c in all_coords)
    max_lon = max(c[0] for c in all_coords)

    gps = _load_tiles_for_bbox(min_lat, max_lat, min_lon, max_lon)

    for path in paths:
        if gps is not None:
            scored = _score_path(path, gps[0], gps[1], gps[2])
            if scored is not None:
                path["scored_time_ms"] = scored

    return Response(content=json.dumps(data).encode(), status_code=200,
                    media_type="application/json")
