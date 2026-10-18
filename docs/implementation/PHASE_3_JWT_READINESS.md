# Phase 3 — JWT identity readiness (G-04)

**Status:** implementation complete, **G-04 stays `VERIFY`** until the tests and
this evidence are reviewed. **G-18 stays `VERIFY`** until CI actually runs.
PostgreSQL verification is separate and is not claimed here.

This document is the operator procedure for JWT mode and states its limits
plainly. **JWT mode is not demo-ready.**

## What Phase 3 enforces

`POST /approvals/{approval_id}/resolve` and `GET /approvals/{approval_id}` are
the only protected endpoints. With `TASKFENCE_AUTH_MODE=jwt`:

- A valid JWT is required **first**; missing/invalid ⇒ `401`, even when
  `X-Admin-Token` is correct.
- Then the existing admin authorization check runs; a valid JWT with a
  missing/invalid admin token ⇒ `403`.
- Verification uses the single configured PEM public key and a fixed algorithm
  allowlist (`RS256` by default). `alg:none` is refused at configuration time
  and again per token.
- Required claims `iss`, `aud`, `sub`, `exp`, `iat`, `jti`; zero leeway.
- The validated `sub` is recorded as **forensic attribution only**. It never
  grants authorization.

The mode is fixed by the validated startup configuration for the process
lifetime. There is no runtime switch and no fallback to legacy authentication:
an unusable configuration fails startup, and a verifier that cannot run returns
`503` rather than authenticating.

## Configuration

JWT settings are read from the process environment. **`TASKFENCE_AUTH_MODE`
must be set to `jwt` alongside them** — JWT settings present while the mode is
`local` are a startup error, never silently ignored.

| Variable | Required | Purpose |
|---|---|---|
| `TASKFENCE_AUTH_MODE` | yes (`jwt`) | Enables JWT mode |
| `TASKFENCE_JWT_PUBLIC_KEY` | yes | The single PEM public key |
| `TASKFENCE_JWT_ALLOWED_ALGS` | no (default `RS256`) | Fixed algorithm allowlist |
| `TASKFENCE_JWT_ISSUER` | yes\* | Expected `iss` |
| `TASKFENCE_JWT_AUDIENCE` | yes\* | Expected `aud` |
| `TASKFENCE_ADMIN_TOKEN` | yes | Existing authorization factor |
| `TASKFENCE_REVIEWER_JWT` | for the dashboard | Reviewer access token (see below) |

\* Falls back to `TASKFENCE_AUTH0_ISSUER` / `TASKFENCE_AUTH0_AUDIENCE` when the
dedicated variables are unset, so existing identity configuration keeps working.

Nothing in this repository loads a `.env` file (there is no `python-dotenv`
dependency and no `--env-file` flag), so export the values in the process
environment. A multi-line PEM does not survive `KEY="...\n..."`; read it from
the file instead:

```bash
export TASKFENCE_AUTH_MODE=jwt
export TASKFENCE_JWT_PUBLIC_KEY="$(cat /path/to/jwt-public.pem)"
export TASKFENCE_JWT_ALLOWED_ALGS=RS256
export TASKFENCE_JWT_ISSUER="https://YOUR_TENANT/"
export TASKFENCE_JWT_AUDIENCE="YOUR_API_AUDIENCE"
export TASKFENCE_ADMIN_TOKEN="$(cat /path/to/admin-token)"
.venv/bin/python -m uvicorn taskfence.gateway:app --port 8000
```

A misconfigured key or an allowlist containing `none` fails startup with a
clear error rather than serving requests.

## Get a reviewer token from the trusted identity provider

TaskFence does not issue or sign tokens, and does not verify anything except the
configured public key. Obtain an **access token for the configured issuer and
audience** from the identity provider you configured — for example through its
token endpoint, its CLI, or the login flow of the application it already
fronts. The token must carry the required claims; any token missing `jti`, or
with `exp` in the past, is refused.

## Give the dashboard that token

The dashboard is a separate process that calls the two protected endpoints.
Export the token **for the dashboard process only**:

```bash
export TASKFENCE_REVIEWER_JWT="$(cat /path/to/reviewer-access-token)"
```

The dashboard attaches it as `Authorization: Bearer …` server-side. The value
is never rendered, never logged, and never sent to the browser; the admin token
and the signing key stay server-side too. The dashboard does not mint tokens,
does not verify them, and has no way to bypass the gateway's verification. When
the token is expired the gateway answers `401` and the dashboard shows an
explicit message telling the operator to obtain a fresh one.

## Limitation — JWT mode is not demo-ready

There is **no in-dashboard login flow** in this phase. A human reviewer logs in
to the identity provider out of band and passes the resulting token to the
dashboard through `TASKFENCE_REVIEWER_JWT`; the token is short-lived and must be
re-obtained when it expires. This is a deliberate stopgap: adding a browser
login flow could not be verified in this environment (there is no live tenant),
and shipping an unverified authentication flow would be worse than an explicit
manual step.

Consequently:

- `run_demo.sh` and the scenario runner exercise **local** mode only. Running
  JWT mode through the demo path is not supported yet.
- Because `TASKFENCE_REVIEWER_JWT` is read once when the dashboard process
  starts, **rotating an expired token requires restarting the dashboard**. An
  interactive demo cannot rely on that ritual.
- Do not describe JWT mode as demo-ready or as an end-user feature.

The next increment is a real OIDC authorization-code + PKCE login in the
dashboard (Streamlit's native OIDC or an equivalent library) that stores the
access token server-side per session, so the manual export disappears.

## Supported verification of the JWT dashboard flow

Because there is no interactive login, the supported evidence that the
reviewer workflow works is the automated end-to-end test:

```bash
.venv/bin/python -m pytest tests/test_dashboard_jwt_flow.py -q
```

It serves the real gateway with real uvicorn in JWT mode, drives the real
Streamlit app, and clicks the pending-approval **Deny** button. It asserts:

- a valid reviewer JWT plus a valid admin token resolves the approval and the
  UI reports it as done;
- a missing and an expired reviewer JWT each surface an explicit `401` message
  and leave the approval **pending** (no implied success);
- a missing admin token surfaces `403` while the JWT verifies, and likewise
  resolves nothing;
- the admin token, the reviewer JWT and the private key never appear in
  rendered content or in the logs.

Manual local walkthrough (the same two processes `run_demo.sh` starts):

```bash
.venv/bin/python -m uvicorn taskfence.gateway:app --port 8000   # local mode
TASKFENCE_ADMIN_TOKEN=devtoken .venv/bin/streamlit run dashboard/app.py
```

To exercise JWT mode by hand, export the configuration from
"Configuration" above **in both processes** and start the dashboard with a
reviewer token:

```bash
TASKFENCE_AUTH_MODE=jwt TASKFENCE_REVIEWER_JWT="$(cat reviewer-token)" \
  .venv/bin/python -m streamlit run dashboard/app.py
```

The dashboard then attaches that token server-side; every approval action
returns `401` once it expires, with a message telling the operator to obtain a
fresh one and restart the dashboard.

## Reviewer identity in audit responses

The validated `sub` is written to the dedicated `audit_events.subject` column —
never into `reasons`, which stays policy-derived. `GET /audit` withholds the
identity fields (`subject`, `approval_actor`) unless the caller passes the same
authentication the approval endpoints require. The agent-side runner reads the
audit trail without credentials and therefore never sees reviewer identity.
