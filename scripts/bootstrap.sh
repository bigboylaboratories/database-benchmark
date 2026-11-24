#!/usr/bin/env bash
# Prepare a target machine (Debian / Ubuntu / Raspberry Pi OS, physical or VM)
# to run dbbench against local SQLite and PostgreSQL.
#
#   git clone <this repo> && cd database-benchmark && ./scripts/bootstrap.sh
#
# Set SKIP_POSTGRES=1 to only install the SQLite side.
set -euo pipefail

cd "$(dirname "$0")/.."

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

echo ">> installing system packages"
$SUDO apt-get update -y
PKGS="python3 python3-venv python3-pip sqlite3"
[ "${SKIP_POSTGRES:-0}" = "1" ] || PKGS="$PKGS postgresql"
# shellcheck disable=SC2086
$SUDO apt-get install -y $PKGS

if [ "${SKIP_POSTGRES:-0}" != "1" ]; then
  echo ">> configuring PostgreSQL user/database 'bench'"
  $SUDO systemctl enable --now postgresql 2>/dev/null || $SUDO service postgresql start
  $SUDO -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='bench'" | grep -q 1 \
    || $SUDO -u postgres psql -c "CREATE USER bench PASSWORD 'bench'"
  $SUDO -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='bench'" | grep -q 1 \
    || $SUDO -u postgres psql -c "CREATE DATABASE bench OWNER bench"
fi

echo ">> creating virtualenv in .venv"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
if [ "${SKIP_POSTGRES:-0}" = "1" ]; then
  .venv/bin/pip install -e .
else
  .venv/bin/pip install -e '.[postgres]'
fi

echo
echo "Done. Next:"
echo "  source .venv/bin/activate"
echo "  dbbench sysinfo --verify-cores"
echo "  dbbench run -b all -p quick -l \"\$(hostname)\""
