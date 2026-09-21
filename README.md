# Shared repository policy

This repository owns organization-neutral CI policy used by the independently published BoxFerry,
ComposeLens, PodmanLens, and QuadletLens repositories.

## Lockfile release-age guard

[`scripts/lockfile_release_age.py`](scripts/lockfile_release_age.py) compares a trusted base commit
with a candidate commit. For changed `Cargo.lock` and `package-lock.json` files, it verifies every
newly introduced public-registry version is at least 72 hours old. Unknown registries, Git sources,
missing timestamps, malformed metadata, and network failures fail closed. Unchanged dependencies
already present in the base remain outside the check. Registry lookups use at most eight workers,
two five-second attempts per request, a 90-second overall lookup deadline, and a 1,000-dependency
input limit so failure remains bounded.

Consumers check out this repository at a full immutable commit SHA, then run:

```console
python3 shared-policy/scripts/lockfile_release_age.py \
  --base "$BASE_SHA" \
  --head "$HEAD_SHA" \
  --minimum-age-hours 72
```

The consumer owns its checkout, aggregate PR gate, required-check configuration, and Renovate pin.
The shared guard never receives registry credentials and does not print registry URLs or response
bodies.
