#!/usr/bin/env python3
"""Print sensors of interest (latest readings) for VRM installations.

Demonstrates the attribute-whitelist strategy for the Victron Energy connector:
- field name  = record["description"]  (API-provided human name)
- field value = record["formattedValue"] (API-provided value with unit)
- selection   = (Device, code) whitelist

Usage:
    VRM_TOKEN=<vrm access token> python3 local/vrm_readings.py [idSite ...]

Without site ids, prints all installations with data in the last 24 hours.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

BASE = "https://vrmapi.victronenergy.com/v2"
TOKEN = os.environ["VRM_TOKEN"]

# (device, code) whitelist. Order = display order (mirrors ER popup).
SENSORS_OF_INTEREST = {
    "Battery Monitor": ["V", "BT", "CE", "I", "McV", "mcV", "SOC", "TTG"],
    "Solar Charger": ["PVP", "YT", "ScS", "ScERR"],
    # Fallbacks from System overview when no Battery Monitor present
    "System overview": ["bv", "bc", "bs", "Pdc", "dc"],
}


def get(path):
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"x-authorization": f"Token {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def readings_for_site(id_site):
    diag = get(f"/installations/{id_site}/diagnostics?count=1000")
    out = []
    for device, codes in SENSORS_OF_INTEREST.items():
        recs = {r["code"]: r for r in diag.get("records", []) if r["Device"] == device}
        for code in codes:
            r = recs.get(code)
            if r is None or r["formattedValue"] in ("", None):
                continue
            out.append(r)
    return out


def main():
    me = get("/users/me")["user"]
    sites = get(f"/users/{me['id']}/installations?extended=1")["records"]

    # Optional filter: site ids as argv, else all live sites (data in last 24h)
    if len(sys.argv) > 1:
        wanted = {int(a) for a in sys.argv[1:]}
        sites = [s for s in sites if s["idSite"] in wanted]
    else:
        now = datetime.now(timezone.utc).timestamp()
        sites = [s for s in sites if now - s.get("last_timestamp", 0) < 86400]

    for s in sites:
        print(f"\n=== {s['name']} (idSite={s['idSite']}, last data {fmt_ts(s['last_timestamp'])}) ===")
        for r in readings_for_site(s["idSite"]):
            name = f"{r['description']} [{r['Device']}]"
            print(f"  {name:<42} {r['formattedValue']:>14}   ({fmt_ts(r['timestamp'])})")


if __name__ == "__main__":
    main()
