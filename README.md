# OpenTour AI

OpenTour autonomously fine-tunes supported open-weight models and verifies improvements against the customer's authoritative business records. The implementation is being built from the current Linear contracts. This bootstrap is development infrastructure, not a completed or production-qualified application.

## Development setup

Supported developer profiles are macOS arm64 and Linux glibc arm64/x86_64. macOS x86_64 is planned but unqualified and setup rejects it; its download pins are retained separately for future qualification. Install the exact Python 3.12.16, Node 24.22.3, uv 0.13.8, pnpm 12.18.0 and just 1.59.0 pins in `tools/toolchain.json`. Use the published artifact URLs/checksums there, not a floating installer. The initial Python invocation needs Python 3.12 or newer; uv locates the exact project interpreter without silently selecting a different patch.

```sh
uv python install 3.12.16
just setup
uv run python -m pytest tests/contract/test_bootstrap.py -q
```

Setup validates tools and platforms, checks manifest/lock agreement, performs frozen dependency sync, imports the CPU service packages and checks the TypeScript package boundary. Runtime and isolated Python build dependencies have exact version and archive hash pins. It makes no cloud calls and does not rewrite README/license or package locks. Network access is only for the configured public package registries when dependencies are uncached. `--offline` requires populated caches and fails if any dependency is absent. No AWS, identity, billing or customer credentials are needed. Receipts mark an uncommitted checkout; only checks against the clean reviewed candidate establish commit-bound qualification.

```sh
python3 tools/bootstrap.py --offline
python3 tools/container_smoke.py --platform linux/arm64
```

Container smoke uses pinned CPU images and separate deployable targets. It verifies packaging/import behavior, not GPU compatibility, authority isolation, customer proof or a running product. Docker must be available. Linux amd64 can also be exercised using `--platform linux/amd64`; emulation evidence is labeled separately from native execution.

## Boundaries

- `services/control-api`, `services/cell-runtime`, `services/audit-journal`: separate CPU uv workspace packages.
- `training`: separate locked Python project; trainer and protected evaluator have distinct execution profiles. CUDA/framework/model qualification is separate and remains NOT_RUN until its owning tickets execute it.
- `apps/web`: Next.js dependency boundary; frontend functionality and Figma state implementation belong to GPT Astra tickets.
- `packages/client-ts`: generated-client boundary; no invented API endpoints.
- `packages/contracts`: shared versioned contracts have a serialized owner.
- `infrastructure`: approved deployment/IAM implementations and tests are separate from setup.

The current native Linear issue and required linked documents are the implementation authority. Begin with [OPE-122](https://linear.app/opentourai/issue/OPE-122), then follow blocked-by dependencies. The initial `justfile` is handed to OPE-123 after review; later commands are not implemented by this scaffold.

Generated local evidence goes in ignored `evidence/`; sanitize and review it before deliberate publication. Product runtime and G1–G6 empirical acceptance remain NOT_RUN until their actual commands and required resources produce reviewed results.
