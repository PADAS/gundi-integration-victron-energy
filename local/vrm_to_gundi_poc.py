#!/usr/bin/env python3
"""Proof of concept: pull latest VRM readings and push them to Gundi as observations.

Pipeline (mirrors the pull_observations action):
  VRM diagnostics -> sensors-of-interest whitelist -> Gundi v2 observation schema
  -> POST {GUNDI_SENSORS_URL}/v2/observations (apikey header)

Usage:
    VRM_TOKEN=<vrm access token> GUNDI_API_KEY=<integration api key> \
        python3 local/vrm_to_gundi_poc.py [--dry-run]

Installation locations are not available from the VRM API; for the PoC they
are hardcoded below (same role as the future action config).
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

VRM_BASE = "https://vrmapi.victronenergy.com/v2"
GUNDI_SENSORS_URL = os.environ.get("GUNDI_SENSORS_URL", "https://sensors.api.stage.gundiservice.org")
VRM_TOKEN = os.environ["VRM_TOKEN"]
GUNDI_API_KEY = os.environ.get("GUNDI_API_KEY")

# --- PoC stand-in for the action config -------------------------------------
# Sites not visible to the current VRM_TOKEN's account are skipped, so this can
# hold installations from several accounts; run the script once per token.
# FICTIONAL placeholder values — replace with your own installation ids and
# coordinates before running (never commit real site data).
INSTALLATIONS = {
    # idSite: (latitude, longitude, optional subject name override)
    100001: (-12.345678, 34.567890, None),
    100002: (-12.350000, 34.570000, "Hilltop Repeater (PoC)"),
    100003: (-12.355000, 34.575000, "HQ Solar (PoC)"),
}
SUBJECT_SUBTYPE = "stationary-object"
SENSORS_OF_INTEREST = {
    "Battery Monitor": ["V", "BT", "CE", "I", "McV", "mcV", "SOC", "TTG"],
    "Solar Charger": ["PVP", "YT", "ScS", "ScERR"],
    "System overview": ["bv", "bc", "bs", "Pdc", "dc"],
}
MAX_READING_AGE_S = 86400  # drop diagnostics records older than this (loose for PoC)
# -----------------------------------------------------------------------------


def vrm_get(path):
    req = urllib.request.Request(
        f"{VRM_BASE}{path}", headers={"x-authorization": f"Token {VRM_TOKEN}"}
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def build_observation(site, lat, lon, name_override):
    """One Gundi observation from the latest whitelisted diagnostics records."""
    diag = vrm_get(f"/installations/{site['idSite']}/diagnostics?count=1000")
    now = datetime.now(timezone.utc).timestamp()
    readings, newest_ts, seen_names = {}, 0, set()
    for device, codes in SENSORS_OF_INTEREST.items():
        recs = {r["code"]: r for r in diag.get("records", []) if r["Device"] == device}
        for code in codes:
            r = recs.get(code)
            if r is None or r["formattedValue"] in ("", None):
                continue
            if now - r["timestamp"] > MAX_READING_AGE_S:
                continue
            if r["description"] in seen_names:  # dedup: Battery Monitor wins
                continue
            seen_names.add(r["description"])
            readings[r["description"]] = r["formattedValue"]
            newest_ts = max(newest_ts, r["timestamp"])
    if not readings:
        return None
    return {
        # idSite, not device identifier: survives GX hardware swaps
        "source": str(site["idSite"]),
        "source_name": name_override or site["name"],
        "type": "stationary-object",
        "subject_type": "stationary-object",
        "subject_subtype": SUBJECT_SUBTYPE,
        "recorded_at": datetime.fromtimestamp(newest_ts, timezone.utc).isoformat(),
        "location": {"lat": lat, "lon": lon},
        "additional": readings,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    me = vrm_get("/users/me")["user"]
    sites = {s["idSite"]: s for s in vrm_get(f"/users/{me['id']}/installations?extended=1")["records"]}

    observations = []
    for id_site, (lat, lon, name) in INSTALLATIONS.items():
        site = sites.get(id_site)
        if site is None:
            print(f"! installation {id_site} not visible to this VRM account, skipping")
            continue
        obs = build_observation(site, lat, lon, name)
        if obs is None:
            print(f"! {site['name']}: no fresh readings, skipping")
            continue
        observations.append(obs)

    print(json.dumps(observations, indent=2))
    if dry_run or not observations:
        print("-- dry run, nothing sent --" if dry_run else "-- nothing to send --")
        return
    if not GUNDI_API_KEY:
        sys.exit("GUNDI_API_KEY is not set — use --dry-run or export the key.")

    req = urllib.request.Request(
        f"{GUNDI_SENSORS_URL}/v2/observations/",
        data=json.dumps(observations).encode(),
        headers={"apikey": GUNDI_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            print(f"Gundi response: HTTP {r.status}")
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Gundi error: HTTP {e.code}")
        print(e.read().decode())


if __name__ == "__main__":
    main()
