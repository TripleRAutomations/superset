# Deploy the (third-party) Superset MCP server on ECS — locked down

Runs `bintocher/mcp-superset` (v0.2.7) as its own Fargate service on `superset-cluster-01`,
talking to Superset over its REST API. **No change to the Superset image.** Because the
server has *no client-side auth* (every caller acts as one Superset admin account) and the
clients are **roaming laptops** (dynamic IPs), `/mcp` is gated either by a **VPN-only internal
ALB** (recommended) or a **shared-secret header + WAF** at the public ALB. A source-IP
allowlist is NOT viable for roaming clients.

Region `eu-central-1` · account `767397765406` · profile `default` (NEVER qradar).

---

## 0. Values to fill in

Known (discovered from your ECS):

| Value | |
|---|---|
| Cluster | `superset-cluster-01` |
| Subnets | `subnet-037b5d88cb0dd4ab3 subnet-061b9a75934cf1b44 subnet-070e80833bf865a4d subnet-0fb391fd01b3bd599 subnet-08caeed98386e0a90` |
| Web task SG | `sg-005d90635058fd020` |
| Exec role | `arn:aws:iam::767397765406:role/ecsTaskExecutionRole` |
| ECR registry | `767397765406.dkr.ecr.eu-central-1.amazonaws.com` |
| Superset URL | `https://baw-superset.kwsdcloud.eu`  ← confirm |

You must supply (ELB read was denied, so I couldn't auto-discover these):

| Placeholder | How to get it |
|---|---|
| `<VPC_ID>` | `aws ec2 describe-subnets --subnet-ids subnet-037b5d88cb0dd4ab3 --query 'Subnets[0].VpcId'` |
| `<ALB_ARN>` / `<HTTPS_LISTENER_ARN>` | from the ALB fronting `superset-tg` |
| `<ALB_SG_ID>` | the ALB's security group |
| `<MCP_ACCESS>` | VPN + internal-ALB details (Option 1) **or** a long random `X-MCP-Token` secret (Option 2) — see step 4 |

---

## 1. Dedicated MCP service account + secrets

Create a **dedicated Superset admin user** for the MCP server (not a human's login) so its
actions are attributable and revocable. Then store its creds in Secrets Manager:

```bash
aws secretsmanager create-secret --name superset/mcp/username \
  --secret-string 'mcp-service' --region eu-central-1 --profile default
aws secretsmanager create-secret --name superset/mcp/password \
  --secret-string '<STRONG_PASSWORD>' --region eu-central-1 --profile default
```

> The task-def references these by ARN. Secrets Manager appends a 6-char suffix to the ARN
> (`...:secret:superset/mcp/username-AbC123`). After creating, copy the real ARNs into
> `superset-mcp-taskdef.json` (or keep the names and let me fetch the ARNs).

The **execution role** (`ecsTaskExecutionRole`) must be able to read them — it needs
`secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:eu-central-1:767397765406:secret:superset/mcp/*`
(and `kms:Decrypt` if you use a CMK). Add if missing.

## 2. Build & push the image

```bash
cd mcp-deploy
aws ecr create-repository --repository-name superset-mcp --region eu-central-1 --profile default
aws ecr get-login-password --region eu-central-1 --profile default \
  | docker login --username AWS --password-stdin 767397765406.dkr.ecr.eu-central-1.amazonaws.com
docker build --platform linux/amd64 -t 767397765406.dkr.ecr.eu-central-1.amazonaws.com/superset-mcp:0.2.7 .
docker push 767397765406.dkr.ecr.eu-central-1.amazonaws.com/superset-mcp:0.2.7
```

## 3. Register task def + create the service

```bash
aws ecs register-task-definition --cli-input-json file://superset-mcp-taskdef.json \
  --region eu-central-1 --profile default

# Dedicated SG so only the ALB can reach port 8001:
aws ec2 create-security-group --group-name superset-mcp-sg \
  --description "MCP task: 8001 from ALB only" --vpc-id <VPC_ID> \
  --region eu-central-1 --profile default            # -> <MCP_SG_ID>
aws ec2 authorize-security-group-ingress --group-id <MCP_SG_ID> \
  --protocol tcp --port 8001 --source-group <ALB_SG_ID> \
  --region eu-central-1 --profile default

aws ecs create-service \
  --cluster superset-cluster-01 --service-name superset-mcp-service \
  --task-definition superset-mcp --desired-count 1 \
  --capacity-provider-strategy capacityProvider=FARGATE,weight=1 \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-037b5d88cb0dd4ab3,subnet-061b9a75934cf1b44,subnet-070e80833bf865a4d,subnet-0fb391fd01b3bd599,subnet-08caeed98386e0a90],securityGroups=[<MCP_SG_ID>],assignPublicIp=ENABLED}' \
  --load-balancers 'targetGroupArn=<MCP_TG_ARN>,containerName=superset-mcp,containerPort=8001' \
  --region eu-central-1 --profile default
```

## 4. ALB: target group + gating for ROAMING clients

Source-IP allowlisting does not work for laptops on dynamic IPs. Pick ONE:

### Option 1 (recommended): VPN-only internal ALB
Expose the MCP target group on an **internal** ALB (scheme `internal`) reachable only from
your VPC/VPN. Laptops hit it over the corporate VPN; nothing is public — access equals VPN
membership and is revoked centrally. Best fit if you have a VPN.
- Create/reuse an **internal** ALB in the same subnets, HTTPS listener + internal cert.
- Target group `superset-mcp-tg` (HTTP:8001, `target-type ip`) → forward rule for `/mcp,/mcp/*`.
- MCP task SG `<MCP_SG_ID>` allows 8001 **from the internal ALB's SG only**.
- Clients point at `https://mcp.internal.<your-domain>/mcp` while on VPN.

### Option 2 (no VPN): public ALB + shared-secret header + WAF
Gate `/mcp` on a secret header at the existing public HTTPS listener. Works for roaming
clients (they send the header); the token is the *only* guard, so treat it as high-value and
rotate it.

```bash
# target group
aws elbv2 create-target-group --name superset-mcp-tg --protocol HTTP --port 8001 \
  --vpc-id <VPC_ID> --target-type ip \
  --health-check-protocol HTTP --health-check-path /mcp/ --matcher HttpCode=200-499 \
  --region eu-central-1 --profile default            # -> <MCP_TG_ARN>

# Rule A (priority 10): correct secret header -> forward to MCP
aws elbv2 create-rule --listener-arn <HTTPS_LISTENER_ARN> --priority 10 \
  --conditions \
     'Field=path-pattern,Values=/mcp,/mcp/*' \
     'Field=http-header,HttpHeaderConfig={HttpHeaderName=X-MCP-Token,Values=[<LONG_RANDOM_SECRET>]}' \
  --actions Type=forward,TargetGroupArn=<MCP_TG_ARN> \
  --region eu-central-1 --profile default

# Rule B (priority 11): any other /mcp request -> 403 (never reaches Superset/MCP)
aws elbv2 create-rule --listener-arn <HTTPS_LISTENER_ARN> --priority 11 \
  --conditions 'Field=path-pattern,Values=/mcp,/mcp/*' \
  --actions 'Type=fixed-response,FixedResponseConfig={StatusCode=403,ContentType=text/plain,MessageBody=forbidden}' \
  --region eu-central-1 --profile default
```

Harden Option 2 (do all three):
- Attach **AWS WAF** to the ALB: a **rate-based rule** on `/mcp*` (e.g. 100 req / 5 min per IP)
  and optionally geo-match to your countries — defense-in-depth against token leakage/bruteforce.
- The `X-MCP-Token` value is visible to anyone with `elbv2:DescribeRules`; **rotate it** on a
  schedule and on any team change.
- TLS is already enforced (HTTPS listener), so the header is never sent in clear.

Common to both options:
- MCP task SG `<MCP_SG_ID>` must allow 8001 **only** from the fronting ALB's SG (internal ALB
  SG for Option 1, public ALB SG for Option 2).
- Health-check `/mcp/` uses a permissive `200-499` matcher because a bare GET to an MCP
  streamable-http endpoint often returns 4xx while still being "up". Confirm against logs.
- **Kill switch:** disabling the dedicated `mcp-service` Superset account instantly revokes ALL
  MCP access regardless of network path — the fastest incident response.

## 5. Verify + smoke test

```bash
aws ecs describe-services --cluster superset-cluster-01 --services superset-mcp-service \
  --region eu-central-1 --profile default \
  --query 'services[0].{running:runningCount,desired:desiredCount,events:events[0:3].message}'
# tail logs:
aws logs tail /ecs/superset-mcp --since 10m --follow --region eu-central-1 --profile default

# Option 2: request WITHOUT the secret header -> expect 403:
curl -i https://baw-superset.kwsdcloud.eu/mcp/
# WITH the header (Option 2), or over VPN to the internal ALB (Option 1), initialize round-trips:
curl -i https://baw-superset.kwsdcloud.eu/mcp/ \
  -H 'X-MCP-Token: <LONG_RANDOM_SECRET>' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
```

Best end-to-end check: point an MCP client (Claude Desktop / Claude Code) at the endpoint —
for **Option 2** set a custom header `X-MCP-Token: <secret>` on the remote MCP connection; for
**Option 1** connect while on the VPN — and confirm the tool list loads and e.g. "list
dashboards" returns.

## Rollback
Additive and isolated: `aws ecs update-service ... --desired-count 0` (or delete the service),
and delete the two `/mcp` listener rules. Superset web is untouched throughout.
