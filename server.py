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

CLUSTER_RADIUS_M    =  3_000  # merge POIs within 3 km into one cluster
DETOUR_MAX_RATIO    =   1.20  # keep via-POI routes up to 20% longer than direct
MAX_CHARGER_ROUTES  =      5  # cap on charger routes returned per request
MAX_CHARGER_GH_REQS =     15  # cap on GH requests fired per query

app = FastAPI()

# ---------------------------------------------------------------------------
# POI data: load + cluster at startup
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))

def _build_clusters(pois: list) -> list:
    """Greedy single-linkage clustering: merge POIs within CLUSTER_RADIUS_M."""
    clusters: list = []
    for poi in pois:
        lat_m = 111_320.0
        lon_m = lat_m * math.cos(math.radians(poi["lat"]))
        for c in clusters:
            dy = (poi["lat"] - c["lat"]) * lat_m
            dx = (poi["lon"] - c["lon"]) * lon_m
            if math.hypot(dy, dx) <= CLUSTER_RADIUS_M:
                c["members"].append(poi)
                n = len(c["members"])
                c["lat"] = sum(m["lat"] for m in c["members"]) / n
                c["lon"] = sum(m["lon"] for m in c["members"]) / n
                break
        else:
            clusters.append({"lat": poi["lat"], "lon": poi["lon"], "members": [poi]})
    return clusters

_CHARGER_FILE = "frontend/flash_chargers.json"
_chargers: list = []
if os.path.exists(_CHARGER_FILE):
    with open(_CHARGER_FILE, encoding="utf-8") as _f:
        _chargers = json.load(_f)
_clusters: list = _build_clusters(_chargers)
log.info(f"Loaded {len(_chargers)} chargers → {len(_clusters)} clusters")

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
            lats   = d["lat"].astype(np.float32)
            lons   = d["lon"].astype(np.float32)
            speeds = d["speed"].astype(np.float32)
            mid_lat = float(tlat) + 0.5
            lat_m   = 111_320.0
            lon_m   = 111_320.0 * math.cos(math.radians(mid_lat))
            tree = cKDTree(np.column_stack([lats * lat_m, lons * lon_m]))
            _tile_cache[key] = (lats, lons, speeds, tree, lat_m, lon_m)
        else:
            _tile_cache[key] = None
    return _tile_cache[key]

# ---------------------------------------------------------------------------
# GPS scoring
# ---------------------------------------------------------------------------

def _score_path(path: dict) -> int | None:
    details_time = path.get("details", {}).get("time", [])
    details_dist = path.get("details", {}).get("distance", [])
    if not details_time or not details_dist:
        return None

    coords = path["points"]["coordinates"]  # [lon, lat]

    total_ms = 0.0
    for (fi, ti, gh_ms), (_, _, dist_m) in zip(details_time, details_dist):
        seg = coords[fi:ti + 1]
        if not seg:
            total_ms += gh_ms
            continue
        mlat = (seg[0][1] + seg[-1][1]) * 0.5
        mlon = (seg[0][0] + seg[-1][0]) * 0.5
        tile = _load_tile(int(math.floor(mlat)), int(math.floor(mlon)))
        if tile is not None:
            _, _, speeds, tree, lat_m, lon_m = tile
            idx = tree.query_ball_point([mlat * lat_m, mlon * lon_m], r=RADIUS_M)
            if len(idx) >= MIN_SAMPLES and dist_m > 0:
                speed_kmh = float(np.median(speeds[idx]))
                total_ms += (dist_m / (speed_kmh / 3.6)) * 1000
                continue
        total_ms += gh_ms

    return round(total_ms)

# ---------------------------------------------------------------------------
# Charger routing helpers
# ---------------------------------------------------------------------------

async def _route_via_cluster(client: httpx.AsyncClient, start_ll: list,
                              cluster: dict, end_ll: list, profile: str) -> dict | None:
    # Route via the cluster member with the smallest straight-line detour
    s_lat, s_lon = start_ll[1], start_ll[0]
    e_lat, e_lon = end_ll[1], end_ll[0]
    best = min(cluster["members"],
               key=lambda m: _haversine_m(s_lat, s_lon, m["lat"], m["lon"])
                           + _haversine_m(e_lat, e_lon, m["lat"], m["lon"]))
    body = {
        "points": [start_ll, [best["lon"], best["lat"]], end_ll],
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
            "name":       best["name"],
            "max_output": best.get("max_output", ""),
            "address":    best.get("address", ""),
            "lat":        best["lat"],
            "lon":        best["lon"],
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
            s_lat, s_lon = start_ll[1], start_ll[0]
            e_lat, e_lon = end_ll[1], end_ll[0]
            d_direct = _haversine_m(s_lat, s_lon, e_lat, e_lon)
            d_max    = d_direct * DETOUR_MAX_RATIO

            ranked = []
            for c in _clusters:
                da = _haversine_m(s_lat, s_lon, c["lat"], c["lon"])
                db = _haversine_m(e_lat, e_lon, c["lat"], c["lon"])
                if da + db <= d_max:
                    ranked.append((da + db - d_direct, c))
            ranked.sort()
            nearby_clusters = [c for _, c in ranked][:MAX_CHARGER_GH_REQS]
            t.mark(f"charger ellipse filter ({len(nearby_clusters)} clusters from {len(_clusters)})")

            charger_paths = await asyncio.gather(
                *[_route_via_cluster(client, start_ll, c, end_ll, profile)
                  for c in nearby_clusters]
            )
            t.mark(f"GH charger routes ({len(nearby_clusters)} requests)")

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
    for path in paths:
        scored = _score_path(path)
        if scored is not None:
            path["scored_time_ms"] = scored
    t.mark(f"GPS scoring ({len(paths)} paths)")

    data["paths"] = paths
    t.report(f"route {profile} {start_ll} → {end_ll}")
    return Response(content=json.dumps(data).encode(), status_code=200,
                    media_type="application/json")
