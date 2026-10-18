"""Configuration tests (Phase 1; AD-21, G-22).

Every test builds configuration from an explicit mapping, so the process
environment never has to be mutated and the tests stay deterministic.
"""

import logging

import pytest

from taskfence.config import (
    DEFAULT_GATEWAY_URL,
    DEFAULT_MCP_TIMEOUT,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_SQLITE_PATH,
    BudgetDefaults,
    Config,
    ConfigError,
    Secret,
    load_config,
)


def test_defaults_preserve_the_local_demo():
    config = Config.from_env({})

    assert config.gateway_url == DEFAULT_GATEWAY_URL
    assert config.ollama_url == "http://localhost:11434"
    assert config.ollama_model == DEFAULT_OLLAMA_MODEL
    # A relative default is anchored to the project root so the
    # working directory cannot change which file is used (G-16).
    from taskfence.config import PROJECT_ROOT
    assert config.sqlite_path == PROJECT_ROOT / DEFAULT_SQLITE_PATH
    assert config.auth_mode == "local"
    assert config.mcp_timeout == DEFAULT_MCP_TIMEOUT
    # Local demo: no cloud path, no credentials, nothing to require.
    assert config.use_postgres is False
    assert config.jwt_enabled is False
    assert config.real_slack_enabled is False
    assert config.gemini_api_key.reveal() == ""
    assert config.budgets == BudgetDefaults()


def test_relative_sqlite_paths_are_independent_of_the_working_directory(
        tmp_path, monkeypatch):
    """G-16: two processes started from different directories resolve the
    same SQLite file from the same configuration snapshot."""
    monkeypatch.chdir(tmp_path)
    from taskfence.config import PROJECT_ROOT
    assert Config.from_env({}).sqlite_path == PROJECT_ROOT / DEFAULT_SQLITE_PATH
    assert (Config.from_env({"TASKFENCE_DB": "./taskfence.sqlite3"}).sqlite_path
            == PROJECT_ROOT / "taskfence.sqlite3")
    assert (Config.from_env(
        {"TASKFENCE_DB": "sub/../taskfence.sqlite3"}).sqlite_path
        == PROJECT_ROOT / "taskfence.sqlite3")


def test_environment_overrides_are_applied():
    config = Config.from_env({
        "TASKFENCE_URL": "http://gateway.internal:9000",
        "TASKFENCE_OLLAMA_URL": "http://ollama.internal:11434",
        "TASKFENCE_MODEL": "qwen3:8b",
        "TASKFENCE_DB": "/tmp/taskfence-test.sqlite3",
        "TASKFENCE_CHAT_TIMEOUT": "12.5",
        "TASKFENCE_MCP_TIMEOUT": "7",
        "TASKFENCE_BUDGET_MAX_TOOL_CALLS": "3",
        "TASKFENCE_BUDGET_MAX_MODEL_TURNS": "2",
    })

    assert config.gateway_url == "http://gateway.internal:9000"
    assert config.ollama_url == "http://ollama.internal:11434"
    assert config.ollama_model == "qwen3:8b"
    assert config.sqlite_path.as_posix() == "/tmp/taskfence-test.sqlite3"
    assert config.chat_timeout == 12.5
    assert config.mcp_timeout == 7.0
    assert config.budgets.max_tool_calls == 3
    assert config.budgets.max_model_turns == 2


def test_postgresql_url_selects_the_postgres_path():
    config = Config.from_env({
        "TASKFENCE_DATABASE_URL": "postgresql://user:pw@db.example:5432/taskfence",
    })
    assert config.use_postgres is True


def test_non_postgres_database_url_fails_early():
    with pytest.raises(ConfigError, match="PostgreSQL URL"):
        Config.from_env({"TASKFENCE_DATABASE_URL": "mysql://user@host/db"})


from tests.jwt_keys import TEST_PUBLIC_KEY

JWT_BASE_ENV = {
    "TASKFENCE_AUTH_MODE": "jwt",
    "TASKFENCE_AUTH0_ISSUER": "https://tenant.example/",
    "TASKFENCE_AUTH0_AUDIENCE": "taskfence-api",
    "TASKFENCE_JWT_PUBLIC_KEY": TEST_PUBLIC_KEY,
    "TASKFENCE_JWT_ALLOWED_ALGS": "RS256",
}


@pytest.mark.parametrize("missing", [
    "TASKFENCE_AUTH0_ISSUER",
    "TASKFENCE_AUTH0_AUDIENCE",
])
def test_jwt_mode_requires_identity_configuration(missing):
    env = dict(JWT_BASE_ENV)
    env.pop(missing)

    with pytest.raises(ConfigError, match=missing):
        Config.from_env(env)


def test_jwt_mode_accepts_complete_identity_configuration():
    config = Config.from_env({**JWT_BASE_ENV, "TASKFENCE_AUTH_MODE": "JWT"})
    assert config.auth_mode == "jwt"
    assert config.jwt_enabled is True
    assert config.jwt_algorithms == ("RS256",)


def test_jwt_mode_requires_a_public_key():
    """Phase 3 (G-04): enabled JWT mode without key material fails at
    startup rather than degrading to legacy authentication."""
    env = dict(JWT_BASE_ENV)
    env.pop("TASKFENCE_JWT_PUBLIC_KEY")

    with pytest.raises(ConfigError, match="TASKFENCE_JWT_PUBLIC_KEY"):
        Config.from_env(env)


def test_jwt_mode_defaults_to_a_fixed_rs256_allowlist():
    """A fixed secure default: the allowlist is never empty or open."""
    env = dict(JWT_BASE_ENV)
    env.pop("TASKFENCE_JWT_ALLOWED_ALGS")
    config = Config.from_env(env)
    assert config.jwt_algorithms == ("RS256",)


def test_jwt_algorithm_allowlist_rejects_none():
    """Unsigned tokens must never be acceptable."""
    with pytest.raises(ConfigError, match="none"):
        Config.from_env({**JWT_BASE_ENV,
                         "TASKFENCE_JWT_ALLOWED_ALGS": "RS256,none"})


def test_jwt_issuer_and_audience_fall_back_to_auth0_configuration():
    """Existing identity configuration keeps working without duplicate vars."""
    config = Config.from_env(JWT_BASE_ENV)
    assert config.jwt_issuer == "https://tenant.example/"
    assert config.jwt_audience == "taskfence-api"


def test_dedicated_jwt_issuer_overrides_auth0_value():
    config = Config.from_env({
        **JWT_BASE_ENV,
        "TASKFENCE_JWT_ISSUER": "https://dedicated.example/",
    })
    assert config.jwt_issuer == "https://dedicated.example/"


@pytest.mark.parametrize("key,value", [
    ("TASKFENCE_JWT_PUBLIC_KEY", "configured"),
    ("TASKFENCE_JWT_ISSUER", "https://issuer.example/"),
    ("TASKFENCE_JWT_AUDIENCE", "taskfence-api"),
])
def test_jwt_settings_without_jwt_mode_fail_at_startup(key, value):
    """Security settings present but unable to take effect are never ignored
    silently (Phase 3 hardening)."""
    with pytest.raises(ConfigError, match="TASKFENCE_AUTH_MODE"):
        Config.from_env({key: value})


def test_jwt_settings_with_explicit_local_mode_fail_at_startup():
    with pytest.raises(ConfigError, match="refusing to silently ignore"):
        Config.from_env({"TASKFENCE_AUTH_MODE": "local",
                         "TASKFENCE_JWT_ISSUER": "https://issuer.example/"})


def test_allowlist_alone_still_does_not_require_jwt_mode():
    """The allowlist has a secure default and is validated for every mode, so
    it is deliberately not part of the silent-downgrade guard."""
    config = Config.from_env({"TASKFENCE_JWT_ALLOWED_ALGS": "RS256"})
    assert config.jwt_enabled is False


def test_auth0_settings_with_local_mode_are_rejected():
    """Identity configuration must never be silently downgraded to anonymous."""
    with pytest.raises(ConfigError, match="refusing to downgrade"):
        Config.from_env({
            "TASKFENCE_AUTH_MODE": "local",
            "TASKFENCE_AUTH0_ISSUER": "https://tenant.example/",
            "TASKFENCE_AUTH0_AUDIENCE": "taskfence-api",
        })


def test_unknown_auth_mode_fails_early():
    with pytest.raises(ConfigError, match="TASKFENCE_AUTH_MODE"):
        Config.from_env({"TASKFENCE_AUTH_MODE": "anonymous"})


@pytest.mark.parametrize("key,value", [
    ("TASKFENCE_CHAT_TIMEOUT", "0"),
    ("TASKFENCE_MCP_TIMEOUT", "-3"),
    ("TASKFENCE_CHAT_TIMEOUT", "soon"),
])
def test_invalid_timeouts_fail_early(key, value):
    with pytest.raises(ConfigError):
        Config.from_env({key: value})


@pytest.mark.parametrize("key,value", [
    ("TASKFENCE_BUDGET_MAX_TOOL_CALLS", "-1"),
    ("TASKFENCE_BUDGET_MAX_OUTBOUND_CALLS", "2.5"),
    ("TASKFENCE_BUDGET_MAX_EXECUTION_SECONDS", "0"),
])
def test_invalid_budget_values_fail_early(key, value):
    with pytest.raises(ConfigError):
        Config.from_env({key: value})


def test_secret_values_are_absent_from_repr_and_str():
    config = Config.from_env({
        "TASKFENCE_DATABASE_URL": "postgresql://user:dbpassword@host:5432/db",
        "GEMINI_API_KEY": "gemini-key-value",
        "UPSTASH_REDIS_TOKEN": "upstash-token-value",
        "TASKFENCE_REAL_SLACK_WEBHOOK": "https://hooks.slack.com/services/T/B/webhookvalue",
        "TASKFENCE_ADMIN_TOKEN": "admin-token-value",
    })

    rendered = f"{config!r}\n{config}\n{config.admin_token}\n{config.real_slack_webhook:>10}"
    for leaked in ("dbpassword", "gemini-key-value", "upstash-token-value",
                   "webhookvalue", "admin-token-value"):
        assert leaked not in rendered

    # The values remain readable at the single explicit point of use.
    assert config.admin_token.reveal() == "admin-token-value"
    assert config.database_url.reveal().endswith("/db")
    assert config.real_slack_enabled is True


def test_secret_values_are_absent_from_logs(caplog):
    config = Config.from_env({"TASKFENCE_ADMIN_TOKEN": "super-secret-token"})
    caplog.set_level(logging.INFO)

    logging.getLogger("taskfence.config").info(
        "loaded config: %s token=%s", config, config.admin_token)

    assert "super-secret-token" not in caplog.text


def test_secret_wrapper_masks_every_formatting_path():
    secret = Secret("hunter2")
    assert repr(secret) == "Secret('***')"
    assert str(secret) == "***"
    assert f"{secret}" == "***"
    assert "{}".format(secret) == "***"
    assert "%s" % secret == "***"
    assert secret.reveal() == "hunter2"
    assert bool(Secret("")) is False


PG_URL = "postgresql://user:pw@db.example:5432/taskfence"


@pytest.mark.parametrize("sqlite_value", [
    "taskfence.sqlite3",
    "./taskfence.sqlite3",
    "sub/../taskfence.sqlite3",
])
def test_postgres_with_default_equivalent_sqlite_path_is_accepted(sqlite_value):
    """Documented safe rule: lexical equivalents of the default coexist with a URL."""
    config = Config.from_env({
        "TASKFENCE_DATABASE_URL": PG_URL,
        "TASKFENCE_DB": sqlite_value,
    })
    assert config.use_postgres is True
    # Preserved for the local fallback, but never read by a security path.
    from taskfence.config import PROJECT_ROOT
    assert config.sqlite_path == PROJECT_ROOT / "taskfence.sqlite3"


@pytest.mark.parametrize("sqlite_value", [
    "/tmp/taskfence.sqlite3",
    "~/taskfence.sqlite3",
    "TaskFence.sqlite3",
    "other.sqlite3",
    "sub/taskfence.sqlite3",
])
def test_postgres_with_different_sqlite_path_is_rejected(sqlite_value):
    """Conflicting-backend case: never choose silently."""
    with pytest.raises(ConfigError, match="different SQLite path"):
        Config.from_env({
            "TASKFENCE_DATABASE_URL": PG_URL,
            "TASKFENCE_DB": sqlite_value,
        })


def test_sqlite_path_without_url_is_authoritative():
    config = Config.from_env({"TASKFENCE_DB": "/tmp/local-only.sqlite3"})
    assert config.use_postgres is False
    assert config.sqlite_path.as_posix() == "/tmp/local-only.sqlite3"


def test_load_config_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("TASKFENCE_MODEL", "qwen3:14b")
    assert load_config().ollama_model == "qwen3:14b"
