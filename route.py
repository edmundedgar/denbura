#!/usr/bin/env python3
"""Query a route from GraphHopper and print a summary."""

import argparse
import sys
import requests

GH_URL = "http://localhost:2027"

# Mashiko, Tochigi
DEFAULT_FROM = "36.476,140.089"


def parse_point(s):
    try:
        lat, lon = s.split(",")
        return float(lat), float(lon)
    except ValueError:
        raise argparse.ArgumentTypeError(f"Expected lat,lon — got: {s!r}")


def write_kml(path, origin, destination, filename):
    coords = " ".join(
        f"{lon},{lat},0"
        for lon, lat in path["points"]["coordinates"]
    )
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Route</name>
    <Placemark><name>Start</name><Point><coordinates>{origin[1]},{origin[0]},0</coordinates></Point></Placemark>
    <Placemark><name>End</name><Point><coordinates>{destination[1]},{destination[0]},0</coordinates></Point></Placemark>
    <Placemark>
      <name>Route</name>
      <LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString>
    </Placemark>
  </Document>
</kml>"""
    with open(filename, "w") as f:
        f.write(kml)
    print(f"KML written to {filename}")


def main():
    parser = argparse.ArgumentParser(description="Get a route from GraphHopper")
    parser.add_argument("--from", dest="origin", type=parse_point,
                        default=DEFAULT_FROM, metavar="lat,lon",
                        help=f"Origin (default: Mashiko {DEFAULT_FROM})")
    parser.add_argument("--to", dest="destination", type=parse_point,
                        required=True, metavar="lat,lon",
                        help="Destination")
    parser.add_argument("--profile", default="car",
                        help="Routing profile (default: car)")
    parser.add_argument("--url", default=GH_URL,
                        help=f"GraphHopper base URL (default: {GH_URL})")
    parser.add_argument("--kml", metavar="FILE",
                        help="Export route as KML for Google My Maps")
    args = parser.parse_args()

    try:
        r = requests.get(f"{args.url}/route", params={
            "point": [
                f"{args.origin[0]},{args.origin[1]}",
                f"{args.destination[0]},{args.destination[1]}",
            ],
            "profile": args.profile,
            "points_encoded": False,
        })
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(f"error: could not connect to GraphHopper at {args.url}", file=sys.stderr)
        sys.exit(1)

    path = r.json()["paths"][0]
    distance_km = path["distance"] / 1000
    minutes = path["time"] // 60000
    hours, mins = divmod(minutes, 60)
    n_points = len(path["points"]["coordinates"])

    print(f"Distance : {distance_km:.1f} km")
    print(f"Time     : {hours}h {mins:02d}m" if hours else f"Time     : {mins}m")
    print(f"Points   : {n_points}")

    if not args.kml:
        print()
        for lon, lat in path["points"]["coordinates"]:
            print(f"{lat},{lon}")
    else:
        write_kml(path, args.origin, args.destination, args.kml)


if __name__ == "__main__":
    main()
