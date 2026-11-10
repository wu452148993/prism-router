"""C3 codex auth pre-flight (§4.2 audit table: codex 401, 8 cases).

Catches obvious auth-route misconfigurations before spawn so the wrapper can
return a typed `auth_401` `pre_flight` envelope instead of letting codex spend
seconds opening a WebSocket and 401'ing. Stream-level 401 still catches the
rest (residue routed to `auth_401` `stream` by `classify.py`).

Pre-flight is deliberately narrow — it judges only env plus `auth.json` and
the user `config.toml` in codex home, and passes what it cannot judge: a wrong
block costs the whole call, a wrong pass seconds
(docs/bugs/codex-preflight-custom-provider.md). Both layers (pre-flight and
stream-fallback) coexist; pre-flight is opportunistic.

Heuristics:
- `config.toml` unreadable, or beyond `tomllib`'s TOML 1.0 → pass (codex
  parses TOML 1.1 and reports a broken file itself)
- `config.toml` selects a provider other than `openai` that skips OpenAI auth
  → only its `env_key`, if declared, must reach codex non-empty through the
  env allowlist
- No `OPENAI_API_KEY` env AND no `auth.json` → no auth at all
- `OPENAI_BASE_URL` set (proxy route) AND no `OPENAI_API_KEY` → proxies
  typically reject OAuth-from-auth.json; requires explicit API key

Anything else: return None (let codex run; stream-layer catches misroutes).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from .dispatcher import _ENV_ALLOWLIST, _ENV_PREFIXES
from .errors import ErrorEnvelope, make_error

_DIRECT_OPENAI_HOSTS = frozenset({"api.openai.com"})


def _is_direct_openai_base_url(raw: str) -> bool:
    """Distinguish official OpenAI host (legitimate direct route) from proxies."""
    candidate = raw if "://" in raw else f"https://{raw}"
    host = (urlparse(candidate).hostname or "").lower()
    return host in _DIRECT_OPENAI_HOSTS


def _reaches_codex(var: str) -> bool:
    """Whether `dispatcher._snapshot_env` forwards `var` from the environment;
    it filters the upper-cased keys of Windows' `os.environ`."""
    key = var.upper()
    return key in _ENV_ALLOWLIST or key.startswith(_ENV_PREFIXES)


def _custom_provider(cfg: dict) -> tuple[str, str | None] | None:
    """`(name, env_key)` when `cfg` selects a provider that may skip OpenAI auth.

    Only the built-in `openai`, codex's default, needs it and cannot be
    overridden. A name without a user table is another built-in, a provider
    from another config layer, or one codex rejects at startup.
    `requires_openai_auth` defaults to false.
    """
    name = cfg.get("model_provider")
    if not isinstance(name, str) or name == "openai":
        return None
    providers = cfg.get("model_providers")
    table = providers.get(name) if isinstance(providers, dict) else None
    if not isinstance(table, dict):
        return name, None
    if table.get("requires_openai_auth", False) is not False:
        return None
    env_key = table.get("env_key")
    return (name, env_key) if env_key is None or isinstance(env_key, str) else None


def preflight_codex_auth(
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> ErrorEnvelope | None:
    e = os.environ if env is None else env
    raw_home = e.get("CODEX_HOME")
    if raw_home:
        codex_home = Path(raw_home)
        if not codex_home.is_absolute():
            return None  # codex resolves it against each call's cwd
    else:
        codex_home = (Path.home() if home is None else home) / ".codex"

    try:
        with (codex_home / "config.toml").open("rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        cfg = {}
    except (OSError, ValueError, RecursionError):
        return None

    provider = _custom_provider(cfg)
    if provider is not None:
        name, env_key = provider
        if env_key is None:
            return None
        if not _reaches_codex(env_key):
            return make_error(
                "auth_401", "pre_flight", retryable=False,
                raw=(
                    f"codex provider {name!r} reads its key from env {env_key}, which "
                    "prism's env allowlist never passes to codex; use a name starting "
                    f"with {' / '.join(_ENV_PREFIXES)}"
                ),
            )
        if not (e.get(env_key) or "").strip():
            return make_error(
                "auth_401", "pre_flight", retryable=False,
                raw=(
                    f"codex provider {name!r} reads its key from env {env_key}, "
                    "which is unset or empty"
                ),
            )
        return None

    auth_file = codex_home / "auth.json"
    has_api_key = bool((e.get("OPENAI_API_KEY") or "").strip())
    has_base_url = bool((e.get("OPENAI_BASE_URL") or "").strip())
    has_auth_file = auth_file.is_file()

    if not has_api_key and not has_auth_file:
        return make_error(
            "auth_401", "pre_flight", retryable=False,
            raw=(
                f"codex auth missing: no OPENAI_API_KEY env, no {auth_file}, and "
                "config.toml selects no model_provider with requires_openai_auth = false"
            ),
        )

    if has_base_url and not has_api_key:
        base = (e.get("OPENAI_BASE_URL") or "").strip()
        if _is_direct_openai_base_url(base):
            return None  # direct openai + OAuth in auth.json is a legitimate route
        return make_error(
            "auth_401", "pre_flight", retryable=False,
            raw=(
                f"OPENAI_BASE_URL set ({base!r}) but OPENAI_API_KEY missing — "
                "proxy routes typically require an explicit API key, "
                "OAuth tokens from auth.json are rarely accepted upstream"
            ),
        )

    return None
