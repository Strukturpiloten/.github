#!/usr/bin/env python3
"""Fail closed when a candidate lockfile introduces dependencies younger than a cutoff."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from multiprocessing.connection import wait as wait_for_connections
from pathlib import Path
from typing import Callable, Iterable


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_INTRODUCED_DEPENDENCIES = 1_000
LOOKUP_DEADLINE_SECONDS = 90
LOOKUP_WORKERS = 8
REQUEST_ATTEMPTS = 2
REQUEST_TIMEOUT_SECONDS = 5
USER_AGENT = "Strukturpiloten-lockfile-release-age/1"


class VerificationError(RuntimeError):
    """A dependency cannot be verified without weakening the age policy."""


@dataclass(frozen=True, order=True)
class Dependency:
    ecosystem: str
    name: str
    version: str
    source: str

    @property
    def display(self) -> str:
        return f"{self.ecosystem}:{self.name}@{self.version}"


def _cargo_dependencies(contents: str) -> set[Dependency]:
    document = tomllib.loads(contents)
    packages = document.get("package", [])
    if not isinstance(packages, list):
        raise VerificationError("Cargo.lock does not contain a package list")
    dependencies: set[Dependency] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise VerificationError("Cargo.lock contains a malformed package record")
        name = package.get("name")
        version = package.get("version")
        source = package.get("source")
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise VerificationError("Cargo.lock contains a package without a valid name and version")
        if source is None:
            continue
        if not isinstance(source, str):
            dependencies.add(Dependency("cargo", name, version, "unverifiable"))
        elif source in {
            "registry+https://github.com/rust-lang/crates.io-index",
            "registry+https://index.crates.io/",
        }:
            dependencies.add(Dependency("cargo", name, version, "crates.io"))
        else:
            dependencies.add(Dependency("cargo", name, version, "unverifiable"))
    return dependencies


def _installed_npm_name(path: str) -> str | None:
    marker = "node_modules/"
    if marker not in path:
        return None
    return path.rsplit(marker, 1)[1] or None


def _npm_registry_name(resolved: str, version: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(resolved)
        port = parsed.port
    except ValueError as error:
        raise VerificationError("package-lock.json contains an invalid resolved URL") from error
    if parsed.hostname != "registry.npmjs.org":
        return None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VerificationError("package-lock.json contains a non-canonical npm registry URL")

    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    try:
        separator = parts.index("-")
    except ValueError as error:
        raise VerificationError("package-lock.json contains an invalid npm tarball URL") from error
    name_parts = parts[:separator]
    if len(name_parts) == 1:
        name = name_parts[0]
    elif len(name_parts) == 2 and name_parts[0].startswith("@"):
        name = "/".join(name_parts)
    else:
        raise VerificationError("package-lock.json contains an invalid npm package identity")
    if not name or separator + 2 != len(parts):
        raise VerificationError("package-lock.json contains an invalid npm tarball URL")
    basename = name.rsplit("/", 1)[-1]
    if parts[-1] != f"{basename}-{version}.tgz":
        raise VerificationError("package-lock.json tarball identity does not match its version")
    return name


def _npm_dependencies(contents: str) -> set[Dependency]:
    document = json.loads(contents)
    packages = document.get("packages")
    if not isinstance(packages, dict):
        raise VerificationError("package-lock.json does not contain a packages mapping")

    dependencies: set[Dependency] = set()
    for path, package in packages.items():
        if not isinstance(path, str) or not isinstance(package, dict):
            raise VerificationError("package-lock.json contains a malformed package record")
        if path == "":
            continue
        installed_name = _installed_npm_name(path)
        if installed_name is None:
            continue
        declared_name = package.get("name")
        if declared_name is not None and (
            not isinstance(declared_name, str) or not declared_name
        ):
            raise VerificationError("package-lock.json contains an invalid package name")
        link = package.get("link")
        if link is not None and not isinstance(link, bool):
            raise VerificationError("package-lock.json contains an invalid link marker")
        resolved = package.get("resolved")
        if link is True:
            if not isinstance(resolved, str) or not _is_local_npm_link(resolved):
                raise VerificationError("package-lock.json contains an invalid local link")
            continue
        version = package.get("version")
        if not isinstance(version, str) or not version:
            raise VerificationError("package-lock.json contains a package without a valid version")
        if isinstance(resolved, str) and resolved.startswith("file:"):
            continue
        registry_name = _npm_registry_name(resolved, version) if isinstance(resolved, str) else None
        if registry_name is not None:
            if declared_name is not None and declared_name != registry_name:
                raise VerificationError(
                    "package-lock.json package name does not match its registry tarball"
                )
            if declared_name is None and installed_name != registry_name:
                raise VerificationError(
                    "package-lock.json alias does not declare its registry package name"
                )
            dependencies.add(
                Dependency("npm", registry_name, version, "registry.npmjs.org")
            )
        else:
            dependencies.add(
                Dependency("npm", declared_name or installed_name, version, "unverifiable")
            )
    return dependencies


def _is_local_npm_link(resolved: str) -> bool:
    if not resolved or "\x00" in resolved:
        return False
    try:
        parsed = urllib.parse.urlsplit(resolved)
    except ValueError:
        return False
    if parsed.scheme == "file":
        return not parsed.netloc and bool(parsed.path)
    return (
        not parsed.scheme
        and not parsed.netloc
        and ":" not in resolved
        and not resolved.startswith(("/", "\\"))
    )


def introduced_dependencies(kind: str, base: str | None, head: str) -> set[Dependency]:
    parser = _cargo_dependencies if kind == "cargo" else _npm_dependencies
    current = parser(head)
    previous = parser(base) if base is not None else set()
    return current - previous


def _git_text(repository: Path, revision: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=repository,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8")


def changed_lockfiles(repository: Path, base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AMR", base, head],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return sorted(
        path
        for path in result.stdout.splitlines()
        if Path(path).name in {"Cargo.lock", "package-lock.json"}
    )


def dependencies_from_git(repository: Path, base: str, head: str) -> set[Dependency]:
    dependencies: set[Dependency] = set()
    for path in changed_lockfiles(repository, base, head):
        head_text = _git_text(repository, head, path)
        if head_text is None:
            raise VerificationError(f"candidate lockfile is unavailable: {path}")
        base_text = _git_text(repository, base, path)
        kind = "cargo" if Path(path).name == "Cargo.lock" else "npm"
        dependencies.update(introduced_dependencies(kind, base_text, head_text))
    return dependencies


def _request_json(url: str, attempts: int = REQUEST_ATTEMPTS) -> object:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise VerificationError("registry metadata response exceeded the size limit")
            return json.loads(data)
        except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.URLError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(1 << attempt)
    raise VerificationError("registry metadata request failed") from last_error


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise VerificationError("registry metadata omitted the release timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise VerificationError("registry metadata contained an invalid release timestamp") from error
    if parsed.tzinfo is None:
        raise VerificationError("registry release timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def registry_release_time(dependency: Dependency) -> datetime:
    if dependency.source == "crates.io":
        name = urllib.parse.quote(dependency.name, safe="")
        version = urllib.parse.quote(dependency.version, safe="")
        metadata = _request_json(f"https://crates.io/api/v1/crates/{name}/{version}")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("version"), dict):
            raise VerificationError("crates.io metadata has an unexpected shape")
        return _parse_timestamp(metadata["version"].get("created_at"))

    if dependency.source == "registry.npmjs.org":
        name = urllib.parse.quote(dependency.name, safe="")
        metadata = _request_json(f"https://registry.npmjs.org/{name}")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("time"), dict):
            raise VerificationError("npm metadata has an unexpected shape")
        return _parse_timestamp(metadata["time"].get(dependency.version))

    raise VerificationError("dependency source is not a supported public registry")


def _lookup_worker(
    sender: object,
    dependency: Dependency,
    lookup: Callable[[Dependency], datetime],
) -> None:
    try:
        released = lookup(dependency)
        if not isinstance(released, datetime) or released.tzinfo is None:
            raise VerificationError("registry metadata returned an invalid release timestamp")
        sender.send(("ok", released.astimezone(timezone.utc)))
    except VerificationError as error:
        sender.send(("error", str(error)))
    except Exception:
        sender.send(("error", "unexpected registry lookup failure"))
    finally:
        sender.close()


def _terminate_workers(active: dict[object, tuple[multiprocessing.Process, Dependency]]) -> None:
    workers = [process for process, _ in active.values()]
    for receiver in active:
        receiver.close()
    for process in workers:
        if process.is_alive():
            process.terminate()
    shutdown_deadline = time.monotonic() + 2
    for process in workers:
        process.join(timeout=max(0, shutdown_deadline - time.monotonic()))
    for process in workers:
        if process.is_alive():
            process.kill()
    for process in workers:
        process.join(timeout=0.25)


def _isolated_lookups(
    dependencies: list[Dependency],
    *,
    lookup: Callable[[Dependency], datetime],
    deadline_seconds: float,
    max_workers: int,
) -> dict[Dependency, datetime | VerificationError]:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        return {
            dependency: VerificationError("process-isolated registry lookup is unavailable")
            for dependency in dependencies
        }

    results: dict[Dependency, datetime | VerificationError] = {}
    active: dict[object, tuple[multiprocessing.Process, Dependency]] = {}
    next_index = 0
    deadline = time.monotonic() + deadline_seconds
    try:
        while next_index < len(dependencies) or active:
            while next_index < len(dependencies) and len(active) < max_workers:
                if time.monotonic() >= deadline:
                    break
                dependency = dependencies[next_index]
                next_index += 1
                receiver, sender = context.Pipe(duplex=False)
                process = context.Process(
                    target=_lookup_worker,
                    args=(sender, dependency, lookup),
                    daemon=True,
                )
                try:
                    process.start()
                except (OSError, RuntimeError):
                    receiver.close()
                    sender.close()
                    results[dependency] = VerificationError(
                        "registry lookup worker could not start"
                    )
                    continue
                sender.close()
                active[receiver] = (process, dependency)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not active:
                continue
            ready = wait_for_connections(active, timeout=min(0.25, remaining))
            for receiver in ready:
                process, dependency = active.pop(receiver)
                try:
                    status, value = receiver.recv()
                except (EOFError, OSError, TypeError, ValueError):
                    status, value = "error", "registry lookup worker failed"
                finally:
                    receiver.close()
                process.join(timeout=0.25)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=0.25)
                if status == "ok" and isinstance(value, datetime):
                    results[dependency] = value
                elif status == "error" and isinstance(value, str):
                    results[dependency] = VerificationError(value)
                else:
                    results[dependency] = VerificationError(
                        "registry lookup worker returned an invalid result"
                    )
    finally:
        _terminate_workers(active)

    for dependency in dependencies:
        results.setdefault(
            dependency, VerificationError("registry lookup deadline was exhausted")
        )
    return results


def verify_dependencies(
    dependencies: Iterable[Dependency],
    *,
    now: datetime,
    minimum_age: timedelta,
    lookup: Callable[[Dependency], datetime] = registry_release_time,
    lookup_deadline_seconds: float = LOOKUP_DEADLINE_SECONDS,
    max_workers: int = LOOKUP_WORKERS,
) -> list[str]:
    ordered = sorted(set(dependencies))
    if len(ordered) > MAX_INTRODUCED_DEPENDENCIES:
        return [
            "introduced dependency count exceeds the bounded verification limit "
            f"({len(ordered)} > {MAX_INTRODUCED_DEPENDENCIES})"
        ]
    if not ordered:
        return []
    if lookup_deadline_seconds <= 0 or max_workers <= 0:
        raise ValueError("lookup deadline and worker count must be positive")

    cutoff = now - minimum_age
    results = _isolated_lookups(
        ordered,
        lookup=lookup,
        deadline_seconds=lookup_deadline_seconds,
        max_workers=min(max_workers, len(ordered)),
    )

    failures: list[str] = []
    for dependency in ordered:
        result = results[dependency]
        if isinstance(result, VerificationError):
            failures.append(f"{dependency.display}: {result}")
            continue
        released = result
        if released > cutoff:
            failures.append(
                f"{dependency.display}: released {released.isoformat()} after cutoff {cutoff.isoformat()}"
            )
    return failures


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="trusted base commit")
    parser.add_argument("--head", required=True, help="candidate commit")
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--minimum-age-hours", type=int, default=72)
    parser.add_argument("--now", help="fixed ISO-8601 time for deterministic verification")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    if arguments.minimum_age_hours <= 0:
        print("lockfile release-age guard requires a positive minimum age", file=sys.stderr)
        return 2
    now = _parse_timestamp(arguments.now) if arguments.now else datetime.now(timezone.utc)
    try:
        dependencies = dependencies_from_git(
            arguments.repository_root.resolve(), arguments.base, arguments.head
        )
        failures = verify_dependencies(
            dependencies,
            now=now,
            minimum_age=timedelta(hours=arguments.minimum_age_hours),
        )
    except (OSError, subprocess.SubprocessError, VerificationError, ValueError) as error:
        print(f"lockfile release-age verification failed closed: {error}", file=sys.stderr)
        return 1

    if failures:
        print("lockfile release-age verification rejected introduced dependencies:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(
        f"lockfile release-age verification passed for {len(dependencies)} introduced registry dependencies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
