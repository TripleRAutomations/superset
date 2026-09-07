# Runbook — Enable Alerts & Reports (FR-37, FR-53)

Superset 6.0.0 on ECS cluster `superset-cluster-01`, region `eu-central-1`,
account `767397765406`, profile `default` (NEVER qradar).

**Current state (verified 2026-07):** `ALERT_REPORTS` flag is ON and `CeleryConfig`
+ `beat_schedule` already exist in `superset_config.py`, Redis broker
(`rediss://` ElastiCache) exists — but **nothing executes them**. Missing:
Celery worker + beat services, a headless browser in the image, real SMTP, and
`DRY_RUN` is still `True`. So users can create reports in the UI but they never run.

Goal: an admin creates a Report (dashboard PNG on a schedule) or an Alert (email
when a SQL condition trips) and it actually delivers.

---

## 0. Values (discovered)

| Value | |
|---|---|
| Cluster | `superset-cluster-01` |
| Web task def | `superset-ecr-init` (rev 27), image `767397765406.dkr.ecr.eu-central-1.amazonaws.com/superset/prod` |
| Web service | `superset-ecr-init-service-jjygu0xr` |
| Web task SG | `sg-005d90635058fd020` (already reaches RDS + Redis) |
| Exec role | `arn:aws:iam::767397765406:role/ecsTaskExecutionRole` |
| Secret (JSON) | `superset-8NKjy1` (keys: `DATABASE_PASSWORD`, `SECRET_KEY`, …) |
| Subnets | `subnet-037b5d88cb0dd4ab3 subnet-061b9a75934cf1b44 subnet-070e80833bf865a4d subnet-0fb391fd01b3bd599 subnet-08caeed98386e0a90` |
| Screenshot engine | Playwright/Chromium (Dockerfile arg `INCLUDE_CHROMIUM` already present) |
| Executor | `ALERT_REPORTS_EXECUTORS = [ExecutorType.OWNER]` (screenshots run AS the report's owner — owner must be able to open the dashboard; use an Admin owner) |

---

## 1. Image: bake in the headless browser

The Dockerfile already gates Playwright behind a build arg — just turn it on and
tell Superset to use Playwright:

```bash
cd <superset image build context>
docker build --platform linux/amd64 \
  --build-arg INCLUDE_CHROMIUM=true \
  -t 767397765406.dkr.ecr.eu-central-1.amazonaws.com/superset/prod:reports .
docker push 767397765406.dkr.ecr.eu-central-1.amazonaws.com/superset/prod:reports
```

The SAME image runs web + worker + beat (so the custom security manager module
copied in the Dockerfile is present everywhere — required, since all three import
`superset_config.py`).

## 2. Config changes (`docker/pythonpath_prod/superset_config.py`)

```python
FEATURE_FLAGS = {
    "ALERT_REPORTS": True,                      # already set
    "PLAYWRIGHT_REPORTS_AND_THUMBNAILS": True,  # ADD — use Playwright/Chromium (not selenium)
    # (optional, item 4) app-side export button hitting the screenshot API:
    "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS": True,
    # ...keep EMBEDDED_SUPERSET, DASHBOARD_RBAC, etc...
}

# line 217 today is `ALERT_REPORTS_NOTIFICATION_DRY_RUN = True` — FLIP IT:
ALERT_REPORTS_NOTIFICATION_DRY_RUN = False

# Worker screenshots must reach Superset over a real hostname (NOT the
# docker-compose "superset_app" default that's baked in now):
WEBDRIVER_BASEURL = "https://baw-superset.kwsdcloud.eu/"
WEBDRIVER_BASEURL_USER_FRIENDLY = "https://baw-superset.kwsdcloud.eu/"

# --- SMTP via AWS SES (eu-central-1) ---
SMTP_HOST = "email-smtp.eu-central-1.amazonaws.com"
SMTP_PORT = 587
SMTP_STARTTLS = True
SMTP_SSL = False
SMTP_USER = os.environ["SMTP_USER"]            # SES SMTP username (from secret)
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]    # SES SMTP password (from secret)
SMTP_MAIL_FROM = "reports@baw-dashboard.kwsdcloud.eu"   # must be SES-verified
EMAIL_REPORTS_SUBJECT_PREFIX = "[BAW Report] "
```

> Note: `WEBDRIVER_BASEURL` sends the worker back through the public ALB. If you
> prefer it stays in-VPC, point it at an internal ALB/hostname the worker SG can
> reach. Either works; public ALB is simplest since the worker screenshots as an
> authenticated Admin owner.

## 3. SES + secret

```bash
# 3a. Verify the FROM domain (or a single email) in SES, region eu-central-1.
aws ses verify-domain-identity --domain baw-dashboard.kwsdcloud.eu \
  --region eu-central-1 --profile default
# add the returned TXT + DKIM CNAMEs to DNS; if the SES account is still in the
# sandbox, also verify each recipient OR request production access.

# 3b. Create SES SMTP credentials (IAM user -> SES SMTP user/pass) in the console,
#     then add them to the existing JSON secret:
aws secretsmanager get-secret-value --secret-id superset-8NKjy1 \
  --region eu-central-1 --profile default --query SecretString --output text > /tmp/s.json
#   edit /tmp/s.json to add "SMTP_USER" and "SMTP_PASSWORD", then:
aws secretsmanager put-secret-value --secret-id superset-8NKjy1 \
  --secret-string file:///tmp/s.json --region eu-central-1 --profile default
rm -f /tmp/s.json
```

## 4. Two new ECS services (worker + beat)

Register a worker task def (`superset-worker`) and a beat task def
(`superset-beat`) — same image, same env/secrets as the web task, only the
`command` differs. Env must include the DB/Redis vars the web task has, plus
`SMTP_USER`/`SMTP_PASSWORD` from the secret.

Worker command:
```
celery --app=superset.tasks.celery_app:app worker --pool=prefork --concurrency=4 -O fair -l INFO
```
Beat command (singleton — desired-count MUST be 1):
```
celery --app=superset.tasks.celery_app:app beat -l INFO
```

```bash
aws ecs register-task-definition --cli-input-json file://superset-worker-taskdef.json \
  --region eu-central-1 --profile default
aws ecs register-task-definition --cli-input-json file://superset-beat-taskdef.json \
  --region eu-central-1 --profile default

# Worker (scale as needed):
aws ecs create-service --cluster superset-cluster-01 --service-name superset-worker-service \
  --task-definition superset-worker --desired-count 1 \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-037b5d88cb0dd4ab3,subnet-061b9a75934cf1b44,subnet-070e80833bf865a4d,subnet-0fb391fd01b3bd599,subnet-08caeed98386e0a90],securityGroups=[sg-005d90635058fd020],assignPublicIp=ENABLED}' \
  --region eu-central-1 --profile default

# Beat (EXACTLY one — do not scale >1, or every schedule fires N times):
aws ecs create-service --cluster superset-cluster-01 --service-name superset-beat-service \
  --task-definition superset-beat --desired-count 1 \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-037b5d88cb0dd4ab3,subnet-061b9a75934cf1b44,subnet-070e80833bf865a4d,subnet-0fb391fd01b3bd599,subnet-08caeed98386e0a90],securityGroups=[sg-005d90635058fd020],assignPublicIp=ENABLED}' \
  --region eu-central-1 --profile default
```
Reusing the web SG (`sg-005d90635058fd020`) gives the worker DB + Redis reach.
It also needs egress to the ALB (screenshots) and to SES:587 — default egress
allow-all covers both; if egress is locked down, add those rules.

## 5. Redeploy web with the new image + config

```bash
aws ecs update-service --cluster superset-cluster-01 \
  --service superset-ecr-init-service-jjygu0xr \
  --task-definition <new web taskdef rev> --force-new-deployment \
  --region eu-central-1 --profile default
```

## 6. Verify

```bash
# services healthy:
aws ecs describe-services --cluster superset-cluster-01 \
  --services superset-worker-service superset-beat-service \
  --region eu-central-1 --profile default \
  --query 'services[].{name:serviceName,running:runningCount,desired:desiredCount}'

# worker picked up the beat tick (fires every minute):
aws logs tail /ecs/superset-ecr-init --since 5m --region eu-central-1 --profile default \
  | grep -iE "reports.scheduler|strategy|execute" | head
```
Then in the UI (Settings → Alerts & Reports):
1. Create a **Report** on a dashboard, schedule `* * * * *`, recipient = your email.
2. Within ~1–2 min you should receive the dashboard PNG.
3. Create an **Alert** with a SQL query returning a row → confirm the email fires.
4. Check SES sending stats for deliveries/bounces.

## Rollback
Additive: `update-service --desired-count 0` on worker+beat (or delete them),
revert `ALERT_REPORTS_NOTIFICATION_DRY_RUN = True`, redeploy web. No data touched.

## Gotchas
- **Beat must be exactly 1 replica** — multiple beats = duplicate report emails.
- **Owner access:** reports screenshot AS the owner (`ExecutorType.OWNER`). Create
  reports under an Admin (or an account that can open the dashboard), else the
  screenshot renders a permission error.
- **SES sandbox** blocks unverified recipients — verify recipients or request prod
  access before the client test.
- The worker imports `superset_config.py` → needs `superset_bearer_request_loader.py`
  in the image (already handled by the Dockerfile COPY added earlier).
