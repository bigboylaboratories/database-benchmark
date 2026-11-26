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

# Run a command as the postgres OS user, whether or not we are root.
as_postgres() {
  if [ "$(id -u)" -eq 0 ]; then runuser -u postgres -- "$@"; else sudo -u postgres "$@"; fi
}

echo ">> installing system packages"
$SUDO apt-get update -y
PKGS="python3 python3-venv python3-pip sqlite3 libpq5"
[ "${SKIP_POSTGRES:-0}" = "1" ] || PKGS="$PKGS postgresql"
# shellcheck disable=SC2086
$SUDO apt-get install -y $PKGS

if [ "${SKIP_POSTGRES:-0}" != "1" ]; then
  echo ">> configuring PostgreSQL user/database 'bench'"
  $SUDO systemctl enable --now postgresql 2>/dev/null || $SUDO service postgresql start
  as_postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='bench'" | grep -q 1 \
    || as_postgres psql -c "CREATE USER bench PASSWORD 'bench'"
  as_postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='bench'" | grep -q 1 \
    || as_postgres psql -c "CREATE DATABASE bench OWNER bench"
fi

echo ">> creating virtualenv in .venv"
python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 'dbbench needs Python 3.10+, found ' + sys.version.split()[0])"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
if [ "${SKIP_POSTGRES:-0}" = "1" ]; then
  .venv/bin/pip install -e .
else
  # No psycopg binary wheels for 32-bit ARM (armv7l Pi OS): fall back to pure-Python
  # psycopg, which uses the system libpq5 installed above.
  .venv/bin/pip install -e '.[postgres]' \
    || { .venv/bin/pip install -e . && .venv/bin/pip install 'psycopg>=3.2'; }
fi

echo
echo "Done. Next:"
echo "  source .venv/bin/activate"
echo "  dbbench sysinfo --verify-cores"
echo "  dbbench run -b all -p quick -l \"\$(hostname)\""
