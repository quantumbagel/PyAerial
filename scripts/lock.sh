#!/usr/bin/env bash
# Regenerate requirements.lock: hashed pins for pyaerial's runtime deps plus the
# setuptools/wheel build backend, used by the Dockerfile and the CI pip-audit job.
#
# Releases younger than 14 days are ignored so a freshly published (or hijacked)
# version never lands in the lock the moment it appears on PyPI.
set -euo pipefail
cd "$(dirname "$0")/.."

cutoff="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) - d.timedelta(days=14)).strftime("%Y-%m-%dT00:00:00Z"))')"

printf 'setuptools>=68\nwheel\n' | uv pip compile pyproject.toml - \
  --universal \
  --python-version 3.11 \
  --generate-hashes \
  --exclude-newer "${cutoff}" \
  --custom-compile-command "scripts/lock.sh" \
  --output-file requirements.lock \
  "$@"
