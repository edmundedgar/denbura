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
MAX_POI_PERP_M      = 50_000  # max perpendicular distance from direct route line
MAX_CHARGER_ROUTES  =      5  # cap on charger routes returned per request
MAX_CHARGER_GH_REQS =     10  # cap on GH requests fired per query

# ---------------------------------------------------------------------------
# Toll calculation constants
# ---------------------------------------------------------------------------

# NEXCO (most expressways outside Tokyo/Osaka): distance-based with long-distance tiers
# 長距離逓減割引, 普通車
_NEXCO_TERMINAL  = 150    # ¥ fixed entry charge per expressway entry
# (distance_km_threshold, discount_fraction) — applied to the portion within each band
_NEXCO_TIERS = [
    (  0, 0.00),   #   0–100 km: base rate
    (100, 0.25),   # 100–200 km: 25% off
    (200, 0.30),   # 200 km+:    30% off
]
_NEXCO_BASE_KM   = 24.6   # ¥/km before discount

# Shutoko (首都高) — Tokyo metropolitan expressway: distance-based with cap
_SHUTOKO_MIN     =  310   # ¥ minimum, 普通車 ETC
_SHUTOKO_MAX     = 1320   # ¥ maximum (cap), 普通車 ETC
_SHUTOKO_PER_KM  = 29.1   # ¥/km, 普通車

# Hanshin (阪神) — Osaka: simplified flat approximation
_HANSHIN_FLAT    =  630   # ¥ typical single-section fare, 普通車

# Geographic bounding boxes to distinguish urban toll networks from NEXCO
# (lat_min, lat_max, lon_min, lon_max)
_SHUTOKO_BBOX = (35.50, 35.90, 139.40, 139.90)
_HANSHIN_BBOX = (34.50, 34.90, 135.20, 135.70)


def _toll_network(lat: float, lon: float) -> str:
    """Return which toll network a coordinate belongs to."""
    if (_SHUTOKO_BBOX[0] <= lat <= _SHUTOKO_BBOX[1] and
            _SHUTOKO_BBOX[2] <= lon <= _SHUTOKO_BBOX[3]):
        return "shutoko"
    if (_HANSHIN_BBOX[0] <= lat <= _HANSHIN_BBOX[1] and
            _HANSHIN_BBOX[2] <= lon <= _HANSHIN_BBOX[3]):
        return "hanshin"
    return "nexco"


def _calc_toll(path: dict) -> int | None:
    """Return estimated toll in yen for a path, or None if no toll data."""
    details = path.get("details", {})
    toll_segs  = details.get("toll", [])
    dist_segs  = details.get("distance", [])
    rc_segs    = details.get("road_class", [])
    if not toll_segs or not dist_segs or not rc_segs:
        return None

    coords = path["points"]["coordinates"]  # [lon, lat]

    # Build per-coord-pair lookups
    pair_dist  = {fi: dm  for fi, ti, dm  in dist_segs}
    pair_toll  = {}
    for fi, ti, tv in toll_segs:
        for i in range(fi, ti):
            pair_toll[i] = tv
    pair_class = {}
    for fi, ti, cls in rc_segs:
        for i in range(fi, ti):
            pair_class[i] = cls

    # Walk pairs in sequence, tracking network transitions to count entries
    nexco_m       = 0.0
    shutoko_m     = 0.0
    hanshin_m     = 0.0
    nexco_entries = 0
    shutoko_seen  = False
    hanshin_seen  = False
    prev_network  = None   # last tolled network (None = not on toll road)

    for i in range(len(coords) - 1):
        if pair_toll.get(i, "no").lower() != "all":
            prev_network = None
            continue
        cls = pair_class.get(i, "")
        if cls not in ("motorway", "trunk"):
            prev_network = None
            continue
        dm = pair_dist.get(i, 0.0)
        mlon = (coords[i][0] + coords[i+1][0]) * 0.5
        mlat = (coords[i][1] + coords[i+1][1]) * 0.5
        network = _toll_network(mlat, mlon)

        if network == "nexco":
            if prev_network != "nexco":
                nexco_entries += 1
            nexco_m += dm
        elif network == "shutoko":
            shutoko_m += dm
            shutoko_seen = True
        elif network == "hanshin":
            hanshin_m += dm
            hanshin_seen = True

        prev_network = network

    total = 0

    if nexco_m > 0:
        km = nexco_m / 1000
        dist_charge = 0.0
        thresholds = [t for t, _ in _NEXCO_TIERS] + [float("inf")]
        for j, (start_km, discount) in enumerate(_NEXCO_TIERS):
            end_km = thresholds[j + 1]
            portion = max(0.0, min(km, end_km) - start_km)
            dist_charge += portion * _NEXCO_BASE_KM * (1 - discount)
            if km <= end_km:
                break
        raw = _NEXCO_TERMINAL * nexco_entries + dist_charge
        total += math.ceil(raw / 10) * 10

    if shutoko_seen:
        raw = (shutoko_m / 1000) * _SHUTOKO_PER_KM
        total += max(_SHUTOKO_MIN, min(_SHUTOKO_MAX, math.ceil(raw / 10) * 10))

    if hanshin_seen:
        total += _HANSHIN_FLAT

    return total if (nexco_m > 0 or shutoko_seen or hanshin_seen) else None

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

def _load_poi_layer(poi_type: str, filepath: str) -> dict:
    pois = []
    if os.path.exists(filepath):
        with open(filepath, encoding="utf-8") as f:
            pois = json.load(f)
    clusters = _build_clusters(pois)
    log.info(f"Loaded {len(pois)} {poi_type} → {len(clusters)} clusters")
    return {"type": poi_type, "pois": pois, "clusters": clusters}

_poi_layers: list = [
    _load_poi_layer("charger",    "frontend/flash_chargers.json"),
    _load_poi_layer("hot_spring", "frontend/hot_springs.json"),
]

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
                              poi_type: str, cluster: dict,
                              end_ll: list, profile: str) -> dict | None:
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
        "details": ["time", "distance", "road_class", "toll"],
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
        path["via_poi"] = {
            "type":    poi_type,
            "name":    best["name"],
            "lat":     best["lat"],
            "lon":     best["lon"],
            # charger-specific fields (empty for other types)
            "max_output": best.get("max_output", ""),
            "address":    best.get("address", ""),
            # hot-spring-specific fields
            "opening_hours": best.get("opening_hours", ""),
            "fee":           best.get("fee", ""),
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
    for d in ("time", "distance", "road_class", "toll"):
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

        # Via-POI routes (expressway profile only, single start/end only)
        if profile == "car_motorway" and len(body["points"]) == 2:
            direct_dist = paths[0]["distance"]
            s_lat, s_lon = start_ll[1], start_ll[0]
            e_lat, e_lon = end_ll[1], end_ll[0]
            d_direct = _haversine_m(s_lat, s_lon, e_lat, e_lon)
            d_max    = d_direct * DETOUR_MAX_RATIO

            ranked = []
            for layer in _poi_layers:
                for c in layer["clusters"]:
                    da = _haversine_m(s_lat, s_lon, c["lat"], c["lon"])
                    db = _haversine_m(e_lat, e_lon, c["lat"], c["lon"])
                    if da + db > d_max:
                        continue
                    if d_direct > 0:
                        cos_a = (da**2 + d_direct**2 - db**2) / (2 * da * d_direct)
                        perp = da * math.sqrt(max(0.0, 1 - max(-1.0, min(1.0, cos_a))**2))
                        if perp > MAX_POI_PERP_M:
                            continue
                    ranked.append((da + db - d_direct, layer["type"], c))
            ranked.sort()
            nearby = ranked[:MAX_CHARGER_GH_REQS]
            total_clusters = sum(len(l["clusters"]) for l in _poi_layers)
            t.mark(f"POI ellipse filter ({len(ranked)} → {len(nearby)} from {total_clusters} clusters)")

            poi_paths = await asyncio.gather(
                *[_route_via_cluster(client, start_ll, poi_type, c, end_ll, profile)
                  for _, poi_type, c in nearby]
            )
            t.mark(f"GH POI routes ({len(nearby)} requests)")

            added = 0
            for cp in poi_paths:
                if cp is None:
                    continue
                if cp["distance"] <= direct_dist * DETOUR_MAX_RATIO:
                    paths.append(cp)
                    added += 1
                    if added >= MAX_CHARGER_ROUTES:
                        break
            t.mark(f"POI filter ({added} kept)")

    # GPS scoring + toll calculation across all paths
    for path in paths:
        scored = _score_path(path)
        if scored is not None:
            path["scored_time_ms"] = scored
        toll = _calc_toll(path)
        if toll is not None:
            path["toll_jpy"] = toll
    t.mark(f"GPS scoring + toll ({len(paths)} paths)")

    data["paths"] = paths
    t.report(f"route {profile} {start_ll} → {end_ll}")
    return Response(content=json.dumps(data).encode(), status_code=200,
                    media_type="application/json")
