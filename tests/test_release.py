import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_public", ROOT / "verify-public-config.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class ReleaseTests(unittest.TestCase):
    def test_source_requires_workspace_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("pair-server.py", "preflight.py", "runtime-agent.env.example"):
                (root / name).write_bytes((ROOT / name).read_bytes())
            errors = VERIFY.verify(root)
        self.assertIn("device/scoped_container.py: required installer source missing", errors)
        self.assertIn("device/Dockerfile: required installer source missing", errors)

    def test_archive_rejects_private_config_and_embedded_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "runtime-agent.env"
            private.write_text("secret=value\n")
            helper = root / "helper.py"
            helper.write_text("SERVER = 'http://" + ".".join(("10", "30", "1", "2")) + "'\n")
            package = root / "package.tar"
            with tarfile.open(package, "w") as archive:
                archive.add(private, arcname="release/runtime-agent.env")
                archive.add(helper, arcname="release/helper.py")
            errors = VERIFY.verify(ROOT, package)
        self.assertIn("release/runtime-agent.env: private configuration in package", errors)
        self.assertIn("release/helper.py: hardcoded IPv4 address is forbidden", errors)


if __name__ == "__main__":
    unittest.main()
