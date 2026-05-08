# Running denbura

## Architecture overview

```
Browser → nginx → frontend (port 2026)   static HTML/JS map UI
               → API server (port 2029)  Python/FastAPI scoring layer
                     ↓
               GraphHopper (port 2027)   routing engine (OSM data)
                     +
               planet_gps/tiles/         pre-built GPS speed estimates
```

Three processes must be running. Start them in separate terminals (or tmux panes).

---

## 1. GraphHopper (routing engine)

```bash
cd ~/working/denbura
java -jar graphhopper/graphhopper-web-11.0.jar server graphhopper/config.yml
```

Listens on **port 2027**. Reads the Japan OSM map (`data/osm/japan-latest.osm.pbf`)
and custom routing profiles from `graphhopper/*.json`.

**Graph cache** (`data/graph-cache/`): built on the first run after the OSM file or any
profile changes (~30–60 min for Japan); subsequent starts load it in seconds.
Delete the cache directory to force a rebuild.

**Profiles** (defined in `graphhopper/config.yml`):
- `car_motorway` — prefers expressways
- `car_local` — avoids expressways, prefers local roads
- `car_extreme` — strongly avoids expressways

Custom model JSON files in `graphhopper/` tune speed and priority per road class.

---

## 2. API server (Python / FastAPI)

```bash
cd ~/working/denbura
source ~/venvs/denbura/bin/activate
uvicorn server:app --host 127.0.0.1 --port 2029
```

Listens on **port 2029**. Sits between the frontend and GraphHopper:

- Forwards route requests to GraphHopper, adding `details: [time, distance]` to get
  per-segment data back.
- Loads GPS speed tiles from `planet_gps/tiles/` (1°×1° `.npz` files) on demand and
  caches them in memory for the lifetime of the process.
- For each route segment, finds GPS trace speed samples within 50 m and computes a
  median speed. If ≥5 samples exist, uses that to compute `scored_time_ms`; otherwise
  falls back to GraphHopper's estimate.
- Returns the original GraphHopper response augmented with `scored_time_ms` on each
  path (the GPS-based time estimate in milliseconds).

Tiles are loaded on first request per region; subsequent requests for the same area
are served from the in-process cache.

---

## 3. Frontend (static file server)

```bash
cd ~/working/denbura/frontend
python3 -m http.server 2026 --bind 127.0.0.1
```

Listens on **port 2026**. Serves `frontend/index.html` — a MapLibre GL map with:

- Geocoding via Nominatim (OSM)
- Two routing profiles shown simultaneously: expressway (blue) and local (orange)
- Up to 12 alternative routes displayed at once, clickable to promote
- Route buttons show both GraphHopper time and GPS-estimated time when available:
  `116.0 km · GH 2h05m / GPS 1h55m`

---

## nginx

nginx is already running as a system service and proxies:

- `talk.edochan.com/map/` → port 2026 (frontend)
- `talk.edochan.com/map/api/` → port 2029 (API server)

Config: `/etc/nginx/sites-available/talk.edochan.com.conf`
Reload after changes: `sudo nginx -s reload`

---

## GPS speed data pipeline (one-off setup)

These scripts built the `planet_gps/tiles/` dataset and do not need to be re-run
unless you want to update the GPS data.

### Source data

`planet_gps/gpx-planet-2013-04-09.tar.xz` — OSM bulk GPS planet dump (20.7 GB).
Download from `https://planet.openstreetmap.org/gps/gpx-planet-2013-04-09.tar.xz`.

### Step 1 — filter to Japan

```bash
python tools/filter_japan_gps.py
```

Streams through the archive, extracts trackpoints within the Japan bounding box
(31–45.5°N, 130–146°E), and writes `planet_gps/japan_gps.csv.gz`
(columns: `track_id, lat, lon, time`). Takes ~1–2 hours.

### Step 2 — compute speeds and tile

```bash
python tools/compute_speeds.py
```

Reads `japan_gps.csv.gz`, groups points by track, computes speed between consecutive
pairs (filtered to 2–180 km/h, gaps ≤ 60 s), and writes one `.npz` file per 1°×1°
tile into `planet_gps/tiles/`. Each tile holds `lat`, `lon`, `hour` (UTC hour of day),
and `speed` (km/h) arrays. Takes ~30–60 minutes.

Once tiles exist the archive and CSV can be deleted to recover disk space.

---

## OSM map data

`data/osm/japan-latest.osm.pbf` — full Japan extract from Geofabrik.
Update with:

```bash
wget -O data/osm/japan-latest.osm.pbf https://download.geofabrik.de/asia/japan-latest.osm.pbf
rm -rf data/graph-cache/   # force GraphHopper to rebuild
```
