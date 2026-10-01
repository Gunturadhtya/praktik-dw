#!/bin/sh
set -e

# Cron syntax: minute hour day-of-month month day-of-week
# Default = every minute. Change it in .env (CRON_SCHEDULE), not here.
SCHEDULE="${CRON_SCHEDULE:-* * * * *}"

mkdir -p /var/log/etl /var/lib/etl
touch /var/log/etl/etl.log /var/log/etl/cron.out

# cron does not inherit the container's environment -> dump it for the job to source
export -p > /app/env.sh

# Overlap protection is done inside run_etl.py (flock on LOCK_PATH).
cat > /etc/cron.d/etl <<CRON
SHELL=/bin/sh
${SCHEDULE} root . /app/env.sh; cd /app/src && python run_etl.py cron >> /var/log/etl/cron.out 2>&1
CRON
chmod 0644 /etc/cron.d/etl

echo "ETL cron installed. schedule='${SCHEDULE}'"

# optional: run once right now so you don't wait for the first tick
if [ "${RUN_ON_START:-true}" = "true" ]; then
  (. /app/env.sh; cd /app/src && python run_etl.py manual >> /var/log/etl/cron.out 2>&1) || true
fi

cron
exec tail -F /var/log/etl/etl.log /var/log/etl/cron.out