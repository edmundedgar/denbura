"""Basic routing smoke test — Mashiko to Utsunomiya via GraphHopper."""

import pytest
import requests

GH_URL = "http://localhost:2027"

# Mashiko, Tochigi (home base)
MASHIKO = (36.476, 140.089)
# Utsunomiya, Tochigi (prefectural capital, ~30km away)
UTSUNOMIYA = (36.555, 139.882)


@pytest.fixture(scope="session", autouse=True)
def graphhopper_health():
    import time
    for _ in range(30):
        try:
            r = requests.get(f"{GH_URL}/health", timeout=2)
            if r.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(2)
    pytest.fail("GraphHopper did not become ready within 60s")


def route(origin, destination, **params):
    r = requests.get(f"{GH_URL}/route", params={
        "point": [f"{origin[0]},{origin[1]}", f"{destination[0]},{destination[1]}"],
        "profile": "car",
        **params,
    })
    r.raise_for_status()
    return r.json()


def test_basic_route_returns_path():
    data = route(MASHIKO, UTSUNOMIYA)
    assert "paths" in data
    assert len(data["paths"]) > 0


def test_basic_route_distance_plausible():
    path = route(MASHIKO, UTSUNOMIYA)["paths"][0]
    distance_km = path["distance"] / 1000
    # Road distance should be between 20km and 60km
    assert 20 < distance_km < 60, f"Unexpected distance: {distance_km:.1f}km"


def test_basic_route_has_geometry():
    path = route(MASHIKO, UTSUNOMIYA, points_encoded=False)["paths"][0]
    assert "points" in path
    coords = path["points"]["coordinates"]
    assert len(coords) > 2
