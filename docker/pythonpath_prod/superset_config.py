# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# This file is included in the final Docker image and SHOULD be overridden when
# deploying the image to prod. Settings configured here are intended for use in local
# development environments. Also note that superset_config_docker.py is imported
# as a final step as a means to override "defaults" configured here
#
import logging
import os
import sys

from celery.schedules import crontab
from flask_caching.backends.filesystemcache import FileSystemCache
from cachelib.redis import RedisCache

from typing import Literal

from superset_bearer_request_loader import BearerAuthSecurityManager
CUSTOM_SECURITY_MANAGER = BearerAuthSecurityManager

SESSION_COOKIE_SAMESITE: Literal["None", "Lax", "Strict"] = "None"

logger = logging.getLogger()

print("CUSTOM SUPERSET CONFIG LOADED")
DATABASE_DIALECT = os.getenv("DATABASE_DIALECT")
DATABASE_USER = os.getenv("DATABASE_USER")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD")
DATABASE_HOST = os.getenv("DATABASE_HOST")
DATABASE_PORT = os.getenv("DATABASE_PORT")
DATABASE_DB = os.getenv("DATABASE_DB")

# The SQLAlchemy connection string.
SQLALCHEMY_DATABASE_URI = (
    f"{DATABASE_DIALECT}://"
    f"{DATABASE_USER}:{DATABASE_PASSWORD}@"
    f"{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_DB}"
)

SQLALCHEMY_EXAMPLES_URI = None

print("CONFIG LOADED")
print("DATABASE_DIALECT=", DATABASE_DIALECT)
print("DATABASE_USER=", DATABASE_USER)
print("DATABASE_HOST=", DATABASE_HOST)
print("DATABASE_PORT=", DATABASE_PORT)
print("DATABASE_DB=", DATABASE_DB)
print("SQLALCHEMY_DATABASE_URI=", SQLALCHEMY_DATABASE_URI.replace(DATABASE_PASSWORD, "***") if DATABASE_PASSWORD else "NO PASSWORD")


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_CELERY_DB = os.getenv("REDIS_CELERY_DB", "0")
REDIS_RESULTS_DB = os.getenv("REDIS_RESULTS_DB", "0")


##
REDIS_LIMITER_DB = os.getenv("REDIS_LIMITER_DB", "0")
RATELIMIT_STORAGE_URI = f"rediss://{REDIS_HOST}:{REDIS_PORT}/{REDIS_LIMITER_DB}"
##
#RESULTS_BACKEND = FileSystemCache("/app/superset_home/sqllab")

RESULTS_BACKEND = RedisCache(
    host=REDIS_HOST,
    port=int(REDIS_PORT),
    db=0,
    key_prefix="superset_results_",
    ssl=True,
)




CACHE_CONFIG = {
    "CACHE_TYPE": "RedisCache",
    "CACHE_DEFAULT_TIMEOUT": 300,
    "CACHE_KEY_PREFIX": "superset_",
    "CACHE_REDIS_URL": f"rediss://{REDIS_HOST}:{REDIS_PORT}/0",
}

DATA_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_data_"}
THUMBNAIL_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_KEY_PREFIX": "superset_thumb_"}

#DATA_CACHE_CONFIG = CACHE_CONFIG
#THUMBNAIL_CACHE_CONFIG = CACHE_CONFIG



class CeleryConfig:
    broker_url = f"rediss://{REDIS_HOST}:{REDIS_PORT}/{REDIS_CELERY_DB}?ssl_cert_reqs=CERT_NONE"
    imports = (
        "superset.sql_lab",
        "superset.tasks.scheduler",
        "superset.tasks.thumbnails",
        "superset.tasks.cache",
    )
    result_backend = f"rediss://{REDIS_HOST}:{REDIS_PORT}/{REDIS_RESULTS_DB}?ssl_cert_reqs=CERT_NONE"
    # ElastiCache Serverless runs Redis in CLUSTER mode. Force every Celery/kombu key
    # into a single hash slot via the {superset} hash tag, so multi-key operations
    # (broker unacked handling, etc.) don't raise "CROSSSLOT Keys ... don't hash to
    # the same slot". Combined with the worker's --without-mingle/--without-gossip.
    broker_transport_options = {"global_keyprefix": "{superset}"}
    result_backend_transport_options = {"global_keyprefix": "{superset}"}
    # The worker remote-control mailbox uses pattern pub/sub (PSUBSCRIBE), which
    # ElastiCache Serverless (cluster mode) rejects. Reports/alerts don't need
    # celery inspect/control, so disable the pidbox.
    worker_enable_remote_control = False
    worker_prefetch_multiplier = 1
    task_acks_late = False
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=10, hour=0),
        },
    }


####
# ---------------------------------------------------------
# Feature Flags
# ---------------------------------------------------------
FEATURE_FLAGS = {
    "ALERT_REPORTS": True,
    "EMBEDDABLE_CHARTS": True,
    "EMBEDDED_SUPERSET": True,
    "DASHBOARD_RBAC": True,
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DASHBOARD_CROSS_FILTERS": True,
    "DISABLE_EMBEDDED_SUPERSET_LOGOUT": True,
    # Alerts & Reports: use Playwright/Chromium for screenshots (image built with
    # --build-arg INCLUDE_CHROMIUM=true) and expose the screenshot API for the
    # app-side export button.
    "PLAYWRIGHT_REPORTS_AND_THUMBNAILS": True,
    "ENABLE_DASHBOARD_SCREENSHOT_ENDPOINTS": True,
}

SUPERSET_FEATURE_EMBEDDED_SUPERSET=True


# ---------------------------------------------------------
# Embedding / Guest Token
# ---------------------------------------------------------
GUEST_ROLE_NAME = "Public"
GUEST_TOKEN_JWT_SECRET = os.getenv("GUEST_TOKEN_JWT_SECRET", "tQa8ckVRqjbHgqjzulkQqa4nFyW4qB6DCssxM1HuJjE=")
#GUEST_TOKEN_JWT_ALGO = "HS256"
#GUEST_TOKEN_HEADER_NAME = "X-GuestToken"
GUEST_TOKEN_JWT_EXP_SECONDS = 300

# ---------------------------------------------------------
# Public Role & User Registration
# This allows publicly accessible dashboards and ensures
# new users are assigned the minimum role instead of Admin
# ---------------------------------------------------------
AUTH_ROLE_PUBLIC = "Public"
PUBLIC_ROLE_LIKE = None
AUTH_USER_REGISTRATION_ROLE = "Public"

# ---------------------------------------------------------
# CORS — update origin to match your embedding domain
# ---------------------------------------------------------

ENABLE_CORS = True
CORS_OPTIONS = {
    "supports_credentials": True,
    "allow_headers": ["*"],
    "resources": ["*"],
    "origins": [
    "http://localhost",
    "https://localhost",
    os.getenv("EMBEDDING_ORIGIN", "https://www.baw-dashboard.kwsdcloud.eu"),
    "https://baw-superset.kwsdcloud.eu",
    ],
}

# ---------------------------------------------------------
# CSP / iframe embedding — update frame-ancestors to match
# your embedding domain
# ---------------------------------------------------------


TALISMAN_ENABLED = True
TALISMAN_CONFIG = {
    "session_cookie_samesite": "None",        # add this
    "session_cookie_secure": True,             # add this too
    "content_security_policy": {
        "default-src": "'self'",
        "img-src": "'self' data: https: http:",
        "script-src": "'self' 'unsafe-inline' 'unsafe-eval'",
        "style-src": "'self' 'unsafe-inline'",
        "font-src": "'self' https: data:",
        "connect-src": "'self' https: wss:",
        "frame-src": "'self' https:",
        "frame-ancestors": "'self' http://localhost https://localhost https://www.baw-dashboard.kwsdcloud.eu",
        "object-src": "'none'",
    },
    "content_security_policy_nonce_in": ["script-src"],
    "force_https": False,
}



####
ENABLE_PROXY_FIX = True
PROXY_FIX_CONFIG = {"x_for": 1, "x_proto": 1, "x_host": 1, "x_prefix": 1}

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
###
CELERY_CONFIG = CeleryConfig

# Alerts & Reports must actually deliver in production (was DRY_RUN=True).
ALERT_REPORTS_NOTIFICATION_DRY_RUN = False

# --- Headless screenshot tuning ---
# ROOT CAUSE of blank report/thumbnail screenshots: the dashboard renders fine in
# headless Chromium, but page.screenshot(full_page=True) only captures what has
# painted INSIDE the viewport — a tall dashboard's off-viewport charts never paint,
# so the capture comes out blank. FIX = Superset's built-in TILED screenshots,
# which scroll the viewport in tiles and stitch them so every chart paints. The
# default thresholds (20 charts / 5000px) sit just above Executive Overview
# (17 charts / 4490px), so we lower them so tiling triggers for it.
SCREENSHOT_TILED_ENABLED = True
SCREENSHOT_TILED_HEIGHT_THRESHOLD = 2000   # tile any dashboard taller than the viewport (default 5000)
SCREENSHOT_TILED_CHART_THRESHOLD = 10      # ...or with >= 10 charts (default 20)

# Waits kept modest — they were never the cause (the page paints fine); larger
# values only slow every report. These are the pre-existing values.
SCREENSHOT_PLAYWRIGHT_WAIT_EVENT = "load"
SCREENSHOT_SELENIUM_HEADSTART = 5          # settle after navigation (default 3)
SCREENSHOT_SELENIUM_ANIMATION_WAIT = 10    # give ECharts time to paint to canvas (default 5)
SCREENSHOT_PLAYWRIGHT_DEFAULT_TIMEOUT = 120000   # ms; headroom for the multi-capture tiled path (default 30000)
# NOTE: SCREENSHOT_REPLACE_UNEXPECTED_ERRORS is intentionally left False — its
# find_unexpected_errors() alert-expansion is broken in this Superset build
# (60s Playwright timeout) and disturbs the page right before capture.

# Chromium launch flags for the headless report/thumbnail renderer. The default
# is just ["--headless"], which is missing the flags needed to render reliably
# inside a container: without --disable-dev-shm-usage, Fargate's small /dev/shm
# fills up and Chromium intermittently fails to load JS chunks (ChunkLoadError).
WEBDRIVER_OPTION_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

# The Celery worker renders screenshots and builds email links against these — they
# must be real, reachable hostnames (NOT the docker-compose "superset_app" default).
WEBDRIVER_BASEURL = "https://baw-superset.kwsdcloud.eu/"
WEBDRIVER_BASEURL_USER_FRIENDLY = "https://baw-superset.kwsdcloud.eu/"

# --- SMTP via AWS SES (eu-central-1); SMTP_USER/PASSWORD come from secret superset-8NKjy1 ---
SMTP_HOST = os.getenv("SMTP_HOST", "email-smtp.eu-central-1.amazonaws.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_STARTTLS = True
SMTP_SSL = False
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_MAIL_FROM = os.getenv("SMTP_MAIL_FROM", "reports@kwsdcloud.eu")
EMAIL_REPORTS_SUBJECT_PREFIX = "[BAW Report] "
EMAIL_REPORTS_CTA = ""   # portal users cannot log in to Superset, so no link in emails

# --- Metadata DB pool hygiene (t3.small has headroom; just keep connections fresh) ---
SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 300}
SQLLAB_CTAS_NO_LIMIT = True

log_level_text = os.getenv("SUPERSET_LOG_LEVEL", "INFO")
LOG_LEVEL = getattr(logging, log_level_text.upper(), logging.INFO)

if os.getenv("CYPRESS_CONFIG") == "true":
    # When running the service as a cypress backend, we need to import the config
    # located @ tests/integration_tests/superset_test_config.py
    base_dir = os.path.dirname(__file__)
    module_folder = os.path.abspath(
        os.path.join(base_dir, "../../tests/integration_tests/")
    )
    sys.path.insert(0, module_folder)
    from superset_test_config import *  # noqa

    sys.path.pop(0)

#
# Optionally import superset_config_docker.py (which will have been included on
# the PYTHONPATH) in order to allow for local settings to be overridden
#
try:
    import superset_config_docker
    from superset_config_docker import *  # noqa: F403

    logger.info(
        f"Loaded your Docker configuration at [{superset_config_docker.__file__}]"
    )
except ImportError:
    logger.info("Using default Docker config...")
