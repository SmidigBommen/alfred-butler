import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SearxngDeploymentTests(unittest.TestCase):
    def test_settings_enable_json_and_inherit_upstream_defaults(self):
        settings = (ROOT / "infra" / "searxng" / "settings.yml").read_text()

        self.assertIn("use_default_settings: true", settings)
        self.assertRegex(settings, r"(?m)^\s+- json$")
        self.assertRegex(settings, r"(?m)^\s+limiter: false$")

    def test_persistently_rate_limited_engines_are_disabled(self):
        settings = (ROOT / "infra" / "searxng" / "settings.yml").read_text()

        self.assertIn("  - name: brave\n    disabled: true", settings)
        self.assertIn("  - name: google cse\n    disabled: true", settings)

    def test_proxy_configuration_is_explicitly_mounted(self):
        limiter = (ROOT / "infra" / "searxng" / "limiter.toml").read_text()
        makefile = (ROOT / "Makefile").read_text()

        self.assertIn("trusted_proxies", limiter)
        self.assertIn("/etc/searxng/limiter.toml:ro,Z", makefile)

    def test_makefile_pins_image_and_binds_only_to_loopback(self):
        makefile = (ROOT / "Makefile").read_text()

        image = re.search(r"^SEARXNG_IMAGE\s*:=\s*(.+)$", makefile, re.MULTILINE)
        self.assertIsNotNone(image)
        self.assertIn("@sha256:", image.group(1))
        self.assertIn("127.0.0.1:8888:8080", makefile)
        self.assertNotIn(":latest", makefile)
        self.assertIn('--header "X-Real-IP: 127.0.0.1"', makefile)

    def test_local_secrets_and_runtime_data_are_ignored(self):
        gitignore = (ROOT / ".gitignore").read_text()

        self.assertIn("infra/searxng/.env", gitignore)
        self.assertIn(".cache/", gitignore)


if __name__ == "__main__":
    unittest.main()
