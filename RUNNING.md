# Running denbura

## Architecture overview

```
Browser → nginx → frontend (port 2026)   static HTML/JS map UI
               → API server (port 2029)  Python/FastAPI scoring layer
                     ↓
               Valhalla (port 8002)      routing engine (OSM data)
                     +
               planet_gps/tiles/         pre-built GPS speed estimates
```

Three processes must be running. Start them in separate terminals (or tmux panes).

---

## 1. Valhalla (routing engine)

```bash
LD_LIBRARY_PATH=$HOME/.local/lib \
  ~/.local/bin/valhalla_service \
  ~/working/denbura/valhalla/config.json 4
```

Listens on **port 8002**. Reads pre-built tiles from `valhalla/tiles/` (3.3 GB).
The `4` argument is the number of worker processes — increase for heavier load.

**Tile build** (one-off, ~13 min for Japan on 16 cores):
```bash
LD_LIBRARY_PATH=$HOME/.local/lib \
  ~/.local/bin/valhalla_build_tiles \
  -c ~/working/denbura/valhalla/config.json \
  ~/working/denbura/data/osm/japan-latest.osm.pbf
```

Rebuild after updating the OSM file. Config is at `valhalla/config.json`.

**Binaries** are installed to `~/.local/bin/`. Built from source at
`~/working/valhalla/` (tag 3.6.0). Requires `LD_LIBRARY_PATH=$HOME/.local/lib`
because prime_server and Valhalla itself are installed to `~/.local/lib`.

**Profiles** are mapped in `server.py`:
- `car_motorway` → `auto` costing with `use_highways: 1.0, use_tolls: 1.0`
- `car_local`    → `auto` costing with `use_highways: 0.0, use_tolls: 0.0`

---

## 2. API server (Python / FastAPI)

```bash
cd ~/working/denbura
source ~/venvs/denbura/bin/activate
uvicorn server:app --host 127.0.0.1 --port 2029
```

Listens on **port 2029**. Sits between the frontend and Valhalla:

- Forwards route requests to Valhalla (`alternates: 3` for up to 4 routes per profile).
- Loads GPS speed tiles from `planet_gps/tiles/` (1°×1° `.npz` files) at startup,
  builds a scipy `cKDTree` per tile, and caches both in memory. For each route
  maneuver, finds GPS trace speed samples within 50 m and computes a median speed.
  If ≥5 samples exist, uses GPS speed to compute `scored_time_ms`; otherwise falls
  back to Valhalla's estimate.
- Calculates expressway toll cost (`toll_jpy`) from Valhalla maneuvers flagged
  `toll: true`. Applies NEXCO rates (¥24.6/km + ¥150 terminal charge per entry,
  普通車) with long-distance discounts (25% off 100–200 km, 30% off 200 km+).
  Uses a distance-capped formula for the Shutoko (Tokyo metro expressway,
  min ¥310 / max ¥1,320) and a flat approximation for Hanshin (Osaka).
  Urban networks are identified by geographic bounding box.
- For `car_motorway` routes, adds via-POI alternatives (chargers, hot springs).
  Clusters POIs at startup, filters candidates with an ellipse pre-filter
  (A→POI→B ≤ 120% of direct distance) plus a 50 km perpendicular-distance cap,
  and caps Valhalla requests at 10 per query.
- Returns a `{"paths": [...]}` response with `scored_time_ms`, `toll_jpy`, and
  `via_poi` added to each path.

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
- Route buttons show Valhalla time, GPS-estimated time, and expressway toll:
  `454.0 km · GH 5h53m / GPS 4h50m · ¥8,890`
- Via-POI routes shown in purple with POI name in the label (⚡ chargers, ♨ hot springs)
- Flash EV charger locations shown as green markers; hot springs as orange markers

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

## Flash EV charger data

`frontend/flash_chargers.json` — charger locations fetched from the Google My Maps
embedded on `ev-charger.jp/area/`. Displayed as green markers on the map with a
popup showing name, address, max output, hours, and connector types.

Refresh with:

```bash
cd ~/working/denbura
source ~/venvs/denbura/bin/activate
python tools/extract_flash_chargers.py
cp flash_chargers.json frontend/flash_chargers.json
```

The script fetches the KMZ from the embedded map (one request, ~40 KB) and parses
coordinates and metadata directly from it. No geocoding needed.

---

## OSM map data

`data/osm/japan-latest.osm.pbf` — full Japan extract from Geofabrik.
Update with:

```bash
wget -O data/osm/japan-latest.osm.pbf https://download.geofabrik.de/asia/japan-latest.osm.pbf
# then rebuild Valhalla tiles (see above)
```
