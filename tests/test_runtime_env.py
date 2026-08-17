from __future__ import annotations

from pathlib import Path
import tempfile
import tomllib
import unittest

from evals.runtime_env import temporary_codex_home


class TemporaryCodexHomeTests(unittest.TestCase):
    def test_copies_auth_and_only_active_provider_routing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
            (source / "config.toml").write_text(
                """
model = "ignored-model"
model_provider = "Relay"
notify = ["ignored.exe"]

[model_providers.Relay]
name = "Relay"
wire_api = "responses"
requires_openai_auth = true
base_url = "https://relay.example.test"

[features]
plugins = true

[windows]
sandbox = "elevated"

[mcp_servers.unsafe]
command = "ignored.exe"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with temporary_codex_home(source_home=source) as isolated:
                self.assertEqual(
                    (isolated / "auth.json").read_text(encoding="utf-8"),
                    '{"token":"secret"}',
                )
                copied = tomllib.loads(
                    (isolated / "config.toml").read_text(encoding="utf-8")
                )

        self.assertEqual(copied["model_provider"], "Relay")
        self.assertEqual(
            copied["model_providers"]["Relay"]["base_url"],
            "https://relay.example.test",
        )
        self.assertEqual(copied["approval_policy"], "never")
        self.assertEqual(copied["windows"]["sandbox"], "elevated")
        self.assertNotIn("features", copied)
        self.assertNotIn("mcp_servers", copied)
        self.assertNotIn("notify", copied)


if __name__ == "__main__":
    unittest.main()
