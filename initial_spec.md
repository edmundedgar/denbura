# EV Route Planner — Product Specification

## Overview

A personal EV route planner optimised for driving in Japan, addressing gaps in existing tools: intelligent toll cost modelling, altitude-aware power consumption, charging network cost awareness, and preference-based POI suggestions.

---

## Goals

- Plan routes that minimise cost (tolls + charging) rather than just time
- Accurately model range based on altitude change and EV consumption profile
- Surface relevant POIs along the route based on user-defined preferences
- Run self-hosted on FOSS infrastructure

---

## Target User (MVP)

Single user (personal tool). Multi-user support deferred.

---

## Tech Stack

| Component | Technology |
|---|---|
| Map display | MapLibre GL JS |
| Vector tiles | Stadia Maps (hosted) or self-hosted PMTiles via Tilemaker |
| Map data | OpenStreetMap (Japan extract via Geofabrik) |
| Routing engine | GraphHopper (self-hosted, Docker) |
| Elevation data | Copernicus DEM or SRTM |
| Backend API | FastAPI (Python) |
| Database | PostgreSQL + PostGIS |
| Deployment | Docker Compose |

---

## Vehicle

**BYD Seal**

Consumption modelling should use BYD Seal real-world efficiency curves, adjusted for:
- Speed (highway vs urban)
- Altitude gain/loss (regen on descent, increased draw on ascent)
- Segment gradient as derived from DEM data overlaid on route geometry

---

## Core Features

### 1. Route Planning

- Origin and destination input (address or map click)
- Route calculated via GraphHopper with custom cost model (see Toll Logic)
- Altitude profile displayed alongside route
- Estimated energy consumption per segment and total
- Estimated remaining range at each waypoint

### 2. Charging Stop Planning

**Charging networks, ranked by preference:**

| Tier | Networks | Notes |
|---|---|---|
| Preferred | Flash, Terra Charge | Lower cost; route should favour these |
| Acceptable | Others | Used if no preferred option available within range |

Routing logic should:
- Ensure charge stops keep battery above a minimum threshold (configurable, default 15%)
- Prefer Flash/Terra Charge even if they require a detour
- Calculate actual charging cost per stop based on network pricing
- Display total trip charging cost

MVP: Flash and Terra Charge data loaded from scraped/API source into PostGIS. Other networks deferred.

### 3. Toll Logic

Tolls are modelled as a cost in JPY per minute of time saved compared to the toll-free alternative.

**Parameters:**
- `base_rate`: JPY per minute saved (user-configurable)
- `fatigue_multiplier`: increases `base_rate` as a function of total trip moving time
- Expressway is taken when: `toll_cost ÷ time_saved_minutes < base_rate × fatigue_multiplier`

**Fatigue multiplier:**
- Based on total trip moving time (stopped time excluded)
- Multiplier increases on a curve — e.g. flat for first 2 hours, then rising
- Exact curve configurable; makes long trips progressively more willing to use expressway

**UI:** User sets base rate (¥/min) as a slider. Fatigue curve shown as a simple graph.

### 4. POI Suggestions

- User defines a preference profile in natural language
  - e.g. "independent kissaten, Showa-era architecture, rural onsen, covered shopping arcades"
- On route planning, POIs are surfaced proactively along the route within a configurable corridor (default: 5km either side)
- POI data sourced from OpenStreetMap; preference matching via LLM embedding or keyword tagging (TBD)
- User can approve or dismiss suggestions; approved stops added as waypoints
- Adding a POI stop recalculates route, toll logic, and charging plan

---

## UI

### Map View
- Full-screen map (MapLibre)
- Route displayed with colour coding by segment (e.g. energy intensity or road type)
- Charging stops marked with network logo and cost
- Toll sections highlighted
- POI suggestions shown as dismissable markers

### Route Summary Panel
- Total distance, moving time, estimated trip time
- Total energy used (kWh), estimated cost
- Total toll cost
- Total charging cost
- Per-stop breakdown (charge time, cost, network)

### Settings
- Base toll rate (¥/min slider)
- Fatigue multiplier curve
- Minimum battery threshold
- Charging network preferences
- POI preference profile (free text)
- Preferred charging corridor width

---

## Data

### Charging Network Data
- Loaded into PostGIS: location, network, charger speed (kW), pricing model
- MVP: Flash and Terra Charge only
- Refresh strategy: manual or scheduled scrape (TBD)

### Elevation
- Copernicus DEM (30m resolution) ingested and queryable via PostGIS raster or a Python elevation service
- Elevation profile computed per route segment from GraphHopper geometry

### OSM Data
- Japan extract from Geofabrik (`japan-latest.osm.pbf`)
- Fed into GraphHopper for routing graph
- Fed into Tilemaker for vector tile generation
- Updated via Geofabrik daily diffs

---

## Out of Scope (MVP)

- Multi-user support
- Mobile app (web-first; responsive layout considered but not prioritised)
- Non-Japan routing
- Real-time traffic
- Other charging networks beyond Flash and Terra Charge
- OSM contribution pipeline (separate sub-project)

---

## Future Considerations

- Mobile app (React Native or PWA)
- Real-time charger availability (if APIs available)
- Trip history and consumption analytics
- OSM upload tool for charging locations
- Share/export route
- Multi-user with preference profiles
