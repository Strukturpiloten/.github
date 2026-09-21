from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "lockfile_release_age.py"
SPEC = importlib.util.spec_from_file_location("lockfile_release_age", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


class LockfileReleaseAgeTests(unittest.TestCase):
    now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)

    def test_cargo_diff_checks_only_introduced_registry_versions(self) -> None:
        base = '''
version = 4
[[package]]
name = "old"
version = "1.0.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
'''
        head = base + '''
[[package]]
name = "new"
version = "2.0.0"
source = "registry+https://index.crates.io/"
'''
        introduced = guard.introduced_dependencies("cargo", base, head)
        self.assertEqual(
            introduced,
            {guard.Dependency("cargo", "new", "2.0.0", "crates.io")},
        )
        self.assertEqual(guard.introduced_dependencies("cargo", head, head), set())

    def test_malformed_cargo_package_fails_closed(self) -> None:
        with self.assertRaisesRegex(guard.VerificationError, "malformed package"):
            guard.introduced_dependencies("cargo", None, 'version = 4\npackage = ["bad"]\n')

    def test_cargo_git_dependency_fails_closed(self) -> None:
        head = '''
version = 4
[[package]]
name = "git-only"
version = "1.0.0"
source = "git+https://example.invalid/repository"
'''
        [dependency] = guard.introduced_dependencies("cargo", None, head)
        failures = guard.verify_dependencies(
            [dependency],
            now=self.now,
            minimum_age=timedelta(hours=72),
        )
        self.assertEqual(
            failures,
            ["cargo:git-only@1.0.0: dependency source is not a supported public registry"],
        )

    def test_npm_scoped_dependency_and_local_link(self) -> None:
        head = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root", "version": "1.0.0"},
                    "node_modules/@scope/tool": {
                        "version": "3.2.1",
                        "resolved": "https://registry.npmjs.org/@scope/tool/-/tool-3.2.1.tgz",
                    },
                    "node_modules/local": {
                        "version": "1.0.0",
                        "resolved": "file:../local",
                    },
                },
            }
        )
        introduced = guard.introduced_dependencies("npm", None, head)
        self.assertEqual(
            introduced,
            {guard.Dependency("npm", "@scope/tool", "3.2.1", "registry.npmjs.org")},
        )

    def test_npm_alias_uses_registry_identity(self) -> None:
        head = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root", "version": "1.0.0"},
                    "node_modules/alias": {
                        "name": "actual-package",
                        "version": "2.1.0",
                        "resolved": (
                            "https://registry.npmjs.org/actual-package/-/"
                            "actual-package-2.1.0.tgz"
                        ),
                    },
                },
            }
        )
        self.assertEqual(
            guard.introduced_dependencies("npm", None, head),
            {
                guard.Dependency(
                    "npm", "actual-package", "2.1.0", "registry.npmjs.org"
                )
            },
        )

    def test_npm_spoofed_name_fails_closed(self) -> None:
        head = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root", "version": "1.0.0"},
                    "node_modules/new-package": {
                        "name": "old-package",
                        "version": "1.0.0",
                        "resolved": (
                            "https://registry.npmjs.org/new-package/-/new-package-1.0.0.tgz"
                        ),
                    },
                },
            }
        )
        with self.assertRaisesRegex(guard.VerificationError, "does not match"):
            guard.introduced_dependencies("npm", None, head)

    def test_malformed_npm_package_fails_closed(self) -> None:
        head = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "root", "version": "1.0.0"},
                    "node_modules/missing-version": {
                        "resolved": (
                            "https://registry.npmjs.org/missing-version/-/"
                            "missing-version-1.0.0.tgz"
                        )
                    },
                },
            }
        )
        with self.assertRaisesRegex(guard.VerificationError, "valid version"):
            guard.introduced_dependencies("npm", None, head)

    def test_npm_network_link_fails_closed(self) -> None:
        for resolved in (
            "git+https://example.invalid/private.git",
            "git@github.com:organization/private.git",
        ):
            with self.subTest(resolved=resolved):
                head = json.dumps(
                    {
                        "lockfileVersion": 3,
                        "packages": {
                            "": {"name": "root", "version": "1.0.0"},
                            "node_modules/network-link": {
                                "link": True,
                                "resolved": resolved,
                            },
                        },
                    },
                )
                with self.assertRaisesRegex(guard.VerificationError, "invalid local link"):
                    guard.introduced_dependencies("npm", None, head)

    def test_old_release_passes_and_young_release_fails(self) -> None:
        old = guard.Dependency("cargo", "old", "1.0.0", "crates.io")
        young = guard.Dependency("npm", "young", "2.0.0", "registry.npmjs.org")
        timestamps = {
            old: self.now - timedelta(hours=73),
            young: self.now - timedelta(hours=2),
        }
        failures = guard.verify_dependencies(
            [old, young],
            now=self.now,
            minimum_age=timedelta(hours=72),
            lookup=timestamps.__getitem__,
        )
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith("npm:young@2.0.0: released "))

    def test_release_at_exact_cutoff_passes(self) -> None:
        dependency = guard.Dependency("cargo", "boundary", "1.0.0", "crates.io")
        self.assertEqual(
            guard.verify_dependencies(
                [dependency],
                now=self.now,
                minimum_age=timedelta(hours=72),
                lookup=lambda _: self.now - timedelta(hours=72),
            ),
            [],
        )

    def test_lookup_deadline_fails_closed(self) -> None:
        dependency = guard.Dependency("cargo", "slow", "1.0.0", "crates.io")

        def slow(_: guard.Dependency) -> datetime:
            time.sleep(0.05)
            return self.now

        failures = guard.verify_dependencies(
            [dependency],
            now=self.now,
            minimum_age=timedelta(hours=72),
            lookup=slow,
            lookup_deadline_seconds=0.001,
            max_workers=1,
        )
        self.assertEqual(
            failures,
            ["cargo:slow@1.0.0: registry lookup deadline was exhausted"],
        )

    def test_lookup_deadline_bounds_process_runtime(self) -> None:
        source = f'''
import importlib.util
import sys
import time
from datetime import datetime, timedelta, timezone
spec = importlib.util.spec_from_file_location("deadline_guard", {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
def slow(_):
    time.sleep(2)
    return datetime.now(timezone.utc)
dependency = module.Dependency("cargo", "slow", "1.0.0", "crates.io")
failures = module.verify_dependencies(
    [dependency],
    now=datetime.now(timezone.utc),
    minimum_age=timedelta(hours=72),
    lookup=slow,
    lookup_deadline_seconds=0.01,
    max_workers=1,
)
assert failures == ["cargo:slow@1.0.0: registry lookup deadline was exhausted"]
'''
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-c", source],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 1.5)

    def test_missing_registry_timestamp_fails_closed(self) -> None:
        dependency = guard.Dependency("cargo", "missing", "1.0.0", "crates.io")
        with mock.patch.object(guard, "_request_json", return_value={"version": {}}):
            failures = guard.verify_dependencies(
                [dependency],
                now=self.now,
                minimum_age=timedelta(hours=72),
            )
        self.assertEqual(
            failures,
            ["cargo:missing@1.0.0: registry metadata omitted the release timestamp"],
        )

    def test_registry_failure_is_reported_without_a_url(self) -> None:
        dependency = guard.Dependency("cargo", "private-name", "1.0.0", "crates.io")

        def fail(_: guard.Dependency) -> datetime:
            raise guard.VerificationError("registry metadata request failed")

        failures = guard.verify_dependencies(
            [dependency],
            now=self.now,
            minimum_age=timedelta(hours=72),
            lookup=fail,
        )
        self.assertEqual(
            failures,
            ["cargo:private-name@1.0.0: registry metadata request failed"],
        )
        self.assertNotIn("http", failures[0])

    def test_changed_lockfiles_selects_supported_names_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self._git(repository, "init", "--initial-branch=main")
            self._git(repository, "config", "user.email", "test@example.invalid")
            self._git(repository, "config", "user.name", "Test")
            (repository / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
            (repository / "notes.txt").write_text("base\n", encoding="utf-8")
            self._git(repository, "add", "Cargo.lock", "notes.txt")
            self._git(repository, "commit", "-m", "base")
            base = self._git(repository, "rev-parse", "HEAD").strip()
            (repository / "Cargo.lock").write_text("version = 4\n# changed\n", encoding="utf-8")
            (repository / "notes.txt").write_text("head\n", encoding="utf-8")
            self._git(repository, "add", "Cargo.lock", "notes.txt")
            self._git(repository, "commit", "-m", "head")
            head = self._git(repository, "rev-parse", "HEAD").strip()
            self.assertEqual(guard.changed_lockfiles(repository, base, head), ["Cargo.lock"])

    def test_cli_rejects_malformed_changed_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self._git(repository, "init", "--initial-branch=main")
            self._git(repository, "config", "user.email", "test@example.invalid")
            self._git(repository, "config", "user.name", "Test")
            (repository / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
            self._git(repository, "add", "Cargo.lock")
            self._git(repository, "commit", "-m", "base")
            base = self._git(repository, "rev-parse", "HEAD").strip()
            (repository / "Cargo.lock").write_text(
                'version = 4\npackage = ["bad"]\n', encoding="utf-8"
            )
            self._git(repository, "add", "Cargo.lock")
            self._git(repository, "commit", "-m", "head")
            head = self._git(repository, "rev-parse", "HEAD").strip()
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--base",
                    base,
                    "--head",
                    head,
                    "--repository-root",
                    str(repository),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("failed closed", result.stderr)
            self.assertNotIn("http", result.stderr)

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout


if __name__ == "__main__":
    unittest.main()
