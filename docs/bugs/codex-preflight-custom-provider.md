# codex auth pre-flight blocked providers that bring their own credential

## Symptom

On 2026-10-08 every prism `codex` call returned `auth_401` / `pre_flight`
(`codex auth missing: no OPENAI_API_KEY env and no ~/.codex/auth.json`)
without spawning codex, while `codex exec` from a terminal answered normally.
Claude Code transcripts hold 1003 prism `codex` calls; the only five
`auth_401` / `pre_flight` results are from that day.

Environment: Windows 10, codex-cli 0.161.0. `~/.codex/config.toml` selects
`model_provider = "cch-proxy"`, whose table sets `requires_openai_auth = false`
and `experimental_bearer_token`. No `OPENAI_API_KEY` in any scope, no
`auth.json`; `codex login status` reports "Not logged in". The check had
passed only because an `auth.json` happened to exist; the operator traced its
removal to the Codex desktop app re-provisioning `~/.codex` that day.

## Root cause

The pre-flight assumed codex authenticates only through `OPENAI_API_KEY` or
`auth.json`. A custom provider can carry its own credential, and codex then
needs neither. Sources below are `openai/codex` tag `rust-v0.161.0` (commit
`979011409de0`), the installed version:

- `requires_openai_auth` is `#[serde(default)]`, i.e. false unless set
  (`codex-rs/model-provider-info/src/lib.rs:184-190`).
- `resolve_provider_auth` takes `env_key` first — unset or blank is a hard
  error, without fallback (`lib.rs:474-490`,
  `model-provider/src/auth.rs:292-305`) — then `experimental_bearer_token`.
  When neither yields a token, `requires_openai_auth` is false and no command
  `auth` table is configured, it returns an unauthenticated auth provider
  (`auth.rs:197-222`).

## Fix

`preflight_codex_auth` reads `config.toml` in codex home (`$CODEX_HOME` when
non-empty, else `~/.codex`: `utils/home-dir/src/lib.rs:13-62`) before the two
env checks:

- A selected provider other than `openai` passes when its user table omits
  `requires_openai_auth` or sets it to false; `true`, or a value of the wrong
  type, keeps the env checks. Without a user table the provider is another
  built-in (`ollama`, `lmstudio`, `amazon-bedrock*`, none needing OpenAI auth:
  `model-provider-info/src/lib.rs:651-683`), one from another config layer
  (see Not covered), or one codex rejects at startup
  (`core/src/config/mod.rs:3830-3839`; measured:
  ``Model provider `typo` not found``, exit 1 within 1 s). A declared
  `env_key` must still be non-empty and pass `dispatcher._snapshot_env`'s
  allowlist, since codex inherits only that (`OPENAI_*`, `AZURE_*`,
  `ANTHROPIC_*`, fixed names): the docs' own example
  `env_key = "MISTRAL_API_KEY"` [1] never reaches codex, which then fails with
  ``Missing environment variable: `X`.`` (`protocol/src/error.rs:870`), a
  text `classify._RE_401` does not match.
- `model_provider = "openai"` keeps the env checks: the built-in `openai`
  cannot be overridden (`config/src/config_toml.rs:67-73, 924-947`; [1]).
- A `config.toml` that exists but cannot be read or parsed passes. codex
  parses TOML 1.1 (`codex-rs/Cargo.lock`: `toml 0.9.11+spec-1.1.0`; toml-rs
  added 1.1 parsing in 0.9.10 [2]) while Python 3.11's `tomllib` parses 1.0
  only (1.1 from 3.15 [3]); falling back to the env checks would block a file
  codex runs. On the affected machine's 3.11.3, nesting 500 deep raised
  `RecursionError` rather than `ValueError`; it is caught too. Without a user
  `config.toml` the env checks apply (absent other layers, codex defaults to
  `openai`: `core/src/config/mod.rs:3826-3829`).
- A relative `CODEX_HOME` passes: codex resolves it against each call's cwd
  (`utils/home-dir/src/lib.rs:20-62`), which pre-flight does not know. A set
  `CODEX_HOME` no longer touches the user home, as in codex.
- `auth.json` is looked up in codex home, where codex keeps it
  (`login/src/auth/storage.rs:162-163`), instead of always `~/.codex`.
- `server.py`: a call with `profile` skips pre-flight. `--profile <p>` layers
  `$CODEX_HOME/<p>.config.toml` over the base file
  (`core/src/config/mod.rs:270, 2033-2041`; `config/src/loader/mod.rs:289-341`)
  and may switch the provider, which pre-flight does not read.

Inputs pre-flight knows it cannot judge pass because the two errors are not
symmetric. Measured on the affected machine with a scratch `CODEX_HOME`, then
through prism's parser and `classify`: a wrong pass toward OpenAI without
credentials ends after 20 s as `auth_401` / `stream` (codex retries the 401
five times); a broken `config.toml` ends within 1 s as `spawn_failed`, codex's
line/column message in `stderr_tail`; a file using a TOML 1.1 multi-line
inline table starts codex normally. A wrong block makes the call impossible,
as on 2026-10-08.

## Not covered

- System and managed layers — `%ProgramData%\OpenAI\Codex\config.toml`,
  `requirements.toml`, cloud bundles — can define, replace or force providers
  (`config/src/loader/mod.rs:107-134`,
  `config/src/model_provider_requirements.rs:23-40`); pre-flight reads only
  the user file. A lower layer that defines a provider requiring OpenAI auth,
  or sets `requires_openai_auth = true` where the user table omits the key,
  yields a wrong pass; a managed table that replaces a user table declaring
  an unset `env_key` with a bearer-token provider yields a wrong block.
  `%ProgramData%\OpenAI\Codex\` does not exist on the affected machine.
- `env_key` variables outside the allowlist stay unforwarded; the error names
  the forwarded prefixes rather than widening the dispatcher's env.
  `PYTHONNOUSERSITE` and `PYTHONUNBUFFERED`, which the dispatcher seeds with
  `1`, count as not forwarded.

## Verification

- `tests/test_auth.py` (nine new tests) and
  `tests/test_server.py::test_codex_auth_preflight_skipped_for_profile_calls`:
  against the pre-fix source (`git archive` + `PYTHONPATH`) 9 of their 16
  cases fail and the 7 asserting unchanged behaviour pass. All markers
  (`pytest -m ""`): 592 passed, 2 skipped.
- Affected machine, final code: `preflight_codex_auth()` returned the envelope
  above before the fix and `None` after; a fresh server's `codex` call
  answered `PROBE_OK` with and without `profile`.

[1]: https://developers.openai.com/codex/config-advanced (fetched 2026-10-08)
[2]: https://github.com/toml-rs/toml/blob/main/crates/toml/CHANGELOG.md
[3]: https://docs.python.org/3.15/library/tomllib.html
