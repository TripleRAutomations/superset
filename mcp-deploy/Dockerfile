# Standalone image for the third-party Superset MCP server (bintocher/mcp-superset).
# It does NOT use the Superset source tree — it talks to Superset's REST API over
# the network. Build context can be this folder alone.
#
#   docker build --platform linux/amd64 -t superset-mcp:0.2.7 .
#
FROM python:3.12-slim

# Pinned for supply-chain safety. Bump deliberately after reviewing the release.
ARG MCP_SUPERSET_VERSION=0.2.7
RUN pip install --no-cache-dir "mcp-superset==${MCP_SUPERSET_VERSION}"

# Non-root runtime user.
RUN useradd -r -u 10001 mcp
USER mcp

EXPOSE 8001

# Long-lived streamable-http server, bound to all interfaces INSIDE the task.
# Public exposure is controlled at the ALB (source-IP allowlist) + a dedicated
# security group — NOT here. This server has no client-side auth of its own:
# every caller that reaches it acts as the single Superset service account.
ENTRYPOINT ["mcp-superset"]
CMD ["--host", "0.0.0.0", "--port", "8001", "--transport", "streamable-http"]
