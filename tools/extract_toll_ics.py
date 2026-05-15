#!/usr/bin/env python3
"""Extract IC positions for known non-NEXCO toll roads from OSM PBF.

Single-pass approach: use locations=True so we get node coordinates
while processing ways in the same scan.
"""
import math
import osmium

PBF = "data/osm/kanto-latest.osm.pbf"

TARGETS = [
    {"refs": ["E81"], "names": ["日光宇都宮道路"]},
]

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))

class TollRoadHandler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        # target_road_name → list of (lat, lon, node_tags) for junction nodes
        self.junctions = {}    # road_name → [(lat, lon, name, ref), ...]
        self.way_points = {}   # road_name → [(lat, lon), ...] all points for ordering

    def _matches(self, tags):
        ref  = tags.get("ref", "")
        name = tags.get("name", "")
        for t in TARGETS:
            if any(r in ref for r in t["refs"]) or any(n in name for n in t["names"]):
                return t["names"][0]
        return None

    def way(self, w):
        tags = dict(w.tags)
        road_name = self._matches(tags)
        if not road_name:
            return
        if tags.get("highway") not in ("motorway", "motorway_link"):
            return

        pts = []
        jcts = []
        for n in w.nodes:
            if not n.location.valid():
                continue
            lat, lon = n.location.lat, n.location.lon
            pts.append((lat, lon))
            ntags = dict(n.tags) if hasattr(n, 'tags') else {}
            nname = ntags.get("name", "")
            nref  = ntags.get("ref", "")
            nhwy  = ntags.get("highway", "")
            if nhwy == "motorway_junction" or "IC" in nname or "料金所" in nname:
                jcts.append((lat, lon, nname, nref))

        if road_name not in self.way_points:
            self.way_points[road_name] = []
            self.junctions[road_name]  = []
        self.way_points[road_name].extend(pts)
        self.junctions[road_name].extend(jcts)

print("Scanning PBF (single pass with locations)...", flush=True)
h = TollRoadHandler()
h.apply_file(PBF, locations=True)
print("Done.", flush=True)

for road_name, jcts in h.junctions.items():
    pts = h.way_points[road_name]
    print(f"\nRoad: {road_name}  ({len(pts)} shape points, {len(jcts)} junctions)")

    # Deduplicate junctions by proximity (within 200m = same IC)
    deduped = []
    for j in jcts:
        lat, lon, name, ref = j
        dup = False
        for d in deduped:
            if haversine_m(lat, lon, d[0], d[1]) < 200:
                dup = True
                break
        if not dup:
            deduped.append(j)

    # Sort by latitude (rough north-south ordering for E81)
    deduped.sort(key=lambda x: x[0])
    print(f"  Deduplicated junctions: {len(deduped)}")
    for lat, lon, name, ref in deduped:
        print(f"    ({lat:.5f}, {lon:.5f})  name={name!r}  ref={ref!r}")
