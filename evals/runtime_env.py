"""Subprocess environment isolation for evaluation backends."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Iterator, Mapping


PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _is_local_proxy(value: str) -> bool:
    normalized = value.lower().replace("[::1]", "localhost")
    return any(host in normalized for host in ("127.0.0.1", "localhost", "0.0.0.0"))


def clean_subprocess_environment(
    overrides: Mapping[str, str] | None = None,
    *,
    strip_local_proxies: bool = True,
) -> dict[str, str]:
    env = dict(os.environ)
    if overrides:
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
        yield temp_home
    finally:
        shutil.rmtree(temp_home, ignore_errors=True)
