#!/usr/bin/env python3
import asyncio
import json
import logging
import math
import os
import time

import httpx
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("denbura")
from scipy.spatial import cKDTree

GH       = "http://localhost:2027"
TILE_DIR = "planet_gps/tiles"
RADIUS_M    = 50   # metres to search around each segment midpoint for GPS scoring
MIN_SAMPLES =  5   # minimum GPS points needed to trust the estimate

CHARGER_MAX_DIST_M = 20_000  # pre-filter: chargers within 20 km of route polyline
DETOUR_MAX_RATIO   =   1.20  # keep via-charger routes up to 20% longer than direct
MAX_CHARGER_ROUTES =      5  # cap on charger routes returned per request

app = FastAPI()

# ---------------------------------------------------------------------------
# Charger data (loaded once at startup)
# ---------------------------------------------------------------------------

_CHARGER_FILE = "frontend/flash_chargers.json"
_chargers: list = []
if os.path.exists(_CHARGER_FILE):
    with open(_CHARGER_FILE, encoding="utf-8") as _f:
        _chargers = json.load(_f)

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
# GPS scoring
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
# Charger routing helpers
# ---------------------------------------------------------------------------

def _chargers_near_path(coords: list) -> list:
    """Return chargers within CHARGER_MAX_DIST_M of the route polyline."""
    if not _chargers:
        return []

    mid_lat = sum(c[1] for c in coords) / len(coords)
    lat_m = 111_320.0
    lon_m = 111_320.0 * math.cos(math.radians(mid_lat))

    route_yx = np.column_stack([
        np.array([c[1] for c in coords], dtype=np.float32) * lat_m,
        np.array([c[0] for c in coords], dtype=np.float32) * lon_m,
    ])
    tree = cKDTree(route_yx)

    nearby = []
    for c in _chargers:
        cy = c["lat"] * lat_m
        cx = c["lon"] * lon_m
        dist, _ = tree.query([cy, cx])
        if dist <= CHARGER_MAX_DIST_M:
            nearby.append((dist, c))

    nearby.sort()
    return [c for _, c in nearby]


async def _route_via_charger(client: httpx.AsyncClient, start_ll: list,
                              charger: dict, end_ll: list, profile: str) -> dict | None:
    body = {
        "points": [start_ll, [charger["lon"], charger["lat"]], end_ll],
        "profile": profile,
        "ch.disable": True,
        "points_encoded": False,
        "details": ["time", "distance"],
    }
    try:
        r = await client.post(f"{GH}/route", content=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        paths = r.json().get("paths", [])
        if not paths:
            return None
        path = paths[0]
        path["charger"] = {
            "name":       charger["name"],
            "max_output": charger.get("max_output", ""),
            "address":    charger.get("address", ""),
            "lat":        charger["lat"],
            "lon":        charger["lon"],
        }
        return path
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------

class _T:
    """Lightweight wall+CPU timer. Use as: t = _T(); ...; t.mark("label")"""
    def __init__(self):
        self._wall0 = time.perf_counter()
        self._cpu0  = time.process_time()
        self._marks: list[tuple] = []

    def mark(self, label: str):
        wall = time.perf_counter() - self._wall0
        cpu  = time.process_time()  - self._cpu0
        self._marks.append((label, wall, cpu))
        self._wall0 = time.perf_counter()
        self._cpu0  = time.process_time()

    def report(self, prefix: str):
        lines = [f"{prefix}"]
        for label, wall, cpu in self._marks:
            io = wall - cpu
            lines.append(f"  {label:<30s}  wall={wall*1000:6.0f}ms  cpu={cpu*1000:6.0f}ms  io={io*1000:6.0f}ms")
        log.info("\n".join(lines))

# ---------------------------------------------------------------------------
# Route endpoint
# ---------------------------------------------------------------------------

@app.post("/route")
async def route(request: Request):
    t = _T()
    body_bytes = await request.body()
    try:
        body = json.loads(body_bytes)
    except Exception:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(f"{GH}/route", content=body_bytes,
                                  headers={"Content-Type": "application/json"})
        return Response(content=r.content, status_code=r.status_code,
                        media_type="application/json")

    body.setdefault("details", [])
    for d in ("time", "distance"):
        if d not in body["details"]:
            body["details"].append(d)
    body["points_encoded"] = False

    profile  = body.get("profile", "")
    start_ll = body["points"][0]
    end_ll   = body["points"][-1]

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Direct routes
        r = await client.post(f"{GH}/route", content=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
        t.mark(f"GH direct ({profile})")

        if r.status_code != 200:
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")

        data  = r.json()
        paths = data.get("paths", [])
        if not paths:
            return Response(content=r.content, status_code=200,
                            media_type="application/json")

        # Via-charger routes (expressway profile only, single start/end only)
        if profile == "car_motorway" and len(body["points"]) == 2:
            direct_dist = paths[0]["distance"]
            nearby      = _chargers_near_path(paths[0]["points"]["coordinates"])
            t.mark(f"charger spatial filter ({len(nearby)} nearby)")

            charger_paths = await asyncio.gather(
                *[_route_via_charger(client, start_ll, c, end_ll, profile)
                  for c in nearby]
            )
            t.mark(f"GH charger routes ({len(nearby)} requests)")

            added = 0
            for cp in charger_paths:
                if cp is None:
                    continue
                if cp["distance"] <= direct_dist * DETOUR_MAX_RATIO:
                    paths.append(cp)
                    added += 1
                    if added >= MAX_CHARGER_ROUTES:
                        break
            t.mark(f"charger filter ({added} kept)")

    # GPS scoring across all paths (direct + via-charger)
    all_coords = [c for p in paths for c in p["points"]["coordinates"]]
    min_lat = min(c[1] for c in all_coords)
    max_lat = max(c[1] for c in all_coords)
    min_lon = min(c[0] for c in all_coords)
    max_lon = max(c[0] for c in all_coords)

    gps = _load_tiles_for_bbox(min_lat, max_lat, min_lon, max_lon)
    t.mark(f"GPS tile load ({len(paths)} paths)")

    for path in paths:
        if gps is not None:
            scored = _score_path(path, gps[0], gps[1], gps[2])
            if scored is not None:
                path["scored_time_ms"] = scored
    t.mark(f"GPS scoring ({len(paths)} paths)")

    data["paths"] = paths
    t.report(f"route {profile} {start_ll} → {end_ll}")
    return Response(content=json.dumps(data).encode(), status_code=200,
                    media_type="application/json")
