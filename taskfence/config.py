"""Centralized configuration (Phase 1; AD-21, G-22).

One place to read environment variables, validate them early, and keep
secrets out of logs and reprs.

Scope for this phase: the module and its own tests. Existing modules still
read the environment directly; adopting this module is deliberately deferred
to the phase that owns each concern (Phase 2 persistence, Phase 3 identity,
Phase 6 credentials, Phase 8 providers, Phase 9 telemetry). Nothing here
changes policy semantics or enables any new behaviour, and no budget value
is enforced yet — Phase 7 owns enforcement.

Fail-closed rules implemented here:

- JWT mode requires an issuer and an audience; there is no anonymous fallback.
- Auth0 settings present while the mode is ``local`` are rejected rather than
  silently downgraded to anonymous access.
- A database URL must be a PostgreSQL URL; anything else fails at startup.
- Timeouts and budget defaults must be valid numbers.

Secret values are wrapped in :class:`Secret`, whose ``repr``/``str``/``format``
render ``***``. Use ``secret.reveal()`` at the single point of use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_GATEWAY_URL = "http://localhost:8000"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3"
DEFAULT_SQLITE_PATH = "taskfence.sqlite3"
# Directory holding this package: the anchor for relative SQLite paths
# so a gateway and a dashboard started from different working
# directories address the same file (G-16, AD-21).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHAT_TIMEOUT = 60.0
DEFAULT_MCP_TIMEOUT = 30.0

AUTH_MODES = ("local", "jwt")
_POSTGRES_SCHEMES = ("postgresql://", "postgres://")

# Phase 3 (G-04): JWT verification uses exactly one configured PEM public key
# and a fixed algorithm allowlist. No JWKS, no key rotation in this phase.
DEFAULT_JWT_ALGORITHMS = "RS256"


class ConfigError(ValueError):
    """Raised when configuration is missing, contradictory, or malformed."""


def normalized_path(value: str) -> str:
    """Lexical path identity used for the PostgreSQL/SQLite conflict rule.

    ``expanduser`` then ``normpath``: ``./taskfence.sqlite3`` and
    ``sub/../taskfence.sqlite3`` compare equal to the default, while absolute,
    ``~``-expanded, or symlinked paths keep a distinct identity. Symlinks are
    deliberately not resolved, so two paths to the same file stay a conflict.
    """
    return os.path.normpath(os.path.expanduser(value))


class Secret:
    """A string that refuses to show itself in logs, reprs, or f-strings.

    ``reveal()`` is the only way to read the value, which keeps the call sites
    where a secret becomes plaintext discoverable by inspection.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | None = None) -> None:
        self._value = value or ""

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __format__(self, format_spec: str) -> str:
        return "***"


@dataclass(frozen=True)
class BudgetDefaults:
    """Configured default limits (Phase 7 enforces them; nothing here does)."""

    max_tool_calls: int = 50
    max_outbound_calls: int = 20
    max_records_read: int = 1000
    max_outbound_messages: int = 20
    max_model_turns: int = 8
    max_execution_seconds: float = 300.0
    max_data_bytes: int = 0  # 0 == not configured; Phase 7 must not claim it enforced


@dataclass(frozen=True)
class Config:
    """Immutable, validated configuration snapshot."""

    gateway_url: str = DEFAULT_GATEWAY_URL
    database_url: Secret = field(default_factory=Secret)
    sqlite_path: Path = Path(DEFAULT_SQLITE_PATH)
    auth_mode: str = "local"
    auth0_issuer: str = ""
    auth0_audience: str = ""
    # Phase 3 (G-04) identity configuration. The PEM public key is public
    # material (never a secret), and the allowlist is a plain string.
    jwt_public_key: str = ""
    jwt_allowed_algs: str = DEFAULT_JWT_ALGORITHMS
    jwt_issuer: str = ""
    jwt_audience: str = ""
    gemini_api_key: Secret = field(default_factory=Secret)
    gemini_model: str = ""
    ollama_url: str = DEFAULT_OLLAMA_URL
    ollama_model: str = DEFAULT_OLLAMA_MODEL
    chat_timeout: float = DEFAULT_CHAT_TIMEOUT
    mcp_timeout: float = DEFAULT_MCP_TIMEOUT
    upstash_url: str = ""
    upstash_token: Secret = field(default_factory=Secret)
    real_slack_webhook: Secret = field(default_factory=Secret)
    admin_token: Secret = field(default_factory=Secret)
    budgets: BudgetDefaults = field(default_factory=BudgetDefaults)

    def __post_init__(self) -> None:
        if not self.gateway_url.startswith(("http://", "https://")):
            raise ConfigError("TASKFENCE_URL must start with http:// or https://")
        if not self.ollama_url.startswith(("http://", "https://")):
            raise ConfigError("TASKFENCE_OLLAMA_URL must start with http:// or https://")

        mode = self.auth_mode.lower()
        if mode not in AUTH_MODES:
            raise ConfigError(
                f"TASKFENCE_AUTH_MODE must be one of {AUTH_MODES}, got {self.auth_mode!r}")
        object.__setattr__(self, "auth_mode", mode)

        # Phase 3 (G-04): validate the fixed identity configuration once, at
        # startup. A missing/contradictory JWT setting fails here, before the
        # process serves a single request; it never degrades to legacy auth.
        algs = tuple(a.strip() for a in self.jwt_allowed_algs.split(",")
                     if a.strip())
        object.__setattr__(self, "jwt_allowed_algs", ",".join(algs))
        if "none" in {a.lower() for a in algs}:
            raise ConfigError(
                "TASKFENCE_JWT_ALLOWED_ALGS must not include 'none' (unsigned "
                "tokens are never accepted)")

        # Phase 3 (G-04): security settings that are present but cannot take
        # effect must never be ignored silently. Captured from the RAW
        # configured values, before the issuer/audience fallback below, so a
        # populated Auth0 variable is not mistaken for a JWT one.
        jwt_material = [name for name, value in (
            ("TASKFENCE_JWT_PUBLIC_KEY", self.jwt_public_key),
            ("TASKFENCE_JWT_ISSUER", self.jwt_issuer),
            ("TASKFENCE_JWT_AUDIENCE", self.jwt_audience),
        ) if value]

        # One configured issuer/audience pair. The dedicated JWT variables
        # take precedence; the Auth0 variables remain the single identity
        # configuration for existing deployments.
        if not self.jwt_issuer:
            object.__setattr__(self, "jwt_issuer", self.auth0_issuer)
        if not self.jwt_audience:
            object.__setattr__(self, "jwt_audience", self.auth0_audience)

        if mode == "jwt":
            if not self.jwt_public_key:
                raise ConfigError(
                    "jwt authentication mode requires TASKFENCE_JWT_PUBLIC_KEY")
            if not algs:
                raise ConfigError(
                    "jwt authentication mode requires TASKFENCE_JWT_ALLOWED_ALGS")
            missing = [name for name, value in (
                ("an issuer (TASKFENCE_JWT_ISSUER or TASKFENCE_AUTH0_ISSUER)",
                 self.jwt_issuer),
                ("an audience (TASKFENCE_JWT_AUDIENCE or TASKFENCE_AUTH0_AUDIENCE)",
                 self.jwt_audience),
            ) if not value]
            if missing:
                raise ConfigError(
                    "jwt authentication mode requires " + " and ".join(missing))
        elif self.auth0_issuer or self.auth0_audience:
            # Refuse to silently downgrade a JWT-intended setup to anonymous.
            raise ConfigError(
                "Auth0 issuer/audience are set but TASKFENCE_AUTH_MODE is 'local'; "
                "refusing to downgrade to anonymous access")
        elif jwt_material:
            # Phase 3 (G-04): JWT verification settings configured without
            # enabling JWT mode would leave the process on legacy
            # authentication while the operator believes identity is
            # enforced. Fail at startup instead (`TASKFENCE_JWT_ALLOWED_ALGS`
            # is deliberately not part of this list: it has a secure default
            # and is validated for every mode).
            raise ConfigError(
                "JWT verification settings are configured ("
                + ", ".join(jwt_material)
                + ") but TASKFENCE_AUTH_MODE is 'local'; refusing to silently "
                "ignore identity configuration — set TASKFENCE_AUTH_MODE=jwt "
                "or remove the JWT settings")

        if self.database_url:
            url = self.database_url.reveal()
            if not url.startswith(_POSTGRES_SCHEMES):
                raise ConfigError(
                    "TASKFENCE_DATABASE_URL must be a PostgreSQL URL "
                    "(postgresql:// or postgres://); use TASKFENCE_DB for SQLite")
            if (normalized_path(str(self.sqlite_path))
                    != normalized_path(DEFAULT_SQLITE_PATH)):
                raise ConfigError(
                    "TASKFENCE_DATABASE_URL is set and TASKFENCE_DB names a "
                    f"different SQLite path ({self.sqlite_path}); configure "
                    "one persistence backend (unset TASKFENCE_DB to use "
                    "PostgreSQL)")

        # G-16 / AD-21: a relative SQLite path must not depend on the
        # working directory. The conflict check above compares the RAW
        # configured value (documented lexical rule); the value stored here
        # is anchored once, so every process resolves the same file.
        if not self.sqlite_path.is_absolute():
            object.__setattr__(
                self, "sqlite_path",
                Path(os.path.normpath(PROJECT_ROOT / str(self.sqlite_path))))

        if self.chat_timeout <= 0:
            raise ConfigError("TASKFENCE_CHAT_TIMEOUT must be greater than zero")
        if self.mcp_timeout <= 0:
            raise ConfigError("TASKFENCE_MCP_TIMEOUT must be greater than zero")
        if self.budgets.max_execution_seconds <= 0:
            raise ConfigError("TASKFENCE_BUDGET_MAX_EXECUTION_SECONDS must be greater than zero")
        for name in ("max_tool_calls", "max_outbound_calls", "max_records_read",
                     "max_outbound_messages", "max_model_turns", "max_data_bytes"):
            if getattr(self.budgets, name) < 0:
                raise ConfigError(f"budget {name} must not be negative")

    @property
    def use_postgres(self) -> bool:
        return bool(self.database_url)

    @property
    def jwt_enabled(self) -> bool:
        return self.auth_mode == "jwt"

    @property
    def jwt_algorithms(self) -> tuple[str, ...]:
        """Fixed algorithm allowlist, validated once at startup."""
        return tuple(a for a in self.jwt_allowed_algs.split(",") if a)

    @property
    def real_slack_enabled(self) -> bool:
        return bool(self.real_slack_webhook)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        source = os.environ if env is None else env

        def text(key: str, default: str = "") -> str:
            raw = source.get(key)
            if raw is None:
                return default
            value = raw.strip()
            return value if value else default

        def secret(key: str) -> Secret:
            return Secret(text(key))

        def number(key: str, default: float, cast):
            raw = text(key)
            if raw == "":
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                raise ConfigError(f"{key} must be a number, got {raw!r}") from None

        budgets = BudgetDefaults(
            max_tool_calls=number("TASKFENCE_BUDGET_MAX_TOOL_CALLS",
                                  BudgetDefaults.max_tool_calls, int),
            max_outbound_calls=number("TASKFENCE_BUDGET_MAX_OUTBOUND_CALLS",
                                      BudgetDefaults.max_outbound_calls, int),
            max_records_read=number("TASKFENCE_BUDGET_MAX_RECORDS_READ",
                                    BudgetDefaults.max_records_read, int),
            max_outbound_messages=number("TASKFENCE_BUDGET_MAX_OUTBOUND_MESSAGES",
                                         BudgetDefaults.max_outbound_messages, int),
            max_model_turns=number("TASKFENCE_BUDGET_MAX_MODEL_TURNS",
                                   BudgetDefaults.max_model_turns, int),
            max_execution_seconds=number("TASKFENCE_BUDGET_MAX_EXECUTION_SECONDS",
                                         BudgetDefaults.max_execution_seconds, float),
            max_data_bytes=number("TASKFENCE_BUDGET_MAX_DATA_BYTES",
                                  BudgetDefaults.max_data_bytes, int),
        )

        return cls(
            gateway_url=text("TASKFENCE_URL", DEFAULT_GATEWAY_URL),
            database_url=secret("TASKFENCE_DATABASE_URL"),
            sqlite_path=Path(os.path.expanduser(
                text("TASKFENCE_DB", DEFAULT_SQLITE_PATH))),
            auth_mode=text("TASKFENCE_AUTH_MODE", "local"),
            auth0_issuer=text("TASKFENCE_AUTH0_ISSUER"),
            auth0_audience=text("TASKFENCE_AUTH0_AUDIENCE"),
            jwt_public_key=text("TASKFENCE_JWT_PUBLIC_KEY"),
            jwt_allowed_algs=text("TASKFENCE_JWT_ALLOWED_ALGS",
                                  DEFAULT_JWT_ALGORITHMS),
            jwt_issuer=text("TASKFENCE_JWT_ISSUER"),
            jwt_audience=text("TASKFENCE_JWT_AUDIENCE"),
            gemini_api_key=secret("GEMINI_API_KEY"),
            gemini_model=text("GEMINI_MODEL"),
            ollama_url=text("TASKFENCE_OLLAMA_URL", DEFAULT_OLLAMA_URL),
            ollama_model=text("TASKFENCE_MODEL", DEFAULT_OLLAMA_MODEL),
            chat_timeout=number("TASKFENCE_CHAT_TIMEOUT", DEFAULT_CHAT_TIMEOUT, float),
            mcp_timeout=number("TASKFENCE_MCP_TIMEOUT", DEFAULT_MCP_TIMEOUT, float),
            upstash_url=text("UPSTASH_REDIS_URL"),
            upstash_token=secret("UPSTASH_REDIS_TOKEN"),
            real_slack_webhook=secret("TASKFENCE_REAL_SLACK_WEBHOOK"),
            admin_token=secret("TASKFENCE_ADMIN_TOKEN"),
            budgets=budgets,
        )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read and validate configuration. Raises ConfigError before startup."""
    return Config.from_env(env)
