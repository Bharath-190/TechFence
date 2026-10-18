"""Phase 3 (G-04): JWT identity validation tests.

Isolated and offline: every token is minted here with static test keys, so no
live Auth0 tenant is required. The suite covers

- legacy compatibility (JWT disabled keeps the X-Admin-Token behaviour),
- the 401/403 split (missing/invalid JWT is 401 even with a valid admin token;
  a valid JWT with a missing/invalid admin token is 403),
- signature, algorithm, issuer, audience, expiry, future-iat and claim-shape
  rejection, including the RS256 -> HS256 public-key-confusion case,
- reviewer identity: recorded for attribution, withheld from unauthenticated
  audit readers, and never mixed into policy `reasons`,
- startup validation of enabled-mode configuration and the absence of any
  runtime authentication downgrade.

AD-25 (rejected replays write no audit/decision row) is covered unchanged by
``tests/test_approval_drift.py``.
"""

import base64
import hashlib
import hmac
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from taskfence import registry
from taskfence.auth import JWTRejected, JWTVerifier
from taskfence.config import Config, ConfigError
from taskfence.gateway import app
from tests.jwt_keys import (
    OTHER_PRIVATE_KEY,
    TEST_PRIVATE_KEY,
    TEST_PUBLIC_KEY,
)

ADMIN = {"X-Admin-Token": "devtoken"}
ISSUER = "https://issuer.test/"
AUDIENCE = "taskfence-api"
SUBJECT = "auth0|reviewer-1"
TASK_TEXT = "Summarize Q3 sales and post it to #sales."
OUTBOUND_ARGS = {"channel": "#sales", "text": "regional conversion summary",
                 "source_assets": ["customer_db"]}

JWT_ENV = {
    "TASKFENCE_AUTH_MODE": "jwt",
    "TASKFENCE_JWT_PUBLIC_KEY": TEST_PUBLIC_KEY,
    "TASKFENCE_JWT_ALLOWED_ALGS": "RS256",
    "TASKFENCE_JWT_ISSUER": ISSUER,
    "TASKFENCE_JWT_AUDIENCE": AUDIENCE,
}


def _claims():
    now = int(time.time())
    return {"iss": ISSUER, "aud": AUDIENCE, "sub": SUBJECT,
            "exp": now + 300, "iat": now - 10, "jti": "jti-0001"}


def _token(private_key=TEST_PRIVATE_KEY, algorithm="RS256", drop=(),
           **overrides):
    claims = _claims()
    claims.update(overrides)
    for name in drop:
        claims.pop(name, None)
    if algorithm == "none":
        return pyjwt.encode(claims, None, algorithm="none")
    return pyjwt.encode(claims, private_key, algorithm=algorithm)


# A plain HMAC secret for the "algorithm not in the allowlist" case; it is
# never a real credential and exists only to mint a non-RS256 token.
HMAC_SECRET = "hmac-shared-secret-0123456789abcdef"


def _raw_token(payload, private_key=TEST_PRIVATE_KEY, algorithm="RS256"):
    """Mint a JWT with an arbitrary JSON payload.

    ``pyjwt.encode`` refuses to emit several malformed claim shapes (numeric
    ``iss``, null ``sub``, ...), so the malformed-value cases are signed here
    directly. This still produces a genuine RS256 signature over the exact
    bytes our verifier will inspect.
    """
    def segment(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = (segment({"alg": algorithm, "typ": "JWT"}) + b"."
                     + segment(payload))
    key = serialization.load_pem_private_key(private_key.encode(),
                                             password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"."
            + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _hmac_forged_token():
    """A genuine HS256 token whose MAC key is the PUBLIC key text.

    This is the classic RS256 -> HS256 confusion forgery. PyJWT refuses to
    *create* it, so the signature is computed here directly; it must also be
    refused on verification even if an operator widens the algorithm
    allowlist (Phase 3 hardening pin).
    """
    def segment(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = (segment({"alg": "HS256", "typ": "JWT"}) + b"."
                     + segment(_claims()))
    signature = hmac.new(TEST_PUBLIC_KEY.encode(), signing_input,
                         hashlib.sha256).digest()
    return (signing_input + b"."
            + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _audit_events(client, task_id, headers=None):
    """Raw audit trail as the given caller receives it (no headers = the
    agent-side/runner view)."""
    response = client.get("/audit", params={"task_id": task_id},
                          headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["events"]


def _with(headers=None):
    merged = dict(ADMIN)
    merged.update(headers or {})
    return merged


def _pending_approval(client):
    response = client.post("/tasks", json={"task_text": TASK_TEXT})
    assert response.status_code == 200, response.text
    task_id = response.json()["task_id"]
    response = client.post(f"/tasks/{task_id}/tool-call",
                           json={"tool": "send_slack", "args": OUTBOUND_ARGS})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"]["outcome"] == "APPROVE", body
    return task_id, body["approval_id"]


def _resolve(client, approval_id, headers):
    return client.post(f"/approvals/{approval_id}/resolve",
                       json={"choice": "deny"}, headers=headers)


@pytest.fixture()
def legacy_client(isolate_gateway, monkeypatch):
    """JWT disabled: the pre-Phase-3 configuration."""
    import taskfence.gateway as gw

    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    config = Config.from_env({})
    monkeypatch.setattr(gw, "CONFIG", config)
    monkeypatch.setattr(gw, "JWT", JWTVerifier.from_config(config))
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client
    registry.reset_outbox()


@pytest.fixture()
def jwt_client(isolate_gateway, monkeypatch):
    """JWT enabled: the verifier is fixed for the process lifetime."""
    import taskfence.gateway as gw

    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    config = Config.from_env(dict(JWT_ENV))
    monkeypatch.setattr(gw, "CONFIG", config)
    monkeypatch.setattr(gw, "JWT", JWTVerifier.from_config(config))
    registry.reset_outbox()
    with TestClient(app) as test_client:
        yield test_client
    registry.reset_outbox()


# --------------------------------------------------------------------------
# Legacy compatibility: JWT disabled preserves X-Admin-Token exactly.
# --------------------------------------------------------------------------

def test_jwt_disabled_valid_admin_token_resolves(legacy_client):
    _, approval_id = _pending_approval(legacy_client)
    response = _resolve(legacy_client, approval_id, ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "denied"


def test_jwt_disabled_missing_admin_token_is_403(legacy_client):
    _, approval_id = _pending_approval(legacy_client)
    response = _resolve(legacy_client, approval_id, {})
    assert response.status_code == 403, response.text


def test_jwt_disabled_wrong_admin_token_is_403(legacy_client):
    _, approval_id = _pending_approval(legacy_client)
    response = _resolve(legacy_client, approval_id,
                        {"X-Admin-Token": "wrong"})
    assert response.status_code == 403, response.text


def test_jwt_disabled_ignores_authorization_header(legacy_client):
    """No JWT behaviour leaks in when the mode is off."""
    _, approval_id = _pending_approval(legacy_client)
    response = _resolve(legacy_client, approval_id,
                        _with(_bearer("not-a-jwt")))
    assert response.status_code == 200, response.text


def test_jwt_disabled_detail_endpoint_keeps_admin_gate(legacy_client):
    _, approval_id = _pending_approval(legacy_client)
    assert legacy_client.get(f"/approvals/{approval_id}").status_code == 403
    assert legacy_client.get(f"/approvals/{approval_id}",
                             headers=ADMIN).status_code == 200


# --------------------------------------------------------------------------
# JWT enabled: 401 for identity, 403 for authorization.
# --------------------------------------------------------------------------

def test_valid_jwt_and_valid_admin_token_resolves(jwt_client):
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id, _with(_bearer(_token())))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "denied"


def test_public_key_is_not_usable_as_an_hmac_secret():
    """A hand-built allowlist that includes HS256 must still refuse a token
    HMAC'd with the configured public key (no algorithm confusion)."""
    verifier = JWTVerifier(public_key=TEST_PUBLIC_KEY,
                           algorithms=("RS256", "HS256"),
                           issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(JWTRejected):
        verifier.verify(_hmac_forged_token())


# --------------------------------------------------------------------------
# Reviewer identity: attribution only, never authorization, never public.
# --------------------------------------------------------------------------

def test_reviewer_identity_requires_authentication_to_read(jwt_client):
    """An unauthenticated reader — including the agent-side scenario runner —
    gets the audit trail but never the reviewer identity."""
    task_id, approval_id = _pending_approval(jwt_client)
    assert _resolve(jwt_client, approval_id,
                    _with(_bearer(_token()))).status_code == 200

    public = _audit_events(jwt_client, task_id)
    assert any(e["action"] == "approval_resolve" for e in public), public
    assert all("subject" not in e for e in public)
    assert SUBJECT not in json.dumps(public)


def test_authenticated_reviewer_identity_is_recorded(jwt_client):
    task_id, approval_id = _pending_approval(jwt_client)
    assert _resolve(jwt_client, approval_id,
                    _with(_bearer(_token()))).status_code == 200

    events = _audit_events(jwt_client, task_id,
                           _with(_bearer(_token())))
    resolved = [e for e in events if e["action"] == "approval_resolve"]
    assert resolved and resolved[-1]["subject"] == SUBJECT
    # Attribution stays out of the policy-reason channel.
    assert all(SUBJECT not in str(e.get("reasons", "")) for e in events)


def test_reviewer_identity_requires_the_admin_token_too(jwt_client):
    """A valid JWT alone does not unlock identity: authorization is separate
    from identity (AD-06)."""
    task_id, approval_id = _pending_approval(jwt_client)
    assert _resolve(jwt_client, approval_id,
                    _with(_bearer(_token()))).status_code == 200

    events = _audit_events(jwt_client, task_id, _bearer(_token()))
    assert all("subject" not in e for e in events)


def test_audit_trail_stays_readable_without_jwt(legacy_client):
    """JWT disabled: the demo readiness probe and the runner keep working,
    and no identity exists to expose."""
    task_id, approval_id = _pending_approval(legacy_client)
    assert _resolve(legacy_client, approval_id, ADMIN).status_code == 200
    events = _audit_events(legacy_client, task_id)
    assert events
    assert all(not e.get("subject") for e in events)


def test_valid_jwt_with_missing_admin_token_is_403(jwt_client):
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id, _bearer(_token()))
    assert response.status_code == 403, response.text


def test_valid_jwt_with_invalid_admin_token_is_403(jwt_client):
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id,
                        _with({**_bearer(_token()),
                               "X-Admin-Token": "wrong"}))
    assert response.status_code == 403, response.text


def test_missing_jwt_with_valid_admin_token_is_401(jwt_client):
    """A correct admin token is NOT a substitute for identity."""
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id, ADMIN)
    assert response.status_code == 401, response.text


def test_missing_jwt_detail_endpoint_is_401(jwt_client):
    _, approval_id = _pending_approval(jwt_client)
    assert jwt_client.get(f"/approvals/{approval_id}",
                          headers=ADMIN).status_code == 401


def test_non_bearer_scheme_is_401(jwt_client):
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id,
                        _with({"Authorization": _token()}))
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("token_maker,label", [
    (lambda: _token(private_key=OTHER_PRIVATE_KEY), "wrong-key"),
    (lambda: _token(exp=int(time.time()) - 1), "expired"),
    (lambda: _token(exp=int(time.time())), "exp-not-strictly-future"),
    (lambda: _token(iat=int(time.time()) + 120), "future-iat"),
    (lambda: _token(iss="https://other.example/"), "wrong-issuer"),
    (lambda: _token(aud="other-audience"), "wrong-audience"),
    (lambda: _token(algorithm="none"), "alg-none"),
    (lambda: pyjwt.encode(_claims(), HMAC_SECRET, algorithm="HS256"),
     "hs256-not-allowed"),
    (lambda: "garbage", "malformed"),
])
def test_invalid_token_is_401_even_with_valid_admin(jwt_client, token_maker,
                                                    label):
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id,
                        _with(_bearer(token_maker())))
    assert response.status_code == 401, (label, response.text)


@pytest.mark.parametrize("missing", ["iss", "aud", "sub", "exp", "iat", "jti"])
def test_missing_required_claim_is_401(jwt_client, missing):
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id,
                        _with(_bearer(_token(drop=(missing,)))))
    assert response.status_code == 401, (missing, response.text)


@pytest.mark.parametrize("claim,value,label", [
    ("iss", "", "empty-iss"),
    ("iss", 123, "numeric-iss"),
    ("sub", "", "empty-sub"),
    ("sub", None, "null-sub"),
    ("jti", "", "empty-jti"),
    ("jti", 42, "numeric-jti"),
    ("exp", True, "boolean-exp"),
    ("exp", "9999999999", "string-exp"),
    ("iat", True, "boolean-iat"),
    ("iat", "1", "string-iat"),
    ("aud", "", "empty-aud"),
])
def test_malformed_required_claim_is_401(jwt_client, claim, value, label):
    """Malformed values are rejected outright rather than coerced."""
    claims = _claims()
    claims[claim] = value
    _, approval_id = _pending_approval(jwt_client)
    response = _resolve(jwt_client, approval_id,
                        _with(_bearer(_raw_token(claims))))
    assert response.status_code == 401, (label, response.text)


def test_rejected_token_does_not_disable_jwt_for_later_requests(jwt_client):
    """A 401 is per-request; the next valid request still succeeds."""
    _, approval_id = _pending_approval(jwt_client)
    assert _resolve(jwt_client, approval_id, ADMIN).status_code == 401
    assert _resolve(jwt_client, approval_id,
                    _with(_bearer(_token()))).status_code == 200


def test_jwt_mode_is_fixed_and_never_downgrades_at_runtime(
        jwt_client, monkeypatch):
    """Changing the environment after startup cannot switch the mode off."""
    _, approval_id = _pending_approval(jwt_client)
    monkeypatch.setenv("TASKFENCE_AUTH_MODE", "local")
    monkeypatch.delenv("TASKFENCE_JWT_PUBLIC_KEY", raising=False)
    assert _resolve(jwt_client, approval_id, ADMIN).status_code == 401
    assert _resolve(jwt_client, approval_id,
                    _with(_bearer(_token()))).status_code == 200


def test_missing_verifier_in_enabled_mode_fails_closed(
        isolate_gateway, monkeypatch):
    """Infrastructure loss is a 503, never a legacy fallback."""
    import taskfence.gateway as gw

    monkeypatch.setenv("TASKFENCE_ADMIN_TOKEN", "devtoken")
    monkeypatch.setattr(gw, "CONFIG", Config.from_env(dict(JWT_ENV)))
    monkeypatch.setattr(gw, "JWT", None)
    registry.reset_outbox()
    with TestClient(app) as test_client:
        _, approval_id = _pending_approval(test_client)
        response = _resolve(test_client, approval_id, ADMIN)
        assert response.status_code == 503, response.text
    registry.reset_outbox()


# --------------------------------------------------------------------------
# Startup validation of enabled-mode configuration.
# --------------------------------------------------------------------------

def test_verifier_is_none_when_jwt_is_disabled():
    assert JWTVerifier.from_config(Config.from_env({})) is None


def test_startup_rejects_an_unusable_pem_key():
    config = Config.from_env({**JWT_ENV,
                              "TASKFENCE_JWT_PUBLIC_KEY": "not-a-pem-key"})
    with pytest.raises(ConfigError, match="TASKFENCE_JWT_PUBLIC_KEY"):
        JWTVerifier.from_config(config)


def test_a_none_algorithm_allowlist_is_rejected_at_startup():
    """Unsigned tokens are never acceptable, at either layer."""
    with pytest.raises(ConfigError, match="none"):
        Config.from_env({**JWT_ENV,
                         "TASKFENCE_JWT_ALLOWED_ALGS": "RS256,none"})


def test_verifier_refuses_alg_none_defence_in_depth():
    """Even a hand-built verifier refuses an alg:none token."""
    verifier = JWTVerifier(public_key=TEST_PUBLIC_KEY,
                           algorithms=("RS256", "none"),
                           issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(JWTRejected):
        verifier.verify(_token(algorithm="none"))
