#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# RDS metadata-DB connection-headroom check for the Superset concurrency bump
# (item 5: web gunicorn workers 1 -> 4) and, if added, the Alerts & Reports
# Celery worker + beat services.
#
# WHY: each Superset PROCESS (every gunicorn worker, every celery prefork child,
# the beat) opens its OWN SQLAlchemy engine pool to the metadata DB. With the
# default pool (pool_size=5 + max_overflow=10 = 15 max/process), more processes
# => more metadata connections. This script reads the DB's real max_connections
# + current usage and compares against the projected peak so the load test
# doesn't hit "FATAL: remaining connection slots are reserved".
#
# The IAM user 'AWS_CLI_ECR' lacks rds:DescribeDBInstances and
# secretsmanager:GetSecretValue, so run this with a profile that has them, OR
# supply the metadata DB password via PGPASSWORD (see PATH B).
#
# Metadata DB (from web taskdef):
#   host superset-metadata-db.ct4y62gactqk.eu-central-1.rds.amazonaws.com
#   port 5432  db postgres  user postgres
# ---------------------------------------------------------------------------
set -euo pipefail

REGION=eu-central-1
PROFILE="${PROFILE:-default}"
PGHOST=superset-metadata-db.ct4y62gactqk.eu-central-1.rds.amazonaws.com
PGPORT=5432
PGDATABASE=postgres
PGUSER=postgres
SECRET_ID=superset-8NKjy1        # JSON secret, key DATABASE_PASSWORD

# ---- Projected peak metadata connections (default pool = 15 max/process) ----
POOL_MAX=15
WEB_WORKERS=4                     # item 5 target
CELERY_CONCURRENCY=4             # A&R worker prefork children (0 if not deploying A&R)
BEAT=1                            # A&R beat (0 if not deploying)
DEPLOY_AR="${DEPLOY_AR:-yes}"     # set to "no" to model item 5 only

web=$(( WEB_WORKERS * POOL_MAX ))
if [ "$DEPLOY_AR" = "yes" ]; then
  worker=$(( CELERY_CONCURRENCY * POOL_MAX ))
  beat=$(( BEAT * POOL_MAX ))
else
  worker=0; beat=0
fi
peak=$(( web + worker + beat ))
echo "=== Projected metadata-DB connection peak ==="
echo "  web:    ${WEB_WORKERS} workers x ${POOL_MAX} = ${web}"
[ "$DEPLOY_AR" = "yes" ] && echo "  worker: ${CELERY_CONCURRENCY} x ${POOL_MAX} = ${worker}"
[ "$DEPLOY_AR" = "yes" ] && echo "  beat:   ${BEAT} x ${POOL_MAX} = ${beat}"
echo "  MCP:    0 (talks to the REST API, not the metadata DB directly)"
echo "  ------------------------------------------"
echo "  WORST-CASE PEAK: ${peak} connections   (steady-state ~1/3 of this)"
echo

# ---- PATH A: read max_connections via RDS API (needs rds:* perms) ----------
echo "=== Reading actual RDS capacity ==="
if aws rds describe-db-instances --db-instance-identifier superset-metadata-db \
      --region "$REGION" --profile "$PROFILE" >/tmp/rds.json 2>/dev/null; then
  CLASS=$(python3 -c "import json;print(json.load(open('/tmp/rds.json'))['DBInstances'][0]['DBInstanceClass'])")
  PG=$(python3 -c "import json;print(json.load(open('/tmp/rds.json'))['DBInstances'][0]['DBParameterGroups'][0]['DBParameterGroupName'])")
  echo "  instance class: $CLASS   parameter group: $PG"
  aws rds describe-db-parameters --db-parameter-group-name "$PG" \
    --region "$REGION" --profile "$PROFILE" \
    --query "Parameters[?ParameterName=='max_connections'].ParameterValue" --output text \
    | sed 's/^/  max_connections (param): /'
  echo "  NOTE: default value is the formula LEAST({DBInstanceClassMemory/9531392},5000)."
else
  echo "  (RDS API not permitted with profile '$PROFILE' — use PATH B)"
fi
echo

# ---- PATH B: read live values straight from Postgres (needs network + pw) ---
# Get the password from Secrets Manager if PGPASSWORD isn't already set:
if [ -z "${PGPASSWORD:-}" ]; then
  PGPASSWORD=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ID" \
    --region "$REGION" --profile "$PROFILE" --query SecretString --output text 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['DATABASE_PASSWORD'])" 2>/dev/null || true)
fi
export PGPASSWORD PGHOST PGPORT PGDATABASE PGUSER
if [ -n "${PGPASSWORD:-}" ] && command -v psql >/dev/null; then
  echo "=== Live metadata-DB state ==="
  psql "sslmode=require" -tA -F' | ' <<'SQL'
SELECT 'max_connections'  AS k, setting FROM pg_settings WHERE name='max_connections'
UNION ALL SELECT 'reserved', setting FROM pg_settings WHERE name='superuser_reserved_connections'
UNION ALL SELECT 'in_use_now', count(*)::text FROM pg_stat_activity
UNION ALL SELECT 'active_now', count(*)::text FROM pg_stat_activity WHERE state='active';
SELECT usename, count(*) FROM pg_stat_activity GROUP BY usename ORDER BY 2 DESC;
SQL
else
  echo "=== PATH B skipped ==="
  echo "  Need psql + the metadata DB password. Either:"
  echo "    PGPASSWORD=<pw> ./rds-connection-headroom-check.sh"
  echo "  or run this SQL from any host with DB access:"
  echo "    SELECT setting FROM pg_settings WHERE name='max_connections';"
  echo "    SELECT count(*), usename FROM pg_stat_activity GROUP BY usename;"
fi

echo
echo "=== Verdict rule ==="
echo "  Need: max_connections - reserved  >=  ${peak}  + existing_app_usage."
echo "  If it's tight (e.g. a db.t3.micro ~100 / t3.small ~200 cap), pick one:"
echo "   A) BOUND Superset pools in superset_config.py (predictable + cheapest):"
echo "        SQLALCHEMY_ENGINE_OPTIONS = {"
echo "            'pool_size': 5, 'max_overflow': 5,"
echo "            'pool_pre_ping': True, 'pool_recycle': 300,"
echo "        }"
echo "      -> caps each process at 10, so peak becomes web 40 + worker 40 + beat 10 = 90."
echo "   B) Raise RDS max_connections (custom parameter group) or size up the instance."
echo "   C) Put pgbouncer in front of the metadata DB (best at high process counts)."
