#!/usr/bin/env python3
"""
Extract Flash EV charger locations from the Google My Maps KMZ embedded on ev-charger.jp/area/
Outputs flash_chargers.json with lat, lon, and metadata parsed from the KML descriptions.

Install dependencies: pip install requests
"""

import io
import json
import re
import zipfile
from xml.etree import ElementTree as ET

import requests

KMZ_URL = "https://www.google.com/maps/d/kml?mid=1jAcy9on69GG3pZxOFcUAYB_sPJu8RzA"
KML_NS  = "{http://www.opengis.net/kml/2.2}"
OUTPUT  = "flash_chargers.json"


def parse_description(html: str) -> dict:
    text = re.sub(r'<br\s*/?>', '\n', html)
    text = re.sub(r'<[^>]+>', '', text)

    info = {}

    m = re.search(r'最大出力[：:]\s*([^\n]+)', text)
    if m:
        info['max_output'] = m.group(1).strip()

    m = re.search(r'営業時間[^：:\n]*[：:]\s*([^\n]+)', text)
    if m:
        info['hours'] = m.group(1).strip()

    m = re.search(r'定休日[：:]\s*([^\n]+)', text)
    if m:
        info['closed'] = m.group(1).strip()

    m = re.search(r'運営会社[：:]\s*([^\n]+)', text)
    if m:
        info['operator'] = m.group(1).strip()

    for line in text.splitlines():
        line = line.strip()
        if re.search(r'[都道府県]', line) and len(line) > 5:
            info['address'] = line
            break

    connectors = []
    if 'CHAdeMO' in text:
        connectors.append('CHAdeMO')
    if 'NACS' in text or 'テスラ' in text:
        connectors.append('NACS')
    if connectors:
        info['connector_types'] = connectors

    if '調整中' in text:
        info['status'] = 'under_adjustment'

    return info


def main():
    print(f"Fetching KMZ from {KMZ_URL} ...")
    r = requests.get(KMZ_URL, timeout=30)
    r.raise_for_status()
    print(f"Downloaded {len(r.content):,} bytes")

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        kml_bytes = z.read("doc.kml")

    root = ET.fromstring(kml_bytes)

    locations = []
    for pm in root.iter(f"{KML_NS}Placemark"):
        name_el   = pm.find(f"{KML_NS}name")
        desc_el   = pm.find(f"{KML_NS}description")
        coords_el = pm.find(f".//{KML_NS}coordinates")

        if coords_el is None:
            continue

        lon, lat, *_ = [float(v) for v in coords_el.text.strip().split(",")]
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

        loc = {"name": name, "lat": lat, "lon": lon}
        loc.update(parse_description(desc))
        locations.append(loc)

    print(f"Parsed {len(locations)} charger locations")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(locations, f, ensure_ascii=False, indent=2)
    print(f"Saved to {OUTPUT}")

    print("\nSample:")
    print(json.dumps(locations[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
