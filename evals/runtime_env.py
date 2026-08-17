"""Subprocess environment isolation for evaluation backends."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import tomllib
from typing import Iterator, Mapping


PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

# Keep subprocesses functional without copying unrelated host credentials.
INHERITED_ENVIRONMENT_ALLOWLIST = frozenset({
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "NO_PROXY",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TZ",
    "USERPROFILE",
    "WINDIR",
    *PROXY_VARIABLES,
})

ALLOWED_ENVIRONMENT_OVERRIDES = frozenset({
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "NO_COLOR",
    "NO_PROXY",
    "OLLAMA_HOST",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "TERM",
    "TZ",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
})


def _is_local_proxy(value: str) -> bool:
    normalized = value.lower().replace("[::1]", "localhost")
    return any(host in normalized for host in ("127.0.0.1", "localhost", "0.0.0.0"))


def clean_subprocess_environment(
    overrides: Mapping[str, str] | None = None,
    *,
    strip_local_proxies: bool = True,
) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in INHERITED_ENVIRONMENT_ALLOWLIST
    }
    if overrides:
        rejected = sorted(set(overrides) - ALLOWED_ENVIRONMENT_OVERRIDES)
        if rejected:
            raise ValueError(f"environment overrides are not allowed: {', '.join(rejected)}")
        env.update({str(key): str(value) for key, value in overrides.items()})
    if strip_local_proxies:
        for name in PROXY_VARIABLES:
            if name in env and _is_local_proxy(env[name]):
                env.pop(name, None)
    env.setdefault("NO_COLOR", "1")
    return env


def _secure_copy(source: Path, destination: Path) -> None:
    shutil.copyfile(source, destination)
    try:
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _write_minimal_codex_config(source: Path, destination: Path) -> None:
    """Copy provider routing and the minimal non-interactive Windows sandbox settings."""

    config_file = source / "config.toml"
    if not config_file.is_file():
        return
    try:
        config = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return
    provider_name = config.get("model_provider")
    providers = config.get("model_providers")
    if not isinstance(provider_name, str) or not isinstance(providers, dict):
        return
    provider = providers.get(provider_name)
    if not isinstance(provider, dict):
        return

    allowed_provider_keys = (
        "name",
        "base_url",
        "wire_api",
        "requires_openai_auth",
        "env_key",
    )
    lines = [
        f"model_provider = {json.dumps(provider_name)}",
        'approval_policy = "never"',
        "",
    ]
    lines.append(f"[model_providers.{json.dumps(provider_name)}]")
    for key in allowed_provider_keys:
        value = provider.get(key)
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, str):
            lines.append(f"{key} = {json.dumps(value)}")
    windows = config.get("windows")
    windows_sandbox = windows.get("sandbox") if isinstance(windows, dict) else None
    if windows_sandbox in {"elevated", "unelevated"}:
        lines.extend(["", "[windows]", f"sandbox = {json.dumps(windows_sandbox)}"])
    if len(lines) == 4:
        return
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


@contextmanager
def temporary_codex_home(
    *,
    source_home: Path | None = None,
    base_dir: Path | None = None,
) -> Iterator[Path]:
    """Create a disposable CODEX_HOME containing only required credentials."""

    configured = os.environ.get("CODEX_HOME")
    source = source_home or (Path(configured) if configured else Path.home() / ".codex")
    parent = str(base_dir.resolve()) if base_dir else None
    temp_home = Path(tempfile.mkdtemp(prefix="codex-eval-", dir=parent))
    try:
        auth_file = source / "auth.json"
        if auth_file.is_file():
            _secure_copy(auth_file, temp_home / "auth.json")
        _write_minimal_codex_config(source, temp_home / "config.toml")
        yield temp_home
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)
