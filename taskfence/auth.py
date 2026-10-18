"""Phase 3 (G-04): JWT identity verification.

Design constraints fixed by the approved Phase 3 design:

- Exactly ONE configured PEM public key. No JWKS, no key rotation.
- A FIXED algorithm allowlist. ``alg:none`` (and any algorithm outside the
  allowlist) is rejected; unsigned tokens are never accepted.
- Required claims: ``iss``, ``aud``, ``sub``, ``exp``, ``iat`` and ``jti``.
  ``iss``/``sub``/``jti`` must be non-empty strings; ``aud`` is matched
  against the configured audience; ``exp``/``iat`` must be genuine
  NumericDate numbers (booleans and strings are rejected, not coerced).
- Zero leeway: ``exp`` must be strictly in the future and ``iat`` must not
  be in the future.
- ``sub`` is FORENSIC attribution only. It never grants authorization and is
  never read from a request body or an untrusted header.

The verifier is built once from the validated startup configuration and is
immutable for the process lifetime (no runtime mode switching). Any
configuration/verification-infrastructure problem fails closed: callers
receive a rejection, never legacy authentication.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import jwt as pyjwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from taskfence.config import Config, ConfigError

# Diagnostics: only the failure CLASS is ever logged (e.g. ExpiredSignatureError,
# InvalidIssuerError). The token, its signature, and every claim value —
# including the forensic `sub` — are never written to a log.
logger = logging.getLogger("taskfence.auth")

REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp", "iat", "jti")


class JWTRejected(Exception):
    """A token was presented but is not acceptable. Never a downgrade."""


class JWTUnavailable(Exception):
    """Verification infrastructure is unavailable. Fail closed, never fall
    back to legacy authentication."""


@dataclass(frozen=True)
class JWTVerifier:
    """Immutable verifier bound to the validated startup configuration."""

    public_key: str
    algorithms: tuple[str, ...]
    issuer: str
    audience: str

    @classmethod
    def from_config(cls, config: Config) -> "JWTVerifier | None":
        """Build the verifier for a validated configuration.

        Returns ``None`` when JWT mode is disabled (legacy behaviour is then
        preserved byte-for-byte). Raises :class:`ConfigError` at startup when
        JWT mode is enabled but the key material is unusable — the process
        must not start and must never silently downgrade.
        """
        if not config.jwt_enabled:
            return None
        verifier = cls(
            public_key=config.jwt_public_key,
            algorithms=tuple(config.jwt_algorithms),
            issuer=config.jwt_issuer,
            audience=config.jwt_audience,
        )
        verifier._assert_usable_key()
        return verifier

    def _assert_usable_key(self) -> None:
        """Parse the single configured PEM key once, at startup."""
        if not self.algorithms:
            raise ConfigError(
                "jwt authentication mode requires a non-empty algorithm allowlist")
        lowered = {algorithm.lower() for algorithm in self.algorithms}
        if "none" in lowered:
            raise ConfigError(
                "TASKFENCE_JWT_ALLOWED_ALGS must not include 'none'; "
                "unsigned tokens are never accepted")
        if not self.issuer or not self.audience:
            raise ConfigError(
                "jwt authentication mode requires a configured issuer and "
                "audience")
        try:
            load_pem_public_key(self.public_key.encode("utf-8"))
        except Exception as error:  # noqa: BLE001 - startup fail-closed
            raise ConfigError(
                "TASKFENCE_JWT_PUBLIC_KEY is not a usable PEM public key: "
                f"{type(error).__name__}") from None

    def verify(self, token: str) -> dict:
        """Verify signature, algorithm, issuer, audience, lifetime and the
        required claim shapes. Returns the validated payload (including the
        forensic ``sub``) or raises :class:`JWTRejected`.

        Never raises anything a caller could mistake for success: every
        failure path is a rejection (or :class:`JWTUnavailable` when the
        verifier itself cannot run).
        """
        if not isinstance(token, str) or not token.strip():
            raise JWTRejected("missing token")
        if not self.public_key or not self.algorithms:
            raise JWTUnavailable("jwt verification is not configured")
        # Explicit unsigned-token rejection, independent of the allowlist:
        # "alg" is read from the protected header and refused before any
        # verification, so alg:none can never be accepted even if a caller
        # builds a verifier with a hand-edited allowlist.
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.PyJWTError:
            raise JWTRejected("malformed token") from None
        if str(header.get("alg", "")).lower() == "none":
            raise JWTRejected("unsigned tokens are not accepted")
        try:
            payload = pyjwt.decode(
                token,
                self.public_key,
                algorithms=list(self.algorithms),
                audience=self.audience,
                issuer=self.issuer,
                leeway=0,
                options={
                    "require": list(REQUIRED_CLAIMS),
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except pyjwt.PyJWTError as error:
            logger.info("jwt rejected: %s", type(error).__name__)
            raise JWTRejected("token rejected") from None
        except Exception as error:  # noqa: BLE001 - fail closed on any fault
            logger.warning("jwt verification fault: %s", type(error).__name__)
            raise JWTRejected("token rejected") from None

        # Strict claim shapes. PyJWT tolerates permissive coercions (e.g.
        # int(True) == 1 or int("123") == 123); the design forbids relying
        # on that, so re-check the raw values here.
        for claim in ("iss", "sub", "jti"):
            value = payload.get(claim)
            if not isinstance(value, str) or not value:
                raise JWTRejected(f"claim {claim} must be a non-empty string")
        if not isinstance(payload.get("aud"), (str, list)) or not payload["aud"]:
            raise JWTRejected("claim aud must be non-empty")

        now = int(time.time())
        for claim in ("exp", "iat"):
            value = payload.get(claim)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise JWTRejected(
                    f"claim {claim} must be a NumericDate number")
        if payload["exp"] <= now:
            raise JWTRejected("token is expired")
        if payload["iat"] > now:
            raise JWTRejected("token was issued in the future")
        return payload
