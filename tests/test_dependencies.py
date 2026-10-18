"""Dependency-manifest regression guard (Phase 3 hardening).

`taskfence/auth.py` imports `cryptography` directly, but PyJWT declares
cryptography only under its `crypto` extra. Declaring bare `PyJWT` therefore
left a clean install unable to import the gateway in ANY mode — the failure is
at import time, not at first use (`gateway.py` imports `taskfence.auth`
unconditionally). These checks fail if the manifest loses that requirement
again.
"""

from importlib.util import find_spec
from pathlib import Path

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def _declared_pyjwt_line() -> str:
    """The requirement line naming PyJWT, with comments and blanks removed."""
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.lower().startswith("pyjwt"):
            return stripped
    raise AssertionError("requirements.txt no longer declares PyJWT")


def test_pyjwt_is_declared_with_the_crypto_extra():
    """Bare `PyJWT` does not install cryptography; the extra is required."""
    assert _declared_pyjwt_line().lower() == "pyjwt[crypto]"


def test_the_environment_satisfies_the_manifest():
    """cryptography must be importable, since auth.py imports it directly."""
    assert find_spec("cryptography") is not None


def test_auth_module_imports():
    """The module that needs the key material must import cleanly."""
    import taskfence.auth as auth

    assert auth.JWTVerifier is not None
