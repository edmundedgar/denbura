#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

VENV="$HOME/venvs/denbura"

mkdir -p data/osm data/graph-cache data/dem graphhopper tests

if [ ! -d "$VENV" ]; then
    echo "Creating virtualenv at $VENV..."
    python3 -m venv "$VENV"
fi

echo "Installing Python dependencies..."
"$VENV/bin/pip" install --quiet -r requirements.txt

GH_VERSION="11.0"
GH_JAR="graphhopper/graphhopper-web-${GH_VERSION}.jar"
GH_URL="https://github.com/graphhopper/graphhopper/releases/download/${GH_VERSION}/graphhopper-web-${GH_VERSION}.jar"

if [ ! -f "$GH_JAR" ]; then
    echo "Downloading GraphHopper ${GH_VERSION}..."
    wget -q --show-progress -O "$GH_JAR" "$GH_URL"
fi

OSM_FILE="data/osm/kanto-latest.osm.pbf"
OSM_URL="https://download.geofabrik.de/asia/japan/kanto-latest.osm.pbf"

if [ ! -f "$OSM_FILE" ]; then
    echo "Downloading Kanto OSM extract..."
    wget -q --show-progress -O "$OSM_FILE" "$OSM_URL"
fi

DEM_DIR="data/dem"
if [ -z "$(ls "$DEM_DIR"/*.tif 2>/dev/null)" ]; then
    echo "Downloading Copernicus DEM tiles for Japan..."
    "$VENV/bin/python" "$DEM_DIR/download_japan_dem.py"
fi

echo "Setup complete. Activate with: source $VENV/bin/activate"
