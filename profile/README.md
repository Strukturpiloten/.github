# Strukturpiloten 🚀

**Open-source container tooling, reproducible infrastructure, and structured technical delivery.**

Strukturpiloten is a consulting and technology company from Bückeburg, Germany. We combine
**DevOps and IT project management** with practical Linux and container engineering to help
organizations plan, modernize, and operate infrastructure they can understand and control.

We are founded and run by
[Martin “Becks” Beckert](https://github.com/TheRealBecks) and
[Frauke Beckert](https://github.com/ladyfrauke).

## What we do 🧭

### DevOps and IT project management 🤝

We connect technical implementation with structured delivery. That includes clarifying
requirements, coordinating work, making risks and dependencies visible, documenting decisions,
and carrying infrastructure changes through to an operable result.

This combination helps technical and organizational work move in the same direction instead of
becoming separate projects.

### Commercial container support 📦

We provide commercial support and consulting for teams that need more than a generic container
setup:

- container architecture, modernization, and technical reviews
- Podman, Docker Compose, and Quadlet environments
- OCI image design, automated builds, registries, and release workflows
- migrations, compatibility analysis, and troubleshooting
- CI/CD, dependency management, documentation, and operational handover

Our goal is not to hide complexity behind another black box. We make decisions, dependencies, and
trade-offs visible so that teams can operate and evolve their systems themselves.

### Open-source engineering 🛠️

We develop our tools and infrastructure in public whenever possible. Our projects focus on
transparent compatibility, deterministic output, reproducible builds, careful diagnostics, and
documentation that can be tested alongside the software.

## Featured projects ✨

### [BoxFerry](https://github.com/Strukturpiloten/boxferry) 🚢

BoxFerry converts Compose, Quadlet, and Podman application definitions through a neutral model. It
keeps incompatible intent visible as structured diagnostics and only produces output when the
chosen loss policy permits it. Generated plans and units remain review material; BoxFerry never
deploys or executes them.

📖 **Website and documentation:** [boxferry.dev](https://boxferry.dev/)

BoxFerry is supported by format-specific Rust libraries:

- [ComposeLens](https://github.com/Strukturpiloten/compose-lens) for Compose documents
- [QuadletLens](https://github.com/Strukturpiloten/quadlet-lens) for version-aware Podman Quadlet
- [PodmanLens](https://github.com/Strukturpiloten/podman-lens) for typed Podman inspection and
  deployment planning

### [Strukturpiloten Containers](https://github.com/Strukturpiloten/containers) 📦

Our public container-image monorepo builds and publishes maintained OCI images with automated
validation, multi-architecture builds, dependency-aware releases, signatures, SBOMs, provenance,
and attestations.

The catalog includes exact upstream Podman compatibility images for Podman 5.4 through 6.1 in
rootful and rootless variants, alongside images used by our Nextcloud and TYPO3 work.

### More public work

- [container-setup](https://github.com/Strukturpiloten/container-setup) — a Go tool for generating
  Compose files, environment files, and project configuration from a declared setup model
- [Nextcloud](https://github.com/Strukturpiloten/nextcloud) — a documented container solution built
  with free and open-source components

## How we work 🌱

- **Open by default:** code, decisions, limitations, and documentation should be inspectable.
- **Reproducible:** builds and deployments should not depend on undocumented manual steps.
- **Explicit:** incompatibilities and trade-offs should be reported, not silently normalized away.
- **Operable:** delivery includes documentation, handover, and a realistic operating model.
- **Human-centered:** technology and project work have to support the people responsible for them.

We currently work primarily with Linux, Podman, Compose, Quadlet, OCI tooling, and CI/CD. That will
be combined with agile IT project management. We are also expanding our practical work with
Kubernetes and immutable platforms such as Talos Linux and Flatcar Container Linux.

## Work with us 💬

Need help planning, delivering, migrating, or stabilizing a container environment? We combine
hands-on container engineering with **DevOps and IT project management** — from architecture and
technical implementation to coordinated delivery and operational handover.

🌐 **Website:** [strukturpiloten.de](https://www.strukturpiloten.de/)

📍 **Bückeburg, Germany**

**Powered by curiosity. Guided by structure.**
