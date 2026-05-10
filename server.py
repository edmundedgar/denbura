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
from scipy.spatial import cKDTree

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("denbura")

VALHALLA   = "http://localhost:8002"
TILE_DIR   = "planet_gps/tiles"
RADIUS_M    = 50
MIN_SAMPLES =  5

CLUSTER_RADIUS_M =  3_000
DETOUR_MAX_RATIO =   1.20
MAX_POI_PERP_M   = 50_000
MAX_POI_ROUTES   =      5
MAX_POI_REQS     =     10

# Map our profile names to Valhalla auto costing options
_PROFILE_OPTS = {
    "car_motorway": {"use_highways": 1.0, "use_tolls": 1.0},
    "car_local":    {"use_highways": 0.0, "use_tolls": 0.0},
}

# ---------------------------------------------------------------------------
# Toll calculation constants (unchanged)
# ---------------------------------------------------------------------------

_NEXCO_TERMINAL = 150
_NEXCO_TIERS = [
    (  0, 0.00),   #   0–100 km: base rate
    (100, 0.25),   # 100–200 km: 25% off
    (200, 0.30),   # 200 km+:    30% off
]
_NEXCO_BASE_KM  = 24.6

_SHUTOKO_MIN    =  310
_SHUTOKO_MAX    = 1320
_SHUTOKO_PER_KM = 29.1

_HANSHIN_FLAT   =  630

_SHUTOKO_BBOX = (35.50, 35.90, 139.40, 139.90)
_HANSHIN_BBOX = (34.50, 34.90, 135.20, 135.70)

# ---------------------------------------------------------------------------
# EV physics model — BYD Seal AWD (Japan WLTC: 82.5 kWh, 575 km, 14.4 kWh/100km)
# ---------------------------------------------------------------------------

_EV_MASS_KG    = 2200.0
_EV_CD         = 0.219
_EV_FRONTAL_M2 = 2.73
_EV_CRR        = 0.009
_EV_ETA_DRIVE  = 0.90   # drivetrain efficiency (motor → wheels)
_EV_ETA_REGEN  = 0.65   # regenerative braking efficiency (wheels → battery)
_EV_BATTERY_KWH = 82.5
_AIR_DENSITY   = 1.225  # kg/m³ at sea level, 15 °C
_GRAVITY       = 9.81   # m/s²


def _toll_network(lat: float, lon: float) -> str:
    if (_SHUTOKO_BBOX[0] <= lat <= _SHUTOKO_BBOX[1] and
            _SHUTOKO_BBOX[2] <= lon <= _SHUTOKO_BBOX[3]):
        return "shutoko"
    if (_HANSHIN_BBOX[0] <= lat <= _HANSHIN_BBOX[1] and
            _HANSHIN_BBOX[2] <= lon <= _HANSHIN_BBOX[3]):
        return "hanshin"
    return "nexco"

# ---------------------------------------------------------------------------
# Valhalla polyline decoder
# ---------------------------------------------------------------------------

def _decode_polyline(encoded: str, precision: int = 6) -> list:
    """Decode Valhalla encoded polyline → [[lat, lon], ...]"""
    factor = 10 ** precision
    coords = []
    lat = lng = 0
    i = 0
    n = len(encoded)
    while i < n:
        for coord_idx in range(2):
            shift = result = 0
            while True:
                b = ord(encoded[i]) - 63
                i += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if coord_idx == 0:
                lat += delta
            else:
                lng += delta
        coords.append([lat / factor, lng / factor])
    return coords

# ---------------------------------------------------------------------------
# GPS tile cache
# ---------------------------------------------------------------------------

_tile_cache: dict = {}


def _load_tile(tlat: int, tlon: int):
    key = (tlat, tlon)
    if key not in _tile_cache:
        path = os.path.join(TILE_DIR, f"{tlat:+04d}_{tlon:+04d}.npz")
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
# GPS scoring — per Valhalla leg
# ---------------------------------------------------------------------------

def _score_leg(leg: dict, coords: list) -> int:
    """GPS-score one Valhalla leg. coords = pre-decoded [[lat, lon], ...].
    Returns milliseconds (mixing GPS speed where available, GH time otherwise)."""
    total_ms = 0.0
    for m in leg.get("maneuvers", []):
        dist_m = m["length"] * 1000
        gh_ms  = m["time"]  * 1000
        mid_i  = (m["begin_shape_index"] + m["end_shape_index"]) // 2
        if mid_i < len(coords):
            mlat, mlon = coords[mid_i]
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
# Toll calculation — per Valhalla leg
# ---------------------------------------------------------------------------

def _calc_toll_leg(leg: dict, coords: list) -> int | None:
    """Calculate toll in yen from Valhalla leg maneuvers."""
    maneuvers = leg.get("maneuvers", [])
    if not any(m.get("toll") for m in maneuvers):
        return None

    nexco_m = shutoko_m = hanshin_m = 0.0
    nexco_entries = 0
    shutoko_seen = hanshin_seen = False
    prev_network = None

    for m in maneuvers:
        if not m.get("toll"):
            prev_network = None
            continue
        dist_m = m["length"] * 1000
        mid_i  = (m["begin_shape_index"] + m["end_shape_index"]) // 2
        if mid_i >= len(coords):
            mid_i = m["begin_shape_index"]
        mlat, mlon = coords[mid_i]
        network = _toll_network(mlat, mlon)

        if network == "nexco":
            if prev_network != "nexco":
                nexco_entries += 1
            nexco_m += dist_m
        elif network == "shutoko":
            shutoko_m += dist_m
            shutoko_seen = True
        elif network == "hanshin":
            hanshin_m += dist_m
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

# ---------------------------------------------------------------------------
# EV energy helpers
# ---------------------------------------------------------------------------

def _energy_joules(dist_m: float, speed_ms: float, dh_m: float) -> float:
    F_roll = _EV_CRR * _EV_MASS_KG * _GRAVITY
    F_aero = 0.5 * _AIR_DENSITY * _EV_CD * _EV_FRONTAL_M2 * speed_ms ** 2
    E_level = (F_roll + F_aero) * dist_m / _EV_ETA_DRIVE
    E_grav  = (_EV_MASS_KG * _GRAVITY * dh_m / _EV_ETA_DRIVE if dh_m >= 0
               else _EV_MASS_KG * _GRAVITY * dh_m * _EV_ETA_REGEN)
    return E_level + E_grav


def _leg_consumed_kwh(leg: dict, coords_ll: list) -> list:
    """Cumulative kWh consumed per shape point (no elevation — DEM not yet configured)."""
    n = len(coords_ll)
    if n == 0:
        return []
    seg_speed_ms = [0.0] * max(n - 1, 0)
    for m in leg.get("maneuvers", []):
        si, ei   = m["begin_shape_index"], m["end_shape_index"]
        dist_m   = m["length"] * 1000
        time_s   = m["time"]
        speed_ms = dist_m / time_s if dist_m > 0 and time_s > 0 else 0.0
        mid_i = (si + ei) // 2
        if mid_i < n:
            mlat, mlon = coords_ll[mid_i]
            tile = _load_tile(int(math.floor(mlat)), int(math.floor(mlon)))
            if tile is not None:
                _, _, speeds, tree, lat_m, lon_m = tile
                idx = tree.query_ball_point([mlat * lat_m, mlon * lon_m], r=RADIUS_M)
                if len(idx) >= MIN_SAMPLES:
                    speed_ms = float(np.median(speeds[idx])) / 3.6
        for j in range(si, min(ei, n - 1)):
            seg_speed_ms[j] = speed_ms

    result = [0.0]
    for j in range(n - 1):
        lat1, lon1 = coords_ll[j]
        lat2, lon2 = coords_ll[j + 1]
        # _haversine_m is defined in the FastAPI section below; Python resolves at call time
        seg_m = _haversine_m(lat1, lon1, lat2, lon2)
        s = seg_speed_ms[j]
        kwh = _energy_joules(seg_m, s, 0.0) / 3_600_000 if s > 0 else 0.0
        result.append(result[-1] + kwh)
    return result

# ---------------------------------------------------------------------------
# Convert a Valhalla trip → our path dict (GH-compatible format for frontend)
# ---------------------------------------------------------------------------

def _trip_to_path(trip: dict, via_poi: dict | None = None) -> dict:
    """Convert a single-leg Valhalla trip to a frontend-compatible path dict."""
    leg    = trip["legs"][0]
    coords = _decode_polyline(leg["shape"])          # [[lat, lon], ...]
    lonlat = [[c[1], c[0]] for c in coords]          # GeoJSON [lon, lat]

    path = {
        "points":         {"type": "LineString", "coordinates": lonlat},
        "distance":       trip["summary"]["length"] * 1000,
        "time":           round(trip["summary"]["time"] * 1000),
        "scored_time_ms": _score_leg(leg, coords),
        "consumed_kwh":   [round(v, 3) for v in _leg_consumed_kwh(leg, coords)],
    }
    toll = _calc_toll_leg(leg, coords)
    if toll is not None:
        path["toll_jpy"] = toll
    if via_poi is not None:
        path["via_poi"] = via_poi
    return path

# ---------------------------------------------------------------------------
# FastAPI app + POI data
# ---------------------------------------------------------------------------

app = FastAPI()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _build_clusters(pois: list) -> list:
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
# Via-POI routing
# ---------------------------------------------------------------------------

async def _route_via_cluster(client: httpx.AsyncClient, start_ll: list,
                              poi_type: str, cluster: dict,
                              end_ll: list, costing_opts: dict) -> dict | None:
    s_lat, s_lon = start_ll[1], start_ll[0]
    e_lat, e_lon = end_ll[1], end_ll[0]
    best = min(cluster["members"],
               key=lambda m: _haversine_m(s_lat, s_lon, m["lat"], m["lon"])
                           + _haversine_m(e_lat, e_lon, m["lat"], m["lon"]))
    body = {
        "locations": [
            {"lat": s_lat,      "lon": s_lon},
            {"lat": best["lat"], "lon": best["lon"]},
            {"lat": e_lat,      "lon": e_lon},
        ],
        "costing": "auto",
        "costing_options": {"auto": costing_opts},
    }
    try:
        r = await client.post(f"{VALHALLA}/route",
                              content=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            return None
        trip = r.json().get("trip")
        if not trip:
            return None

        # Combine both legs (A→POI and POI→B) into one path
        all_lonlat: list      = []
        all_consumed_kwh: list = []
        total_time_ms = total_dist_m = total_scored_ms = total_toll = 0

        for i, leg in enumerate(trip["legs"]):
            coords = _decode_polyline(leg["shape"])
            lonlat = [[c[1], c[0]] for c in coords]
            kwh    = _leg_consumed_kwh(leg, coords)
            if i > 0:
                lonlat = lonlat[1:]   # skip duplicate junction point
                offset = all_consumed_kwh[-1] if all_consumed_kwh else 0.0
                kwh = [offset + v for v in kwh[1:]]
            all_lonlat.extend(lonlat)
            all_consumed_kwh.extend(kwh)
            total_time_ms    += round(leg["summary"]["time"]   * 1000)
            total_dist_m     += leg["summary"]["length"] * 1000
            total_scored_ms  += _score_leg(leg, coords)
            toll = _calc_toll_leg(leg, coords)
            if toll:
                total_toll += toll

        via_poi = {
            "type":          poi_type,
            "name":          best["name"],
            "lat":           best["lat"],
            "lon":           best["lon"],
            "max_output":    best.get("max_output", ""),
            "address":       best.get("address", ""),
            "opening_hours": best.get("opening_hours", ""),
            "fee":           best.get("fee", ""),
        }
        path = {
            "points":         {"type": "LineString", "coordinates": all_lonlat},
            "distance":       total_dist_m,
            "time":           total_time_ms,
            "scored_time_ms": total_scored_ms,
            "via_poi":        via_poi,
            "consumed_kwh":   [round(v, 3) for v in all_consumed_kwh],
        }
        if total_toll:
            path["toll_jpy"] = total_toll
        return path
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------

class _T:
    def __init__(self):
        self._wall0 = time.perf_counter()
        self._cpu0  = time.process_time()
        self._marks: list = []

    def mark(self, label: str):
        wall = time.perf_counter() - self._wall0
        cpu  = time.process_time()  - self._cpu0
        self._marks.append((label, wall, cpu))
        self._wall0 = time.perf_counter()
        self._cpu0  = time.process_time()

    def report(self, prefix: str):
        lines = [prefix]
        for label, wall, cpu in self._marks:
            lines.append(f"  {label:<40s}  wall={wall*1000:6.0f}ms  cpu={cpu*1000:6.0f}ms")
        log.info("\n".join(lines))

# ---------------------------------------------------------------------------
# Route endpoint
# ---------------------------------------------------------------------------

@app.post("/route")
async def route(request: Request):
    t = _T()
    body = json.loads(await request.body())

    profile    = body.get("profile", "car_motorway")
    start_ll   = body["points"][0]   # [lon, lat]
    end_ll     = body["points"][-1]  # [lon, lat]
    s_lat, s_lon = start_ll[1], start_ll[0]
    e_lat, e_lon = end_ll[1],   end_ll[0]

    costing_opts = _PROFILE_OPTS.get(profile, _PROFILE_OPTS["car_motorway"])

    valhalla_body = {
        "locations": [
            {"lat": s_lat, "lon": s_lon},
            {"lat": e_lat, "lon": e_lon},
        ],
        "costing": "auto",
        "costing_options": {"auto": costing_opts},
        "alternates": 3,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{VALHALLA}/route",
                              content=json.dumps(valhalla_body).encode(),
                              headers={"Content-Type": "application/json"})
        t.mark(f"Valhalla direct ({profile})")

        if r.status_code != 200:
            return Response(content=r.content, status_code=r.status_code,
                            media_type="application/json")

        data = r.json()
        main_trip = data.get("trip")
        if not main_trip:
            return Response(content=r.content, status_code=200,
                            media_type="application/json")

        # Collect all trips: main + alternates
        all_trips = [main_trip] + [a["trip"] for a in data.get("alternates", [])]
        paths = [_trip_to_path(trip) for trip in all_trips]
        t.mark(f"GPS scoring + toll ({len(paths)} paths)")

        # Via-POI routes (motorway profile, single A→B only)
        if profile == "car_motorway" and len(body["points"]) == 2:
            direct_dist = paths[0]["distance"]
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
                        perp  = da * math.sqrt(max(0.0, 1 - max(-1.0, min(1.0, cos_a))**2))
                        if perp > MAX_POI_PERP_M:
                            continue
                    ranked.append((da + db - d_direct, layer["type"], c))
            ranked.sort()
            nearby = ranked[:MAX_POI_REQS]
            total_clusters = sum(len(l["clusters"]) for l in _poi_layers)
            t.mark(f"POI ellipse filter ({len(ranked)} → {len(nearby)} from {total_clusters} clusters)")

            poi_paths = await asyncio.gather(
                *[_route_via_cluster(client, start_ll, poi_type, c, end_ll, costing_opts)
                  for _, poi_type, c in nearby]
            )
            t.mark(f"Valhalla POI routes ({len(nearby)} requests)")

            added = 0
            for pp in poi_paths:
                if pp is None:
                    continue
                if pp["distance"] <= direct_dist * DETOUR_MAX_RATIO:
                    paths.append(pp)
                    added += 1
                    if added >= MAX_POI_ROUTES:
                        break
            t.mark(f"POI filter ({added} kept)")

    t.report(f"route {profile} {start_ll} → {end_ll}")
    return Response(content=json.dumps({"paths": paths}).encode(),
                    status_code=200, media_type="application/json")
