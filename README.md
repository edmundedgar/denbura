# denbura

EV route planning tool for Japan. Combines OSM routing via Valhalla with GPS-derived speed estimates, elevation-aware EV range calculation, and toll cost estimation.

## Architecture

```
Browser → nginx → frontend (port 2026)   static HTML/JS map UI
               → API server (port 2029)  Python/FastAPI scoring layer
                     ↓
               Valhalla (port 8002)      routing engine (OSM data)
                     +
               planet_gps/tiles/         pre-built GPS speed estimates
               data/dem/                 DEM GeoTIFF tiles (elevation)
               data/toll_ics_auto.json   non-NEXCO toll IC database
```

Three processes must be running. Start them in separate terminals or tmux panes.

---

## Running

### 1. Valhalla (routing engine)

```bash
LD_LIBRARY_PATH=$HOME/.local/lib \
  ~/.local/bin/valhalla_service \
  ~/working/denbura/valhalla/config.json 4
```

Listens on **port 8002**. Reads pre-built tiles from `valhalla/tiles/` (3.3 GB). The `4` argument is the number of worker threads.

**Rebuild tiles** after updating the OSM file (~13 min for Japan on 16 cores):

```bash
LD_LIBRARY_PATH=$HOME/.local/lib \
  ~/.local/bin/valhalla_build_tiles \
  -c ~/working/denbura/valhalla/config.json \
  ~/working/denbura/data/osm/japan-latest.osm.pbf
```

Valhalla binaries are at `~/.local/bin/`, built from source at `~/working/valhalla/` (tag 3.6.0). Requires `LD_LIBRARY_PATH=$HOME/.local/lib`.

Routing profiles:
- `car_motorway` → `auto` costing with `use_highways: 1.0, use_tolls: 1.0`
- `car_local`    → `auto` costing with `use_highways: 0.0, use_tolls: 0.0`

### 2. API server (Python / FastAPI)

```bash
cd ~/working/denbura
source ~/venvs/denbura/bin/activate
uvicorn server:app --host 127.0.0.1 --port 2029
```

Listens on **port 2029**. Streams responses as NDJSON so the frontend can display direct routes immediately while via-POI routes are still computing.

Key responsibilities:

- **Routing**: forwards requests to Valhalla (`alternates: 3`), returns up to 4 routes per profile.
- **Speed scoring**: loads GPS speed tiles from `planet_gps/tiles/` at startup, builds a `cKDTree` per tile. For each maneuver, finds GPS samples within 50 m and uses median speed if ≥ 5 samples exist; otherwise falls back to Valhalla's estimate.
- **EV range estimation**: physics model for BYD Seal AWD (2200 kg, Cd 0.219). Samples elevation from DEM GeoTIFF tiles at maneuver endpoints to compute per-segment altitude change; adds 1.5 kW constant auxiliary load (HVAC, electronics). Reports per-waypoint charge percentage.
- **Toll calculation**: NEXCO rates (¥24.6/km + ¥150 terminal, with 25%/30% long-distance discounts). Shutoko (Tokyo metro, ¥310–¥1,320 cap) and Hanshin (Osaka, ¥630 flat) detected by bounding box.
- **Via-POI routing**: for `car_motorway`, computes charger and onsen alternatives. Clusters POIs at startup, filters by ellipse ratio (≤ 120% detour) and perpendicular distance (≤ 50 km), caps at 8 Valhalla requests per query. Skips chargers whose name begins with `【調整中】` (under maintenance).

### 3. Frontend

```bash
cd ~/working/denbura/frontend
python3 -m http.server 2026 --bind 127.0.0.1
```

Listens on **port 2026**. Serves `frontend/index.html` — a MapLibre GL map featuring:

- Geocoding via Nominatim
- Two routing profiles (expressway in blue, local in orange) displayed simultaneously
- Up to 12 alternative routes, clickable to promote
- Route buttons: `454.0 km · GH 5h53m / GPS 4h50m · ¥8,890`
- Via-POI routes in purple with POI label (`⚡ charger name`, `♨ onsen name`)
- Per-waypoint EV charge percentage shown along routes
- Flash EV charger markers (green) and hot spring markers (orange)
- Search parameters saved to `/#!` URL — reload or share to restore; browser back/forward supported

### nginx

nginx proxies as a system service:

- `talk.edochan.com/map/` → port 2026
- `talk.edochan.com/map/api/` → port 2029

Config: `/etc/nginx/sites-available/talk.edochan.com.conf`

---

## Data setup

### OSM map data

```bash
wget -O data/osm/japan-latest.osm.pbf \
  https://download.geofabrik.de/asia/japan-latest.osm.pbf
# then rebuild Valhalla tiles (see above)
```

Also keep `data/osm/kanto-latest.osm.pbf` for faster tool development/testing.

### DEM elevation tiles

GeoTIFF files covering Japan, stored as `data/dem/N{lat}E{lon}_DEM.tif`. Used by the server to compute altitude change per route maneuver. The `.tif` files are gitignored (large); download with:

```bash
python3 data/dem/download_japan_dem.py
```

### GPS speed tiles

`planet_gps/tiles/` — 1°×1° `.npz` files built from the OSM GPS planet dump. Gitignored (large). Pipeline:

```bash
# Step 1: filter planet dump to Japan (~1–2 h)
python tools/filter_japan_gps.py

# Step 2: compute speeds and tile (~30–60 min)
python tools/compute_speeds.py
```

Source: `planet_gps/gpx-planet-2013-04-09.tar.xz` (20.7 GB, download from planet.openstreetmap.org).

### Toll IC database

Non-NEXCO toll road interchange positions, built from OSM:

```bash
# Test on Kanto (fast, ~2 min)
python tools/build_toll_ic_db.py kanto

# Full Japan run (slow — can take hours, run in background)
python tools/build_toll_ic_db.py japan
```

Outputs:
- `data/toll_ics_auto.json` — auto-generated from OSM (committed; regenerate after OSM updates)
- `data/toll_ics_overrides.json` — hand-edited; a road key here replaces the auto-generated IC list for that road

Detection: finds `toll=yes` motorway ways whose toll-booth nodes carry a non-NEXCO, non-urban-expressway operator. Urban expressways (首都高速, 阪神高速, etc.) are excluded and will be handled separately.

### Charger and onsen data

```bash
# Flash EV chargers (from ev-charger.jp KMZ)
source ~/venvs/denbura/bin/activate
python tools/extract_flash_chargers.py
cp tools/flash_chargers.json frontend/flash_chargers.json

# Hot springs (from various sources)
python tools/fetch_hot_springs.py
```

---

## Python dependencies

```
pip install -r requirements.txt
```

Key packages beyond the standard stack: `fastapi`, `uvicorn`, `httpx`, `numpy`, `scipy`, `tifffile`, `imagecodecs`, `osmium` (pyosmium).
