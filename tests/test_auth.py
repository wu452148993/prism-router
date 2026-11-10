"""C3 codex auth pre-flight (§4.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_router.auth import preflight_codex_auth

_CUSTOM = 'model_provider = "p"\n[model_providers.p]\nname = "P"\n'


def _seed_auth_file(home: Path) -> None:
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "auth.json").write_text('{"OPENAI_API_KEY":null,"tokens":{}}', encoding="utf-8")


def _write_config(codex_home: Path, body: str) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(body, encoding="utf-8")


def test_no_key_no_authfile_returns_auth_401(tmp_path: Path):
    env = preflight_codex_auth(env={}, home=tmp_path)
    assert env is not None
    assert env["code"] == "auth_401"
    assert env["stage"] == "pre_flight"
    assert env["retryable"] is False


def test_authfile_alone_passes(tmp_path: Path):
    """OAuth-only direct route — codex CLI handles refresh; let it run."""
    _seed_auth_file(tmp_path)
    assert preflight_codex_auth(env={}, home=tmp_path) is None


def test_api_key_alone_passes(tmp_path: Path):
    assert preflight_codex_auth(env={"OPENAI_API_KEY": "sk-xxx"}, home=tmp_path) is None


def test_proxy_without_key_blocked(tmp_path: Path):
    """OPENAI_BASE_URL points to a proxy but no API key — OAuth in auth.json
    typically isn't accepted by third-party proxies; pre-flight blocks."""
    _seed_auth_file(tmp_path)
    env = preflight_codex_auth(
        env={"OPENAI_BASE_URL": "https://cc.minecrahfut.club:20001/v1"},
        home=tmp_path,
    )
    assert env is not None
    assert env["code"] == "auth_401"
    assert env["stage"] == "pre_flight"
    assert "OPENAI_BASE_URL" in env["raw"]


def test_direct_openai_base_url_with_authfile_passes(tmp_path: Path):
    """OPENAI_BASE_URL=api.openai.com + OAuth tokens is the legitimate direct
    route; pre-flight must not block it as proxy-without-key."""
    _seed_auth_file(tmp_path)
    for base_url in ("https://api.openai.com/v1", "api.openai.com/v1"):
        assert preflight_codex_auth(
            env={"OPENAI_BASE_URL": base_url},
            home=tmp_path,
        ) is None


def test_proxy_with_key_passes(tmp_path: Path):
    out = preflight_codex_auth(
        env={"OPENAI_API_KEY": "sk-xxx", "OPENAI_BASE_URL": "https://proxy.example/v1"},
        home=tmp_path,
    )
    assert out is None


def test_whitespace_only_api_key_treated_as_missing(tmp_path: Path):
    out = preflight_codex_auth(env={"OPENAI_API_KEY": "   "}, home=tmp_path)
    assert out is not None
    assert out["code"] == "auth_401"


def test_whitespace_only_base_url_ignored(tmp_path: Path):
    """Empty BASE_URL shouldn't trigger proxy-route detection."""
    _seed_auth_file(tmp_path)
    out = preflight_codex_auth(env={"OPENAI_BASE_URL": "  "}, home=tmp_path)
    assert out is None


def test_envelope_shape_strict_keys(tmp_path: Path):
    out = preflight_codex_auth(env={}, home=tmp_path)
    assert set(out.keys()) == {"code", "stage", "retryable", "raw", "stderr_tail"}
    assert out["stderr_tail"] is None


def test_self_authenticating_provider_passes(tmp_path: Path):
    """2026-10-08: a bearer-token provider ran under the CLI with neither
    OPENAI_API_KEY nor auth.json while pre-flight blocked every call. Neither
    env check concerns a provider that skips OpenAI auth."""
    _write_config(
        tmp_path / ".codex",
        _CUSTOM + 'requires_openai_auth = false\nexperimental_bearer_token = "sk-x"\n',
    )
    assert preflight_codex_auth(env={}, home=tmp_path) is None
    _seed_auth_file(tmp_path)
    assert preflight_codex_auth(
        env={"OPENAI_BASE_URL": "https://proxy.example/v1"}, home=tmp_path,
    ) is None


def test_omitted_requires_openai_auth_means_false(tmp_path: Path):
    """codex's serde default; e.g. a local provider with no credential at all."""
    _write_config(tmp_path / ".codex", _CUSTOM)
    assert preflight_codex_auth(env={}, home=tmp_path) is None


def test_provider_without_user_table_passes(tmp_path: Path):
    """A built-in other than `openai` (none needs OpenAI auth), or a name codex
    itself rejects at startup."""
    typo = 'model_provider = "typo"\n[model_providers.p]\nname = "P"\n'
    for body in ('model_provider = "ollama"\n', typo):
        _write_config(tmp_path / ".codex", body)
        assert preflight_codex_auth(env={}, home=tmp_path) is None


def test_env_key_must_be_set(tmp_path: Path):
    _write_config(tmp_path / ".codex", _CUSTOM + 'env_key = "AZURE_OPENAI_API_KEY"\n')
    assert preflight_codex_auth(env={"AZURE_OPENAI_API_KEY": "v"}, home=tmp_path) is None
    for env in ({}, {"AZURE_OPENAI_API_KEY": "  "}):
        out = preflight_codex_auth(env=env, home=tmp_path)
        assert (out["code"], out["stage"]) == ("auth_401", "pre_flight")
        assert "AZURE_OPENAI_API_KEY" in out["raw"]


def test_env_key_must_survive_the_dispatcher_allowlist(tmp_path: Path):
    """codex inherits only `dispatcher._snapshot_env`, which drops a
    MISTRAL_API_KEY set in the server's env; matching ignores case, as
    Windows env names do."""
    _write_config(tmp_path / ".codex", _CUSTOM + 'env_key = "MISTRAL_API_KEY"\n')
    out = preflight_codex_auth(env={"MISTRAL_API_KEY": "v"}, home=tmp_path)
    assert out["code"] == "auth_401"
    assert "MISTRAL_API_KEY" in out["raw"] and "allowlist" in out["raw"]
    _write_config(tmp_path / ".codex", _CUSTOM + 'env_key = "openai_compat_key"\n')
    assert preflight_codex_auth(env={"openai_compat_key": "v"}, home=tmp_path) is None


@pytest.mark.parametrize("body", [
    "",
    '[model_providers.p]\nname = "P"\nrequires_openai_auth = false\n',  # no model_provider
    _CUSTOM + "requires_openai_auth = true\n",
    _CUSTOM + 'requires_openai_auth = "false"\n',  # not a bool
    _CUSTOM + "env_key = 1\n",  # not a string
    # Built in and not overridable: its table cannot waive OpenAI auth.
    'model_provider = "openai"\n[model_providers.openai]\nname = "O"\n'
    "requires_openai_auth = false\n",
])
def test_config_without_custom_route_keeps_env_checks(tmp_path: Path, body: str):
    """Every env / auth.json case keeps the verdict it gets without config.toml."""
    cases = [({}, False), ({}, True), ({"OPENAI_BASE_URL": "https://proxy.example/v1"}, True)]
    for i, (env, seeded) in enumerate(cases):
        home = tmp_path / str(i)
        home.mkdir()
        if seeded:
            _seed_auth_file(home)
        before = preflight_codex_auth(env=env, home=home)
        _write_config(home / ".codex", body)
        assert preflight_codex_auth(env=env, home=home) == before


def test_unjudgeable_config_passes(tmp_path: Path):
    """codex parses TOML 1.1, which tomllib rejects, and reports a broken or
    unreadable file itself; blocking here would only hide a runnable config."""
    toml_1_1 = _CUSTOM + 'requires_openai_auth = false\nhttp_headers = {\n  "X-A" = "1",\n}\n'
    too_deep = "a = " + "[" * 1000 + "]" * 1000  # RecursionError in 3.11's tomllib
    for body in ("model_provider = ", toml_1_1, too_deep):
        _write_config(tmp_path / ".codex", body)
        assert preflight_codex_auth(env={}, home=tmp_path) is None
    unreadable = tmp_path / "unreadable"
    (unreadable / ".codex" / "config.toml").mkdir(parents=True)
    assert preflight_codex_auth(env={}, home=unreadable) is None


def test_codex_home_holds_config_and_authfile(tmp_path: Path):
    """codex reads both from a non-empty CODEX_HOME instead of ~/.codex."""
    _write_config(tmp_path / ".codex", _CUSTOM)
    _seed_auth_file(tmp_path)
    alt = tmp_path / "alt"
    alt.mkdir()
    env = {"CODEX_HOME": str(alt)}
    assert preflight_codex_auth(env=env, home=tmp_path)["code"] == "auth_401"
    (alt / "auth.json").write_text("{}", encoding="utf-8")
    assert preflight_codex_auth(env=env, home=tmp_path) is None
    (alt / "auth.json").unlink()
    _write_config(alt, _CUSTOM)
    assert preflight_codex_auth(env=env, home=tmp_path) is None
    assert preflight_codex_auth(env={"CODEX_HOME": ""}, home=tmp_path) is None


def test_codex_home_set_needs_no_default_home(monkeypatch, tmp_path: Path):
    """A non-empty CODEX_HOME replaces the user home, as in codex; a relative
    one resolves against each call's cwd, which pre-flight does not know."""
    def no_home():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", no_home)
    _write_config(tmp_path, _CUSTOM)
    assert preflight_codex_auth(env={"CODEX_HOME": str(tmp_path)}) is None
    assert preflight_codex_auth(env={"CODEX_HOME": "rel"}) is None
