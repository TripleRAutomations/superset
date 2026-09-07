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
"""
Custom Superset security manager that authenticates FAB Bearer JWTs in the
Flask-Login request loader.

WHY THIS EXISTS
---------------
Embedded dashboards require the ``Public`` role to hold read permissions
(``can read on Dashboard/Chart/Dataset`` ...), because an embedded *guest*
request carries only an ``X-GuestToken`` and can never satisfy
``verify_jwt_in_request()`` — so it clears FAB's ``@protect`` gate solely via
``is_item_public()`` (i.e. the Public role's permissions).

Side effect: ``@protect`` checks ``is_item_public()`` *before* it calls
``verify_jwt_in_request()``. For any endpoint whose permission the Public role
holds, that check short-circuits and runs the view as whatever ``g.user``
already is. A pure Bearer-JWT API client (e.g. the MCP service account) has no
Flask session, so ``current_user`` is anonymous at that point and its JWT is
never evaluated — its *reads* of Dashboard/Chart/Dataset come back empty even
though the account is an Admin. (Writes are unaffected: those endpoints are not
public, so ``verify_jwt_in_request()`` still runs and authenticates the token.)

THE FIX
-------
Flask-Login calls ``request_loader`` when a request has no authenticated
session. By decoding a valid ``Authorization: Bearer <access_token>`` here, we
set ``current_user`` to the token's user *before* ``@protect`` runs. Now an
``is_item_public`` short-circuit executes the view as the authenticated user
(Admin), so its reads return full results. Nothing new is granted — a valid,
unexpired, correctly-signed access token simply authenticates the user it
already identifies, exactly as ``verify_jwt_in_request()`` would.

Guest tokens keep working: the parent ``request_loader`` is consulted first.

DEPLOY
------
1. Put this module on Superset's PYTHONPATH (same place as ``superset_config.py``).
2. In ``superset_config.py``:

       from superset_bearer_request_loader import BearerAuthSecurityManager
       CUSTOM_SECURITY_MANAGER = BearerAuthSecurityManager

3. Rebuild/redeploy the Superset image.

After deploy, verify BOTH: the MCP can read dashboards again, AND the MCP can
still WRITE (create/update) — see the smoke test at the bottom of this file.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from flask import Request
from flask_appbuilder.security.sqla.models import User
from flask_jwt_extended import decode_token

from superset.security import SupersetSecurityManager

logger = logging.getLogger(__name__)

# flask-jwt-extended stamps access tokens with {"type": "access"}. We refuse
# refresh tokens (and anything else) so a refresh token can't be replayed as an
# API credential.
_ACCESS_TOKEN_TYPE = "access"
_BEARER_PREFIX = "Bearer "


class BearerAuthSecurityManager(SupersetSecurityManager):
    """Authenticate FAB Bearer JWTs up-front so ``is_item_public`` short-circuits
    don't silently drop token-authenticated API clients to the anonymous user."""

    def request_loader(self, request: Request) -> Optional[User]:
        # 1) Preserve existing behaviour first — notably embedded guest tokens
        #    (X-GuestToken). If the parent resolves a user, use it.
        user = super().request_loader(request)
        if user is not None:
            return user

        # 2) Otherwise, try to authenticate a FAB Bearer access token.
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith(_BEARER_PREFIX):
            return None

        raw_token = auth_header[len(_BEARER_PREFIX):].strip()
        if not raw_token:
            return None

        try:
            # decode_token validates signature + expiry (raises on failure) using
            # the app's JWT_SECRET_KEY / SECRET_KEY. A guest token (signed with
            # GUEST_TOKEN_JWT_SECRET) or any invalid token raises here -> None.
            claims: dict[str, Any] = decode_token(raw_token)
        except Exception:  # noqa: BLE001 - any decode/validation error => not authenticated
            logger.debug("Bearer request_loader: token failed validation", exc_info=True)
            return None

        if claims.get("type") != _ACCESS_TOKEN_TYPE:
            # Refuse refresh tokens / non-access tokens as API credentials.
            return None

        identity = claims.get("sub")
        if identity is None:
            return None

        # load_user() casts to int and returns the user only if it is active.
        loaded = self.load_user(identity)
        if loaded is None:
            logger.warning(
                "Bearer request_loader: valid token for unknown/inactive user id=%s",
                identity,
            )
            return None
        return loaded


# --- Smoke test after deploy -------------------------------------------------
# From any host that can reach Superset, using the MCP service account:
#
#   BASE=https://baw-superset.kwsdcloud.eu
#   TOKEN=$(curl -s $BASE/api/v1/security/login -H 'Content-Type: application/json' \
#     -d '{"username":"mcp-service","password":"<pw>","provider":"db","refresh":true}' \
#     | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
#   # READ must now return real dashboards (was count:0 before this SM):
#   curl -s "$BASE/api/v1/dashboard/" -H "Authorization: Bearer $TOKEN" \
#     | python3 -c 'import sys,json;print("dashboards:",json.load(sys.stdin)["count"])'
#   # And anonymous (no token) must still be locked:
#   curl -s -o /dev/null -w '%{http_code}\n' "$BASE/api/v1/dashboard/4"   # expect 404
