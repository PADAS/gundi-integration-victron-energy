#!/usr/bin/env bash
# Register (or update) the victron_energy integration type and its actions in
# the Gundi environment configured in the repo-root .env file.
#
# Registers: integration type "victron_energy" with the auth and
# pull_observations actions, their JSON config schemas / UI schemas, and the
# crontab schedules declared with @crontab_schedule in app/actions/handlers.py.
#
# Usage:
#   ./local/register_integration.sh                 # register/update
#
# Re-run whenever action config schemas change so the portal forms stay in sync.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "No .venv found — create it first:"
    echo "  python3.10 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

echo "Registering integration type 'victron_energy' in: $(grep -E '^GUNDI_API_BASE_URL' .env)"
exec "$PYTHON" -m app.register --slug victron_energy
