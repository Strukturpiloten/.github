from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
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
    first_party_names = ("compose-lens", "podman-lens", "quadlet-lens", "docker-lens")

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

    def test_first_party_crates_pass_after_successful_registry_lookup(self) -> None:
        for name in self.first_party_names:
            with self.subTest(name=name):
                dependency = guard.Dependency("cargo", name, "1.0.0", "crates.io")
                with mock.patch.object(
                    guard,
                    "_request_json",
                    return_value={"version": {"created_at": "2026-09-21T13:00:00+02:00"}},
                ):
                    self.assertEqual(
                        guard.verify_dependencies(
                            [dependency], now=self.now, minimum_age=timedelta(hours=72)
                        ),
                        [],
                    )

    def test_first_party_lockfile_accepts_only_canonical_crates_io_sources(self) -> None:
        for name in self.first_party_names:
            for source in (
                "registry+https://github.com/rust-lang/crates.io-index",
                "registry+https://index.crates.io/",
            ):
                with self.subTest(name=name, source=source):
                    head = f'''
version = 4
[[package]]
name = "{name}"
version = "1.0.0"
source = "{source}"
'''
                    dependencies = guard.introduced_dependencies("cargo", None, head)
                    self.assertEqual(
                        dependencies, {guard.Dependency("cargo", name, "1.0.0", "crates.io")}
                    )
                    self.assertEqual(
                        guard.verify_dependencies(
                            dependencies,
                            now=self.now,
                            minimum_age=timedelta(hours=72),
                            lookup=lambda _: self.now - timedelta(hours=1),
                        ),
                        [],
                    )

    def test_first_party_exception_retains_third_party_delay(self) -> None:
        dependencies = [
            guard.Dependency("cargo", name, "1.0.0", "crates.io")
            for name in self.first_party_names
        ]
        third_party = guard.Dependency("cargo", "serde", "1.0.0", "crates.io")
        old = guard.Dependency("cargo", "old", "1.0.0", "crates.io")
        npm_head = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/parent/node_modules/transitive": {
                        "version": "1.0.0",
                        "resolved": "https://registry.npmjs.org/transitive/-/transitive-1.0.0.tgz",
                    }
                },
            }
        )
        [transitive] = guard.introduced_dependencies("npm", None, npm_head)
        failures = guard.verify_dependencies(
            [*dependencies, third_party, old, transitive],
            now=self.now,
            minimum_age=timedelta(hours=72),
            lookup=lambda dependency: self.now
            - timedelta(hours=73 if dependency == old else 1),
        )
        self.assertEqual(len(failures), 2)
        self.assertTrue(failures[0].startswith(f"{third_party.display}: released "))
        self.assertTrue(failures[1].startswith(f"{transitive.display}: released "))

    def test_first_party_exception_requires_exact_name_source_and_ecosystem(self) -> None:
        dependencies = [
            guard.Dependency("npm", name, "1.0.0", "registry.npmjs.org")
            for name in self.first_party_names
        ]
        dependencies.extend(
            (
                guard.Dependency("npm", "compose-lens", "1.0.0", "crates.io"),
                guard.Dependency("cargo", "compose-lens", "1.0.0", "registry.npmjs.org"),
                guard.Dependency("cargo", "compose-lens", "1.0.0", "crates.io.example.invalid"),
                guard.Dependency("cargo", "compose-lens-extra", "1.0.0", "crates.io"),
                guard.Dependency("cargo", "Compose-Lens", "1.0.0", "crates.io"),
                guard.Dependency("cargo", "compose_lens", "1.0.0", "crates.io"),
                guard.Dependency("cargo", "boxferry", "1.0.0", "crates.io"),
            )
        )
        failures = guard.verify_dependencies(
            dependencies,
            now=self.now,
            minimum_age=timedelta(hours=72),
            lookup=lambda _: self.now - timedelta(hours=1),
        )
        self.assertEqual(len(failures), len(dependencies))
        self.assertTrue(all(": released " in failure for failure in failures))

    def test_first_party_lockfile_rejects_noncanonical_sources(self) -> None:
        for source in (
            "git+https://github.com/Strukturpiloten/compose-lens",
            "registry+https://index.crates.io",
            "registry+https://index.crates.io/?trusted=true",
            "registry+https://index.crates.io.example.invalid/",
            "registry+https://github.com/rust-lang/crates.io-index/",
            "registry+https://private.example.invalid/",
        ):
            with self.subTest(source=source):
                head = f'''
version = 4
[[package]]
name = "compose-lens"
version = "1.0.0"
source = "{source}"
'''
                dependencies = guard.introduced_dependencies("cargo", None, head)
                with mock.patch.object(
                    guard, "_request_json", side_effect=AssertionError("unexpected request")
                ):
                    self.assertEqual(
                        guard.verify_dependencies(
                            dependencies, now=self.now, minimum_age=timedelta(hours=72)
                        ),
                        ["cargo:compose-lens@1.0.0: dependency source is not a supported public registry"],
                    )

    def test_first_party_missing_or_malformed_metadata_fails_closed(self) -> None:
        for name in self.first_party_names:
            for metadata, message in (
                (None, "crates.io metadata has an unexpected shape"),
                ({"version": None}, "crates.io metadata has an unexpected shape"),
                ({"version": {}}, "registry metadata omitted the release timestamp"),
                (
                    {"version": {"created_at": 123}},
                    "registry metadata omitted the release timestamp",
                ),
                (
                    {"version": {"created_at": "invalid"}},
                    "registry metadata contained an invalid release timestamp",
                ),
                (
                    {"version": {"created_at": "2026-09-21T11:00:00"}},
                    "registry release timestamp has no timezone",
                ),
            ):
                with self.subTest(name=name, metadata=metadata):
                    dependency = guard.Dependency("cargo", name, "1.0.0", "crates.io")
                    with mock.patch.object(guard, "_request_json", return_value=metadata):
                        self.assertEqual(
                            guard.verify_dependencies(
                                [dependency], now=self.now, minimum_age=timedelta(hours=72)
                            ),
                            [f"{dependency.display}: {message}"],
                        )

    def test_first_party_lookup_failure_and_deadline_fail_closed(self) -> None:
        for name in self.first_party_names:
            dependency = guard.Dependency("cargo", name, "1.0.0", "crates.io")
            with self.subTest(name=name, failure="request"):
                with mock.patch.object(
                    guard,
                    "_request_json",
                    side_effect=guard.VerificationError("registry metadata request failed"),
                ):
                    self.assertEqual(
                        guard.verify_dependencies(
                            [dependency], now=self.now, minimum_age=timedelta(hours=72)
                        ),
                        [f"{dependency.display}: registry metadata request failed"],
                    )
            with self.subTest(name=name, failure="deadline"):

                def slow(_: guard.Dependency) -> datetime:
                    time.sleep(1)
                    return self.now

                self.assertEqual(
                    guard.verify_dependencies(
                        [dependency],
                        now=self.now,
                        minimum_age=timedelta(hours=72),
                        lookup=slow,
                        lookup_deadline_seconds=0.01,
                        max_workers=1,
                    ),
                    [f"{dependency.display}: registry lookup deadline was exhausted"],
                )

    def test_first_party_cutoff_current_and_future_timestamps(self) -> None:
        for name in self.first_party_names:
            dependency = guard.Dependency("cargo", name, "1.0.0", "crates.io")
            for age in (
                timedelta(hours=73),
                timedelta(hours=72),
                timedelta(),
                -timedelta(seconds=1),
            ):
                with self.subTest(name=name, age=age):
                    failures = guard.verify_dependencies(
                        [dependency],
                        now=self.now,
                        minimum_age=timedelta(hours=72),
                        lookup=lambda _: self.now - age,
                    )
                    if age < timedelta():
                        self.assertEqual(len(failures), 1)
                        self.assertIn("after verification time", failures[0])
                    else:
                        self.assertEqual(failures, [])

    def test_first_party_invalid_lookup_timestamp_fails_closed(self) -> None:
        class MissingOffset(tzinfo):
            def utcoffset(self, value: datetime | None) -> None:
                return None

        dependency = guard.Dependency("cargo", "compose-lens", "1.0.0", "crates.io")
        for timestamp in (
            None,
            "2026-09-21T11:00:00Z",
            self.now.replace(tzinfo=None),
            self.now.replace(tzinfo=MissingOffset()),
        ):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(
                    guard.verify_dependencies(
                        [dependency],
                        now=self.now,
                        minimum_age=timedelta(hours=72),
                        lookup=lambda _: timestamp,
                    ),
                    [f"{dependency.display}: registry metadata returned an invalid release timestamp"],
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
