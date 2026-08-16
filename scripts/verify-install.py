from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SKILL_NAME = "constraint-aware-task-execution"
SOURCE_SKILL = ROOT / "skills" / SKILL_NAME
SKILLS_PACKAGE = "skills@1.5.22"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify skills CLI discovery and cross-agent installation.")
    parser.add_argument("--skip-global", action="store_true", help="Skip isolated global installation")
    return parser.parse_args()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_path(name: str) -> str:
    candidate = shutil.which(f"{name}.cmd") if os.name == "nt" else shutil.which(name)
    if not candidate:
        raise RuntimeError(f"{name} was not found on PATH")
    return candidate


def strip_terminal_codes(value: str) -> str:
    return ANSI_ESCAPE.sub("", value).replace("\r", "")


def discovered_skill_count(listing: str) -> int:
    clean_listing = strip_terminal_codes(listing)
    pattern = rf"(?m)^[\s|│]*{re.escape(SKILL_NAME)}[\s|│]*$"
    return len(re.findall(pattern, clean_listing))


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=os.name == "nt",
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout + completed.stderr)
    return completed.stdout


def install_command(npx: str, source: Path, global_install: bool) -> list[str]:
    command = [
        npx, "--yes", SKILLS_PACKAGE, "add", str(source),
        "--skill", SKILL_NAME,
        "--agent", "codex",
        "--agent", "claude-code",
        "--agent", "opencode",
        "--copy", "--yes",
    ]
    if global_install:
        command.append("--global")
    return command


def assert_install(root: Path) -> None:
    expected = (
        root / ".agents" / "skills" / SKILL_NAME,
        root / ".claude" / "skills" / SKILL_NAME,
    )
    source_files = {
        path.relative_to(SOURCE_SKILL): digest(path)
        for path in SOURCE_SKILL.rglob("*")
        if path.is_file()
    }
    for installed_root in expected:
        if not installed_root.is_dir():
            raise FileNotFoundError(installed_root)
        installed_files = {
            path.relative_to(installed_root): digest(path)
            for path in installed_root.rglob("*")
            if path.is_file()
        }
        if installed_files != source_files:
            raise RuntimeError(f"Installed Skill tree differs from source: {installed_root}")


def assert_project_lock(project: Path) -> None:
    lock_path = project / "skills-lock.json"
    if not lock_path.is_file():
        raise FileNotFoundError(lock_path)
    if SKILL_NAME not in lock_path.read_text(encoding="utf-8"):
        raise RuntimeError(f"Installed Skill is missing from {lock_path}")


def main() -> None:
    args = parse_args()
    npx = command_path("npx")
    listing = run([npx, "--yes", SKILLS_PACKAGE, "add", str(ROOT), "--list"], ROOT)
    if discovered_skill_count(listing) != 1:
        raise RuntimeError(f"Expected exactly one discoverable Skill, got:\n{listing}")

    with tempfile.TemporaryDirectory(prefix="constraint-skill-install-") as temp:
        temp_path = Path(temp)
        project = temp_path / "project"
        project.mkdir()
        run(["git", "init", "-q", "-b", "main"], project)
        run(install_command(npx, ROOT, global_install=False), project)
        assert_install(project)
        assert_project_lock(project)

        if not args.skip_global:
            home = temp_path / "home"
            home.mkdir()
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "USERPROFILE": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "npm_config_cache": str(temp_path / "npm-cache"),
            })
            run(install_command(npx, ROOT, global_install=True), project, env=env)
            assert_install(home)

    print("Discovery, project install, global install, and content checks passed.")


if __name__ == "__main__":
    main()
