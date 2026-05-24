# CyberRange — Lab Orchestration Platform

> **WIP branch `feature/mainwebsite-ui-updates`:** Work in progress — **do not merge to `main`** without owner approval. See [WIP_BRANCH_NOTICE.md](./WIP_BRANCH_NOTICE.md).

> **PROPRIETARY & CONFIDENTIAL** — Internal use by authorised CySTAR / CeSTA personnel only.
> IIT Madras — RISE Research Labs

CyberRange is a multi-tenant lab orchestration engine that provisions isolated AWS environments for cybersecurity research and training. It manages the complete lifecycle of complex lab topologies — from user authentication through infrastructure provisioning, VPN routing, course-scoped access management, credential auditing, and automatic cleanup.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [System Components](#system-components)
- [Process Isolation](#process-isolation)
- [Role Model](#role-model)
- [Authentication & Security](#authentication--security)
- [Token Audit Trail](#token-audit-trail)
- [Course Admin Model](#course-admin-model)
- [Lab Deployment Flow](#lab-deployment-flow)
- [Lab Cleanup Flow](#lab-cleanup-flow)
- [Deployment Status Lifecycle](#deployment-status-lifecycle)
- [Database Schema](#database-schema)
- [API Reference](#api-reference)
- [Guardrail System](#guardrail-system)
- [Monitoring & Alerting](#monitoring--alerting)
- [Lab Blueprints](#lab-blueprints)
- [Database Migrations](#database-migrations)
- [Prerequisites](#prerequisites)
- [Setup Guide](#setup-guide)
- [Configuration Reference](#configuration-reference)
- [Running the Platform](#running-the-platform)
- [Management CLI](#management-cli)
- [Frontend Integration Guide](#frontend-integration-guide)
- [Troubleshooting](#troubleshooting)
- [Internal Development Policy](#internal-development-policy)

---

## Architecture Overview

```mermaid
flowchart TD
    User([User Device]) <-->|HTTPS + JWT| API(FastAPI Backend)
    API <-->|AsyncSession| DB[(PostgreSQL 16)]
    API <-->|jti blocklist| Redis[(Redis)]

    API -->|INSERT queued| DB
    Worker["lab_worker\n(separate process)"] <-->|FOR UPDATE SKIP LOCKED| DB
    CleanupWorker["lab_cleanup_worker\n(separate process)"] <-->|FOR UPDATE SKIP LOCKED| DB

    API <-->|HTTP REST + Bearer| HS[Headscale Server]
    Worker <-->|mint preauth key| HS
    CleanupWorker <-->|delete nodes| HS

    Worker -->|exec deploy-lab.sh| TF[Terraform CLI]
    CleanupWorker -->|exec destroy-lab.sh| TF

    Worker -->|store preauth key| SSM[(AWS SSM\nParameter Store)]

    subgraph AWS Infrastructure
        TF --> VPC[VPC + Tenant Subnet]
        VPC --> DC[Domain Controller]
        VPC --> Client[Domain Client]
        VPC --> Kali[Kali Attack Machine]
        VPC --> Wazuh[Wazuh Manager]
        VPC --> Router[Subnet Router EC2]
        Router -->|fetch key at boot via IMDSv2| SSM
    end

    TF -->|workspace state| S3[(S3 + DynamoDB Lock)]

    Router <-->|tailscale up + advertise routes| HS
    User <-->|tailscale up + accept routes| HS
    User <-->|reach VPC private IPs via tunnel| Router

    API -->|custom metrics every 30s| CW[CloudWatch]
    CW -->|alarm breach| SNS[SNS Topic]
    SNS -->|email| Ops([Ops Team])
```

---

## System Components

```mermaid
graph LR
    subgraph Backend Processes
        API[FastAPI API\nPort 8000]
        LW[lab_worker]
        CLW[lab_cleanup_worker]
    end

    subgraph Storage
        PG[(PostgreSQL 16)]
        RD[(Redis 7)]
        S3[(S3 Remote State)]
        DDB[(DynamoDB Lock)]
    end

    subgraph AWS
        SSM[SSM Parameter Store]
        EC2[EC2 Instances]
        IAM[IAM Instance Profiles]
        CWM[CloudWatch Metrics]
    end

    subgraph Networking
        HS[Headscale Server]
        TS[Tailscale Clients]
    end

    API --> PG
    API --> RD
    LW --> PG
    CLW --> PG
    LW --> SSM
    LW --> EC2
    LW --> HS
    CLW --> HS
    API --> CWM
    EC2 --> SSM
    EC2 <--> HS
    TS <--> HS
```

---

## Process Isolation

The platform runs as **three completely separate OS processes**. A crash in one process cannot affect the others. All three are managed by systemd with `Restart=on-failure`.

| Process | Purpose | Systemd Unit |
|---|---|---|
| `cyberrange-api` | Handles all HTTP requests | `cyberrange-api.service` |
| `cyberrange-lab-worker` | Provisions labs via Terraform apply | `cyberrange-lab-worker.service` |
| `cyberrange-lab-cleanup-worker` | Destroys expired labs via Terraform destroy | `cyberrange-lab-cleanup-worker.service` |

---

## Role Model

CyberRange uses a three-tier role hierarchy. Every authenticated request is checked against this model.

```mermaid
graph TD
    SA[sys_admin] -->|full platform access| Everything
    SA -->|manages| CA[course_admin]
    SA -->|manages| P[participant]
    CA -->|scoped to assigned courses| Courses[course endpoints]
    CA -->|manages participants| CP[course_participants]
    P -->|view assigned deployments| Status[GET labs/status]
    P -->|join assigned deployment| Join[POST labs/join]

    SA -->|deploy any lab| Deploy1[POST labs/deploy]
    CA -->|deploy within guardrails| Deploy2[POST course/id/deploy]

    P -->|blocked 403| Deploy1
    P -->|blocked 403| Deploy2
    P -->|blocked 403| Admin[admin endpoints]
    P -->|blocked 403| CourseAdmin[course endpoints]

    style Deploy1 fill:#fee,stroke:#f99
    style Deploy2 fill:#fee,stroke:#f99
    style Admin fill:#fee,stroke:#f99
    style CourseAdmin fill:#fee,stroke:#f99
```

### Role definitions

| Role | Who | What they can do |
|---|---|---|
| `sys_admin` | Platform administrators | Everything — deploy any lab, manage all users, assign course_admins, set guardrails |
| `course_admin` | Professors / instructors | Deploy labs within assigned courses, manage participants for their courses only |
| `participant` | Students / researchers | View and join deployments they have been added to |

### Role constants

Defined in `backend/config.py` — import these everywhere, never hardcode role strings:

```python
ROLE_SYS_ADMIN    = "sys_admin"
ROLE_COURSE_ADMIN = "course_admin"
ROLE_PARTICIPANT  = "participant"
```

### Setting roles

Roles are set via the management CLI (first sys_admin) or via `POST /admin/users/{id}/role` (subsequent changes). There is no bootstrap HTTP endpoint — role promotion requires shell access to the server.

```bash
# Promote a user to sys_admin (requires shell access)
python3 -m backend.cli promote-admin their@email.com
```

---

## Authentication & Security

### Authentication flow

```mermaid
sequenceDiagram
    actor Browser
    participant Google
    participant API
    participant DB
    participant Redis

    Browser->>Google: Sign in with Google
    Google-->>Browser: Google ID token
    Browser->>API: POST /auth/sso/callback {provider, id_token}
    API->>Google: verify_oauth2_token(token, GOOGLE_CLIENT_ID)
    Google-->>API: Verified payload {sub, email, email_verified}
    API->>DB: UPSERT users (provider, subject, email)
    API->>DB: INSERT token_audit_log (event=issued, jti, ip)
    API-->>Browser: {access_token: "eyJ...", token_type: "bearer"}

    Browser->>API: Any authenticated request + Bearer token
    API->>API: Decode + validate JWT (iss, aud, exp, nbf, sig)
    API->>Redis: EXISTS jti:revoked:{jti}
    Redis-->>API: 0 (not revoked)
    API->>DB: SELECT user WHERE id=sub AND is_active=true
    API-->>Browser: Response

    Browser->>API: POST /auth/logout
    API->>Redis: SETEX jti:revoked:{jti} {remaining_ttl} 1
    API->>DB: INSERT token_audit_log (event=revoked, jti, ip)
    API-->>Browser: 204 No Content
```

### Security properties

| Property | Implementation |
|---|---|
| Google token verification | Server-side via `google-auth` library with `GOOGLE_CLIENT_ID` audience check |
| JWT claims | `iss`, `aud`, `iat`, `nbf`, `jti`, `exp` all set and validated on every request |
| JWT secret strength | Minimum 32 characters enforced at startup via pydantic validator |
| Token revocation | Redis blocklist keyed by `jti`, TTL = remaining token lifetime — no unbounded growth |
| `is_active` check | Every authenticated request re-checks DB — disabled users blocked immediately |
| SQL injection | All queries use SQLAlchemy parameterised `text()` — no string formatting in SQL |
| Headscale keys | SHA-256 hash only stored in DB — raw key value stored in AWS SSM SecureString |
| EC2 metadata | IMDSv2 enforced on all instances (`http_tokens = required`) |
| EBS encryption | Enabled on all volumes |
| Public IPs | `map_public_ip_on_launch = false` on all lab subnets |
| IAM | Instance profiles scoped to exact SSM parameter path per deployment |
| Intra-lab traffic | All user traffic flows through Tailscale tunnel — lab machines not publicly reachable |
| CORS | Configurable allowlist in `CORS_ALLOWED_ORIGINS` — never use wildcard in production |
| Rate limiting | Per-endpoint slowapi limits, proxy-aware via `TRUSTED_PROXY_IPS` |
| Swagger UI | Disabled by default (`ENABLE_DOCS=false`) — enable only for local dev |

---

## Token Audit Trail

Every credential event is recorded in `token_audit_log`. This was implemented in response to a prior IIT Madras incident involving credential misuse.

```mermaid
sequenceDiagram
    participant Event
    participant AuditWriter
    participant DB

    Note over Event: User logs in
    Event->>AuditWriter: log_token_event(event=issued, jti, user_id, ip)
    AuditWriter->>DB: INSERT token_audit_log

    Note over Event: User logs out
    Event->>AuditWriter: log_token_event(event=revoked, jti, user_id, ip)
    AuditWriter->>DB: INSERT token_audit_log

    Note over Event: User joins a deployment
    Event->>AuditWriter: log_token_event(event=join_key_issued, jti=authkey, user_id, ip)
    AuditWriter->>DB: INSERT token_audit_log
```

### Audit events

| Event | When | `jti` field contains |
|---|---|---|
| `issued` | User completes Google SSO login | JWT `jti` claim (UUID) |
| `revoked` | User calls `POST /auth/logout` | Same JWT `jti` as issued |
| `join_key_issued` | User calls `POST /labs/join/{id}` | Headscale preauth key string |

### Querying the audit log

```sql
-- Full credential lifecycle for a user
SELECT event, jti, ip_address, user_agent, created_at
FROM token_audit_log
WHERE user_id = 'user-uuid-here'
ORDER BY created_at DESC;

-- All join keys issued in the last 24 hours
SELECT u.email, tal.jti, tal.ip_address, tal.created_at
FROM token_audit_log tal
JOIN users u ON tal.user_id = u.id
WHERE tal.event = 'join_key_issued'
  AND tal.created_at > now() - interval '24 hours'
ORDER BY tal.created_at DESC;

-- Issued/revoked pairs — find tokens that were never revoked
SELECT i.user_id, i.jti, i.created_at AS issued_at, r.created_at AS revoked_at
FROM token_audit_log i
LEFT JOIN token_audit_log r
    ON i.jti = r.jti AND r.event = 'revoked'
WHERE i.event = 'issued'
ORDER BY i.created_at DESC;
```

> **Note:** `ip_address` is currently recorded as `request.client.host`. When a reverse proxy is deployed in production, update the audit wiring to extract the real client IP from `X-Forwarded-For` using the existing trusted proxy logic in `backend/limiter.py`.

---

## Course Admin Model

`course_admin` users are scoped to specific courses. They cannot see or interact with courses they are not assigned to.

```mermaid
flowchart TD
    SA[sys_admin] -->|POST /admin/courses| CI[content_items\ncourse created]
    SA -->|POST /admin/courses/id/admins/uid| CAA[course_admin_assignments\ncourse_admin assigned]
    SA -->|POST /admin/courses/id/guardrails/uid| CG[course_guardrails\nlimits set]

    CA[course_admin] -->|POST /course/id/participants/uid| CP[course_participants\nparticipant enrolled]
    CA -->|POST /course/id/deploy| LD[lab_deployments\nqueued]
    LD -->|auto bulk insert| DM[deployment_members\nall enrolled participants added]

    P[participant] -->|GET /labs/status| View[sees assigned deployments]
    P -->|POST /labs/join/id| Join[gets Tailscale join key]
```

### Guardrail enforcement

When a `course_admin` deploys, two guardrails are checked in order:

```mermaid
flowchart TD
    A[POST /course/id/deploy] --> B{course_admin\nassigned to course?}
    B -->|No| E1[403 Not assigned]
    B -->|Yes| C{expires_at <=\nnow + max_duration_hours?}
    C -->|No| E2[400 Duration exceeds limit]
    C -->|Yes| D{active deployments <\nmax_concurrent?}
    D -->|No| E3[400 Concurrent limit reached]
    D -->|Yes| F[INSERT lab_deployments]
    F --> G[Bulk INSERT deployment_members\nfor all enrolled participants]
    G --> H[200 queued]
```

If no guardrail row exists for a `course_admin`+`content_id` pair, the system-wide defaults apply:

```python
GUARDRAIL_DEFAULT_MAX_CONCURRENT     = 10   # backend/config.py
GUARDRAIL_DEFAULT_MAX_DURATION_HOURS = 4    # backend/config.py
```

---

## Lab Deployment Flow

```mermaid
sequenceDiagram
    actor CourseAdmin
    participant API
    participant DB
    participant Worker
    participant Headscale
    participant SSM
    participant Terraform
    participant EC2

    CourseAdmin->>API: POST /course/{id}/deploy {expires_at}
    API->>DB: Check assignment + guardrails
    API->>DB: INSERT lab_deployments (queued)
    API->>DB: INSERT deployment_members (all enrolled participants)
    API-->>CourseAdmin: {deployment_id, status: queued, participants_added: N}

    Worker->>DB: Poll FOR UPDATE SKIP LOCKED
    DB-->>Worker: Claim job → status=provisioning
    Worker->>DB: Allocate tenant subnet (/24)
    Worker->>Headscale: Ensure user identity exists
    Worker->>Headscale: Mint router preauth key
    Worker->>SSM: Store key as SecureString
    Worker->>Terraform: exec deploy-lab.sh {lab_type, deployment_id}
    Terraform->>EC2: Launch 5 instances in tenant subnet
    EC2->>SSM: Fetch preauth key at boot (IMDSv2 + IAM role)
    EC2->>Headscale: tailscale up --authkey=...
    Headscale-->>EC2: Registered, routes advertised
    Worker->>DB: UPDATE status=running, store IPs
```

---

## Lab Cleanup Flow

```mermaid
sequenceDiagram
    participant CleanupWorker
    participant DB
    participant Terraform
    participant Headscale
    participant SSM

    CleanupWorker->>DB: Poll WHERE expires_at <= now() FOR UPDATE SKIP LOCKED
    DB-->>CleanupWorker: Claim job → status=terminating
    CleanupWorker->>Terraform: exec destroy-lab.sh {lab_type, deployment_id}
    Terraform-->>CleanupWorker: All EC2 instances terminated
    CleanupWorker->>Headscale: Delete subnet router node
    CleanupWorker->>SSM: Delete preauth key parameter
    CleanupWorker->>DB: UPDATE status=expired
```

> **SSM key lifecycle:** The preauth key is **not** deleted immediately after Terraform apply. The subnet router fetches it at boot — deleting it early causes the router to fail to join Headscale silently. The cleanup worker handles deletion after the lab expires.

---

## Deployment Status Lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: POST /labs/deploy or POST /course/id/deploy
    queued --> provisioning: lab_worker claims job
    provisioning --> running: Terraform apply succeeds
    provisioning --> failed: Terraform apply fails
    running --> terminating: expires_at <= now()
    failed --> queued: sys_admin manually re-queues
    terminating --> expired: Terraform destroy succeeds
    terminating --> cleanup_failed: Terraform destroy fails
    cleanup_failed --> terminating: retry on next cleanup poll
```

---

## Database Schema

```mermaid
erDiagram
    users ||--o{ lab_deployments : "owns"
    users ||--o{ deployment_members : "is member of"
    users ||--o{ token_audit_log : "has audit trail"
    users ||--o{ course_admin_assignments : "assigned to courses"
    users ||--o{ course_participants : "enrolled in courses"
    users ||--o{ course_guardrails : "has guardrails"
    content_items ||--o{ lab_deployments : "deployed as"
    content_items ||--o{ course_admin_assignments : "has admins"
    content_items ||--o{ course_participants : "has participants"
    content_items ||--o{ course_guardrails : "has guardrails"
    lab_deployments ||--o{ deployment_members : "has members"

    users {
        uuid id PK
        text sso_provider
        text sso_subject
        text email
        text name
        text role
        bool is_active
        timestamptz created_at
        timestamptz updated_at
    }

    content_items {
        uuid id PK
        text type
        text title
        text description
        text difficulty
        int duration_minutes
        bool is_active
        jsonb metadata
        timestamptz created_at
    }

    lab_deployments {
        uuid id PK
        uuid user_id FK
        uuid content_id FK
        text lab_type
        text status
        text terraform_workspace
        text instance_public_ip
        text instance_private_ip
        jsonb terraform_outputs
        text error_message
        timestamptz expires_at
        timestamptz created_at
        timestamptz updated_at
    }

    deployment_members {
        uuid deployment_id PK_FK
        uuid user_id PK_FK
        uuid added_by FK
        timestamptz added_at
    }

    token_audit_log {
        uuid id PK
        uuid user_id FK
        text jti
        text event
        text ip_address
        text user_agent
        timestamptz created_at
    }

    course_admin_assignments {
        uuid user_id PK_FK
        uuid content_id PK_FK
        uuid assigned_by FK
        timestamptz assigned_at
    }

    course_guardrails {
        uuid id PK
        uuid course_admin_id FK
        uuid content_id FK
        int max_concurrent_deployments
        int max_duration_hours
        uuid set_by FK
        timestamptz updated_at
    }

    course_participants {
        uuid user_id PK_FK
        uuid content_id PK_FK
        uuid enrolled_by FK
        timestamptz enrolled_at
    }

    worker_status {
        text id PK
        timestamptz last_seen
    }
```

---

## API Reference

### Authentication (`/auth`)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/auth/sso/callback` | None | Exchange Google ID token for CyberRange JWT. Writes `issued` audit event. |
| `POST` | `/auth/logout` | JWT | Revoke current token via Redis blocklist. Writes `revoked` audit event. |
| `GET` | `/auth/me` | JWT | Return current authenticated user with role. |
| `GET` | `/auth/admin/users` | `sys_admin` | List all user accounts with roles. |
| `POST` | `/auth/admin/users/{id}/disable` | `sys_admin` | Deactivate a user account. |

### Labs (`/labs`)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/labs/deploy` | `sys_admin` | Queue a lab deployment with explicit `expires_at`. No guardrail limits. |
| `GET` | `/labs/status` | Any | List deployments visible to current user. Owners see IPs; members see status only. |
| `POST` | `/labs/join/{deployment_id}` | Any | Mint a 15-minute Headscale preauth key. Writes `join_key_issued` audit event. |
| `POST` | `/labs/admin/deployments/{id}/members/{uid}` | `sys_admin` | Add a participant to a deployment. |
| `DELETE` | `/labs/admin/deployments/{id}/members/{uid}` | `sys_admin` | Remove a participant from a deployment. |
| `GET` | `/labs/admin/deployments/{id}/members` | `sys_admin` | List all participants in a deployment. |
| `GET` | `/labs/admin/all` | `sys_admin` | List all deployments across all users with participant counts. |

### Catalog (`/catalog`)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/catalog/labs` | None | Public marketing list of labs. Returns only courses with **`visibility = public`** (plus `type = lab`, `is_active`, etc.). See **Admin panel — lab visibility** below. |

### Admin (`/admin`)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/admin/courses` | `sys_admin` | Create a new course (`content_items`, type `lab`). Body includes `lab_type` in metadata; optional **`visibility`**: `public` \| `unlisted` \| `private` (default **`public`**). |
| `GET` | `/admin/courses` | `sys_admin` | List all courses; each row includes **`visibility`**. |
| `PATCH` | `/admin/courses/{content_id}` | `sys_admin` | Update **`visibility`** only (no need to recreate the course). |
| `POST` | `/admin/courses/{id}/admins/{uid}` | `sys_admin` | Assign a `course_admin` to a course. Promotes participant to course_admin automatically. |
| `DELETE` | `/admin/courses/{id}/admins/{uid}` | `sys_admin` | Remove a `course_admin` assignment. Demotes to participant if no remaining assignments. |
| `GET` | `/admin/courses/{id}/admins` | `sys_admin` | List all `course_admins` for a course with their guardrail settings. |
| `POST` | `/admin/courses/{id}/guardrails/{uid}` | `sys_admin` | Set or update guardrails for a `course_admin` on a course. |
| `POST` | `/admin/users/{id}/role` | `sys_admin` | Set any user's role directly. sys_admin cannot demote themselves. |

### Admin panel — lab visibility (`content_items.visibility`)

Operators set **visibility** in the **Admin → Courses** UI (or via `POST` / `PATCH` above). The column lives on **`content_items`** (Alembic `0005`). It controls **discovery in the public catalog** (`GET /catalog/labs`). It does **not** replace payments, entitlements, or `course_admin` / participant flows — those stay as elsewhere in this README.

#### APIs (existing vs new)

| Endpoint | Notes |
|---|---|
| `GET /catalog/labs` | **Existing route**, behavior: response includes **only** `visibility = public` labs. |
| `POST /admin/courses` | **Existing route**, body extended with optional **`visibility`**. |
| `GET /admin/courses` | **Existing route**, response extended with **`visibility`**. |
| `PATCH /admin/courses/{content_id}` | **New route** — change visibility after create. |

#### What each value means (simple)

| Value | Shows on **Labs** browse (`GET /catalog/labs`)? | Idea |
|---|---|---|
| **`public`** | **Yes** | Normal product: anyone can **see** it in the catalog; **buy/deploy** still needs login + your usual rules (checkout, entitlements, etc.). |
| **`unlisted`** | **No** | **Not advertised** on the public list. You share a **direct link** (e.g. purchase page with that lab’s id). After **SSO**, the user follows the **same** payment/entitlement rules you already have — visibility only hid the **shop shelf**, not your business rules. |
| **`private`** | **No** | Same **listing** behavior as unlisted (hidden from browse). **Intent:** later, **invite-only** via a **per-user grant** list (not yet enforced by `visibility` alone). Until grants exist, use **`course_admin` / participants** or process discipline for who gets the link. |

#### Why `unlisted` vs `private` if both hide the catalog?

- **`unlisted`** = “don’t put it on the public shelf; otherwise treat like any other lab (pay → entitlement, etc.).”
- **`private`** = “closed cohort; we **plan** URL + identity checks so only invited users pass.” Same **catalog** filter as unlisted **today**; the extra **door check** is a future **grants** feature.

#### Examples (same company, three labs)

1. **`public`** — “AD Basics” appears when a visitor opens **Labs**. They sign in, pay, get entitlement, deploy.  
2. **`unlisted`** — “Partner promo lab” does **not** appear on **Labs**. Sales emails a link to `/purchase/…` (or your app route) with that `content_id`. User signs in, pays — **normal checkout** — never saw it on the public list.  
3. **`private`** — “Acme Corp workshop” does **not** appear on **Labs**. You assign **`course_admin`** and **participants** (or, when built, **grants**) so only that cohort is meant to access; you still distribute links out-of-band.

> Lab visibility + catalog filter + course `PATCH` — documented by `pb`.

### Course (`/course`)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/course/my-courses` | `course_admin` | List courses assigned to the current user with active guardrail settings. |
| `POST` | `/course/{id}/deploy` | `course_admin` | Deploy a lab within guardrail limits. Auto-adds all enrolled participants. |
| `POST` | `/course/{id}/participants/{uid}` | `course_admin` | Enroll a participant in a course. |
| `DELETE` | `/course/{id}/participants/{uid}` | `course_admin` | Unenroll a participant from a course. |
| `GET` | `/course/{id}/participants` | `course_admin` | List all enrolled participants. |

### Tailnet (`/tailnet`)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/tailnet/join-token` | Any | Mint a short-lived Headscale device key for the current user. Requires active entitlement. |

### Health (`/health`)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Liveness probe — always 200 if process is up. |
| `GET` | `/health/ready` | None | Readiness probe — checks DB connectivity. |
| `GET` | `/health/workers` | None | Worker heartbeat status. Returns 503 if any worker is stale (>60s). |

---

## Guardrail System

Guardrails limit what a `course_admin` can deploy. They are set per `course_admin` per course by a `sys_admin`.

| Guardrail | Default | Maximum allowed | What it controls |
|---|---|---|---|
| `max_concurrent_deployments` | 10 | 50 | How many active (`queued`+`provisioning`+`running`) deployments this `course_admin` can have for this course at once |
| `max_duration_hours` | 4 | 72 | How far in the future `expires_at` can be set |

Defaults are defined in `backend/config.py` and apply automatically when no guardrail row exists for a given `course_admin`+`content_id` pair.

To override for a specific course_admin:

```bash
curl -X POST /admin/courses/{content_id}/guardrails/{user_id} \
  -H "Authorization: Bearer <sys_admin_token>" \
  -H "Content-Type: application/json" \
  -d '{"max_concurrent_deployments": 5, "max_duration_hours": 8}'
```

---

## Monitoring & Alerting

### CloudWatch metrics

The API publishes custom metrics to CloudWatch every 30 seconds via a background asyncio task:

| Namespace | Metric | Dimension | Description |
|---|---|---|---|
| `CyberRange/Workers` | `WorkerHeartbeatAge` | `WorkerId` | Seconds since last heartbeat for each worker |
| `CyberRange/API` | `Api5xxCount` | — | Count of 5xx responses |
| `CyberRange/Workers` | `TerraformFailureCount` | — | Count of Terraform provisioning failures |

### Alarms

| Alarm | Threshold | Action |
|---|---|---|
| Worker stale | Heartbeat age > 60s for 2 consecutive periods | Email via SNS |
| API 5xx rate | > 10 errors in 5 minutes | Email via SNS |
| Terraform failures | > 3 failures in 5 minutes | Email via SNS |

### Deploy monitoring infrastructure

```bash
cd monitoring
terraform init
terraform apply -var="alert_email=ops@yourorg.com"
```

> Confirm the SNS subscription after apply — check your inbox for the AWS confirmation email and click the link. Without this, alarms fire but no emails are sent.

### Health check commands

```bash
curl http://localhost:8000/health           # liveness
curl http://localhost:8000/health/ready     # DB reachability
curl http://localhost:8000/health/workers   # worker heartbeats + ages
```

Expected healthy worker response:
```json
{
  "status": "ok",
  "workers": [
    {"id": "lab_worker", "age_seconds": 3.2, "status": "ok"},
    {"id": "lab_cleanup_worker", "age_seconds": 1.8, "status": "ok"}
  ]
}
```

---

## Lab Blueprints

### Lab 1 — Windows Active Directory (`lab-1/`)

```mermaid
graph TD
    subgraph VPC [AWS VPC - Tenant Subnet /24]
        DC[Domain Controller<br/>10.x.x.10<br/>Windows Server 2022]
        Client[Domain Client<br/>10.x.x.20<br/>Windows 10]
        Wazuh[Wazuh Manager<br/>10.x.x.30<br/>Ubuntu 22.04]
        Kali[Kali Attack Machine<br/>10.x.x.40<br/>Kali Linux]
        Router[Subnet Router<br/>10.x.x.95<br/>Ubuntu + Tailscale]
    end

    Router <-->|Tailscale tunnel| User([User Device])
    User -->|RDP 3389| DC
    User -->|RDP 3389| Client
    User -->|HTTPS 443| Wazuh
    User -->|SSH 22| Kali
    Kali -->|attack traffic| DC
    Kali -->|attack traffic| Client
    DC -->|Wazuh agent| Wazuh
    Client -->|Wazuh agent| Wazuh
```

| Instance | Role | Private IP | User Access |
|---|---|---|---|
| Domain Controller | AD DS + DNS | `subnet.10` | RDP 3389 |
| Domain Client | AD member workstation | `subnet.20` | RDP 3389 |
| Wazuh Manager | SIEM + EDR | `subnet.30` | HTTPS 443, API 55000 |
| Kali Attack Machine | Offensive tooling | `subnet.40` | SSH 22 |
| Subnet Router | Tailscale VPN gateway | `subnet.95` | — (internal only) |

### Lab 2 — Linux Target (`lab-2/`)

Placeholder for future lab content. Terraform module exists but is not currently wired to the provisioning worker.

---

## Database Migrations

All schema changes use **Alembic** with an async SQLAlchemy engine. Never apply schema changes via raw psql in production — always create a migration file.

### Migration history

| Revision | Description |
|---|---|
| `0001` | Baseline — all schema applied before Alembic was introduced |
| `0002` | Course admin model — `course_admin_assignments`, `course_guardrails`, `course_participants` |
| `0003` | Drop `unique_lab_type_idx` to allow multiple courses per lab type |

### Common commands

```bash
# Apply all pending migrations
alembic upgrade head

# Check current revision
alembic current

# View full history
alembic history --verbose

# Roll back one revision
alembic downgrade -1

# Generate a new migration (after adding ORM models)
alembic revision --autogenerate -m "description"
```

### Creating a new migration manually

```python
# alembic/versions/0004_your_description.py
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""
        CREATE TABLE your_table (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ...
        )
    """)

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS your_table")
```

---

## Prerequisites

### Host requirements

| Requirement | Version |
|---|---|
| OS | Ubuntu 22.04 / 24.04 |
| Python | 3.12+ |
| PostgreSQL | 16+ |
| Redis | 7+ |
| Terraform | 1.5+ |
| AWS CLI | v2 (must be v2 — v1 will fail) |
| Network | HTTPS access to Headscale server |

### AWS resources (must exist before first deploy)

| Resource | Purpose |
|---|---|
| S3 bucket `cyberrange-tfstate-prod-ap-south-1` | Terraform remote state (versioning + encryption enabled) |
| DynamoDB table `cyberrange-tfstate-lock` | Terraform state locking (`LockID` partition key, String type) |
| VPC with `/16` CIDR space | Lab subnet allocation (`10.20.0.0/16` recommended) |
| EC2 key pairs | SSH/RDP access to lab instances |
| Golden AMIs | Pre-baked images for each machine type (see below) |

### Golden AMI requirements

The **subnet router AMI** must have these baked in:

| Requirement | Check command |
|---|---|
| `tailscale` installed and `tailscaled` enabled | `systemctl status tailscaled` |
| `subnet-router-join.service` systemd unit | `systemctl status subnet-router-join` |
| `/usr/local/sbin/subnet-router-join.sh` | `cat /usr/local/sbin/subnet-router-join.sh` |
| `netfilter-persistent` installed | `dpkg -l netfilter-persistent` |
| **AWS CLI v2** at `/usr/local/bin/aws` | `aws --version` → must show `aws-cli/2.x.x` |

> **Critical:** AWS CLI v2 must be baked into the subnet router AMI. The user_data script fetches the Headscale auth key from SSM at boot using the instance's IAM role. Without CLI v2, the router will silently fail to join Headscale with no visible error.

---

## Setup Guide

### 1. Clone and create Python environment

```bash
git clone <repo-url> cyberrange-infra
cd cyberrange-infra
python3 -m venv env && source env/bin/activate
pip install -r requirements.txt
```

### 2. Install and start Redis

Redis is required for JWT token revocation. The API will refuse to start without it.

```bash
sudo apt-get install -y redis-server
sudo systemctl enable --now redis
redis-cli ping   # must return PONG
```

### 3. Configure environment

```bash
cp backend/.env.example backend/.env
# Edit backend/.env — see Configuration Reference below
```

### 4. Initialise the database

```bash
createdb cyberrange
psql -d cyberrange -f backend/infrastructure/db_schema.sql
alembic upgrade head
psql -d cyberrange -c "INSERT INTO subnet_tracker (id, last_assigned_octet) VALUES ('counter', 1) ON CONFLICT DO NOTHING;"
```

### 5. Initialise Terraform remote state

```bash
cd lab-1 && terraform init
cd ../lab-2 && terraform init
```

### 6. Configure Headscale

Generate an API key on the Headscale server:

```bash
sudo headscale apikeys create --expiration 999d
```

Add it to `backend/.env` as `HEADSCALE_API_KEY`.

### 7. Create your first sys_admin

Start the API (see Running the Platform), then log in with Google once to create your user account. Then run:

```bash
python3 -m backend.cli promote-admin your@email.com
```

Log in again to get a fresh JWT with `role: sys_admin`.

### 8. Deploy monitoring infrastructure

```bash
cd monitoring
terraform init
terraform apply -var="alert_email=your@email.com"
```

Confirm the SNS email subscription — check your inbox for the AWS confirmation and click the link.

---

## Configuration Reference

All variables are validated at startup via `pydantic-settings`. The process exits immediately with a clear error if any required variable is missing.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | ✅ | — | `postgresql+asyncpg://user:pass@localhost/cyberrange` |
| `JWT_SECRET` | ✅ | — | Min 32 chars. Generate: `openssl rand -hex 32` |
| `GOOGLE_CLIENT_ID` | ✅ | — | From Google Cloud Console → OAuth 2.0 Credentials |
| `HEADSCALE_API_URL` | ✅ | — | `https://your-headscale.example.com` |
| `HEADSCALE_API_KEY` | ✅ | — | From `sudo headscale apikeys create` |
| `REDIS_URL` | ✅ | `redis://localhost:6379/0` | Redis connection string |
| `AWS_REGION` | ✅ | `ap-south-1` | AWS region for all resources |
| `CORS_ALLOWED_ORIGINS` | ✅ | `[]` | e.g. `["https://app.cyberrange.io"]` |
| `ENABLE_DOCS` | ❌ | `false` | Set `true` for local dev only — never in production |
| `RATE_LIMIT_AUTH` | ❌ | `10/minute` | Rate limit for `/auth/sso/callback` |
| `RATE_LIMIT_DEPLOY` | ❌ | `5/minute` | Rate limit for deploy endpoints |
| `RATE_LIMIT_TAILNET` | ❌ | `10/minute` | Rate limit for tailnet endpoints |
| `TRUSTED_PROXY_IPS` | ❌ | `[]` | IPs to trust `X-Forwarded-For` from |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | ❌ | `120` | JWT lifetime in minutes |

> **Redis TLS:** For AWS ElastiCache use `rediss://` (double-s): `rediss://your-cluster.cache.amazonaws.com:6380/0`

---

## Running the Platform

All three processes must be running. Always run from the **project root**.

### Development (three terminals)

```bash
# Terminal 1 — API
source env/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — Lab provisioning worker
source env/bin/activate
python3 -m backend.workers.run_lab_worker

# Terminal 3 — Lab cleanup worker
source env/bin/activate
python3 -m backend.workers.run_lab_cleanup_worker
```

> ⚠️ **Always use `python3 -m backend.workers.run_lab_worker`** from the project root. Never `cd backend/workers && python3 run_lab_worker.py` — this breaks all relative imports.

### Production (systemd)

```bash
sudo cp backend/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cyberrange-api
sudo systemctl enable --now cyberrange-lab-worker
sudo systemctl enable --now cyberrange-lab-cleanup-worker

# Verify all three are running
sudo systemctl status cyberrange-api cyberrange-lab-worker cyberrange-lab-cleanup-worker
```

---

## Management CLI

The management CLI provides administrative operations that require direct shell access to the server. There is no HTTP endpoint for these operations.

```bash
# Promote a user to sys_admin
python3 -m backend.cli promote-admin their@email.com

# List all users with their roles
python3 -m backend.cli list-users
```

Example `list-users` output:
```
email                                    role            active   id
------------------------------------------------------------------------------------------
umasatyanarayana35@gmail.com             sys_admin       True     e1f343a1-a7ee-43be-...
shamilireddy340@gmail.com                participant     True     6360f798-2fdb-4c8f-...
```

---

## Frontend Integration Guide

### Base URL

```
http://localhost:8000        # development
https://api.cyberrange.io    # production
```

### Authentication flow

```mermaid
sequenceDiagram
    actor User
    participant Frontend
    participant Google
    participant API

    User->>Frontend: Click Sign in with Google
    Frontend->>Google: Initialize GSI SDK
    Google-->>Frontend: Google ID token
    Frontend->>API: POST /auth/sso/callback\n{provider: "google", id_token: "..."}
    API-->>Frontend: {access_token: "eyJ...", token_type: "bearer"}
    Frontend->>API: GET /auth/me
    API-->>Frontend: {id, email, role, is_active}
    Note over Frontend: Route user to role-appropriate UI
```

**Load Google Identity Services:**
```html
<script src="https://accounts.google.com/gsi/client" async></script>
```

**Initialise and handle sign-in:**
```javascript
google.accounts.id.initialize({
  client_id: "YOUR_GOOGLE_CLIENT_ID",
  callback: async (response) => {
    const res = await fetch("/auth/sso/callback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        provider: "google",
        id_token: response.credential,
      }),
    });
    const { access_token } = await res.json();
    sessionStorage.setItem("token", access_token);
    // Redirect based on role from GET /auth/me
  },
});
google.accounts.id.renderButton(
  document.getElementById("signin-btn"),
  { theme: "outline", size: "large" }
);
```

### Participant lab flow

```javascript
const token = sessionStorage.getItem("token");
const headers = { "Authorization": `Bearer ${token}` };

// 1. See assigned deployments
const { deployments } = await (await fetch("/labs/status", { headers })).json();
const joinable = deployments.filter(d => d.can_join);

// 2. Get Tailscale join command
const { command, expires_at } = await (
  await fetch(`/labs/join/${deploymentId}`, { method: "POST", headers })
).json();
// Display command to user — they run it in their terminal:
// "sudo tailscale up --login-server=... --authkey=... --accept-routes=true"
```

### course_admin flow

```javascript
// 1. See assigned courses
const { courses } = await (await fetch("/course/my-courses", { headers })).json();

// 2. Deploy a lab
const { deployment_id, participants_added } = await (await fetch(
  `/course/${contentId}/deploy`,
  {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({ content_id: contentId, expires_at: "2026-04-06T12:00:00Z" }),
  }
)).json();

// 3. Poll until running
const poll = setInterval(async () => {
  const { deployments } = await (await fetch("/labs/status", { headers })).json();
  const lab = deployments.find(d => d.deployment_id === deployment_id);
  if (lab?.status === "running") clearInterval(poll);
}, 10000);
```

### Handle 401 globally

```javascript
// Any 401 means the token expired or was revoked — redirect to login
if (response.status === 401) {
  sessionStorage.removeItem("token");
  window.location.href = "/login";
}
```

### Logout

```javascript
await fetch("/auth/logout", {
  method: "POST",
  headers: { "Authorization": `Bearer ${token}` },
});
sessionStorage.removeItem("token");
window.location.href = "/login";
```

### Response shapes

**`GET /labs/status` — owner view**
```json
{
  "count": 1,
  "deployments": [{
    "deployment_id": "uuid",
    "status": "running",
    "is_owner": true,
    "public_ip": "1.2.3.4",
    "private_ip": "10.20.3.10",
    "error": null,
    "lab_title": "Windows AD Lab - Batch 1",
    "created_at": "2026-04-06T03:36:15Z",
    "expires_at": "2026-04-06T07:00:00Z",
    "can_join": false
  }]
}
```

**`GET /labs/status` — participant view**
```json
{
  "count": 1,
  "deployments": [{
    "deployment_id": "uuid",
    "status": "running",
    "is_owner": false,
    "public_ip": null,
    "private_ip": null,
    "error": null,
    "lab_title": "Windows AD Lab - Batch 1",
    "created_at": "2026-04-06T03:36:15Z",
    "expires_at": "2026-04-06T07:00:00Z",
    "can_join": true
  }]
}
```

**`POST /labs/join/{deployment_id}`**
```json
{
  "deployment_id": "uuid",
  "login_server": "https://your-headscale.example.com",
  "authkey": "tskey-auth-...",
  "expires_at": "2026-04-06T03:51:00Z",
  "command": "sudo tailscale up --login-server=... --authkey=... --accept-routes=true",
  "ttl_minutes": 15
}
```

---

## Troubleshooting

### Subnet router doesn't join Headscale

```bash
ssh -i your-key.pem ubuntu@ROUTER_IP
sudo cat /var/log/cloud-init-output.log | tail -30
```

| Error | Cause | Fix |
|---|---|---|
| `aws: command not found` | AWS CLI not in golden AMI | Bake AWS CLI v2 into AMI and rebuild |
| `Invalid endpoint: https://ssm..amazonaws.com` | IMDSv2 token missing — region empty | Update user_data to use IMDSv2 token for region lookup |
| `NoCredentials: Unable to locate credentials` | IAM instance profile race condition at boot | Re-run manually: `sudo bash /var/lib/cloud/instance/scripts/part-001` |
| `ParameterNotFound` | SSM key fetched after cleanup worker deleted it | Cleanup worker now handles deletion — this should not occur |
| Service `failed (resources)` | `/etc/subnet-router.env` missing | user_data script failed — check cloud-init logs above |

### Workers not starting

**Symptom:** `ModuleNotFoundError: No module named 'backend'`

```bash
# Correct — always run from project root
cd ~/cyberrange-infra
python3 -m backend.workers.run_lab_worker

# Wrong — always fails
cd backend/workers && python3 run_lab_worker.py
```

### Redis connection refused

```bash
sudo systemctl start redis
sudo systemctl enable redis
redis-cli ping   # must return PONG
```

### Deployment stuck in `provisioning`

The cleanup worker does not touch `provisioning` deployments. If the worker crashed mid-apply:

```bash
# Mark as failed so it can be re-queued
psql -d cyberrange -c "UPDATE lab_deployments SET status='failed' WHERE id='<deployment-id>';"

# If infra was partially created, destroy the workspace manually
cd lab-1
terraform workspace select ws-<deployment-id>
terraform destroy -auto-approve [vars...]
terraform workspace select default
terraform workspace delete ws-<deployment-id>
```

### Terraform state lock stuck

```bash
aws dynamodb scan \
  --table-name cyberrange-tfstate-lock \
  --region ap-south-1 \
  --query "Items[*].LockID"

cd lab-1 && terraform force-unlock <LOCK_ID>
```

### Google SSO: Access blocked / invalid_request

- Access the login page via `http://localhost:3000` not `http://0.0.0.0:3000`
- Add your email as a test user: Google Cloud Console → APIs & Services → OAuth consent screen → Test users
- Confirm `data-client_id` in your HTML matches `GOOGLE_CLIENT_ID` in `.env`
- Confirm `http://localhost:3000` is in Authorised JavaScript origins

### CloudWatch alarms not sending emails

Confirm the SNS subscription — check your inbox for "AWS Notification - Subscription Confirmation" and click the confirm link.

---

## Internal Development Policy

| Rule | Detail |
|---|---|
| **Secrets** | Never commit `.env` files, API keys, or PEM files. All secrets go in `backend/.env` which is `.gitignore`d. |
| **Migrations** | Every schema change gets an Alembic migration file. Never apply raw SQL to production directly. |
| **Terraform** | Run `terraform fmt` and `terraform validate` before pushing infrastructure changes. |
| **Remote state** | Never delete the S3 bucket or DynamoDB lock table — orphans all active lab state. |
| **Workers** | Never start workers as asyncio tasks inside the API process — always run as separate OS processes. |
| **Redis** | Must be running before starting the API. |
| **Docs endpoint** | `ENABLE_DOCS=false` in production at all times. |
| **Worker invocation** | Always `python3 -m backend.workers.run_lab_worker` from the project root. |
| **Role strings** | Always import role constants from `backend/config.py` — never hardcode `"admin"`, `"student"` etc. |
| **Audit writes** | `log_token_event()` never raises — failures are logged but must not break auth or lab access. |

---

**Proprietary Property of CySTAR Team, IIT Madras.**
*Confidentiality is our Priority.*