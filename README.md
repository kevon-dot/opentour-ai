# OpenTour AI

For the first ten customers, a workflow owner connects one permitted MCP source with two scoped views, one for workflow events and one for independently verified corrections, and sets the decision, verified correction fields, allowed model, compute limits, and named Slack reviewer. A supervised worker on the customer's machine watches that source for new verified corrections. It tests an executable rule first. Recurring errors that meet the agreed criteria can trigger a bounded local fit of the supported open-weight model. The reviewer inspects the protected comparison in Slack and decides whether that exact version may be handed off.

## Run the first-ten pilot

Use Python 3.12.16 on a supported POSIX machine. From a reviewed source checkout, build the portable kit and unpack it into a new directory. The operator who receives the ZIP can start at the unpack step.

```sh
python -I -S -B tools/build_slack_pilot_kit.py OpenTour_Slack_Workspace_Kit.zip
python -m zipfile -e OpenTour_Slack_Workspace_Kit.zip opentour-pilot
cd opentour-pilot
python -I -S -B opentour-slack.pyz evaluate --report-dir first-readout generated/readout-input.json
```

Open `first-readout/brief.md` for the comparison. The same kit includes local MCP enrollment, frozen-model replay, a native Slack app, an optional consent-bound Zapier receipt and custody closeout. A workspace administrator handles the private installation. OpenTour provisions zero AWS resources for this pilot.

- [Connect a workplace source and run the autonomous cycle](services/cell-runtime/docs/pilot-autonomous-cycle.md)
- [Set training limits and renew or withdraw the standing mandate](services/cell-runtime/docs/pilot-learning-mandate.md)
- [Configure the named reviewer in the Zapier Slack approval action](services/cell-runtime/docs/pilot-zapier-approval.md)
- [Run the managed simple-repair and open-weight workflows](services/cell-runtime/docs/pilot-managed-repairs.md)
- [Operator quickstart and reproducible training example](services/cell-runtime/docs/slack-workspace-kit.md)
- [Client MCP enrollment and consent](services/cell-runtime/docs/pilot-onboarding-mcp.md)
- [Agree on minimum coverage and maximum error before a comparison](services/cell-runtime/docs/pilot-operating-point.md)
- [Install the workspace app and check the approval flow](services/cell-runtime/integrations/slack/README.md)
- [Find and reconcile an uncertain Zapier delivery](services/cell-runtime/docs/pilot-receipt-recovery.md)

## Compare two workflows offline

Use the cell runtime to compare decisions already produced by a current workflow and a proposed repair on the same cases. Try the generated eight-case fixture from the repository root.

```sh
export PYTHONPATH=services/cell-runtime/src
python -m opentour_cell.pilot.readout services/cell-runtime/examples/pilot-evaluation.json
python -m opentour_cell.pilot.review services/cell-runtime/examples/pilot-evaluation.json
```

The fixture includes one fix, one regression, an unknown reference and a missing prediction. Both alternatives automate five cases, but they automate different cases. Use the paired counts and review workload together when interpreting the result.

For an actual evaluation, agree on the decision and acceptable error level with the workflow owner. Use independently checked references and a held-out population in the approved environment. The operator can run the comparison through the CLI or the read-only local MCP tool. Selected exceptions can go through the team's existing review process.

- [Manifest format and readout](services/cell-runtime/docs/pilot-readout.md)
- [Local MCP client setup](services/cell-runtime/docs/pilot-mcp.md)
- [Local exception worklist](services/cell-runtime/REVIEW.md)

## Development setup

Supported developer profiles are macOS 13.5+ arm64 and Linux arm64/x86_64 with kernel 4.18+, glibc 2.28+ and libstdc++ providing GLIBCXX_3.4.25. These match the [pinned Node runtime requirements](https://github.com/nodejs/node/blob/v24.22.3/BUILDING.md#platform-list); executing Node during preflight also checks that its shared libraries load. macOS x86_64 is planned but unqualified and setup rejects it; its download pins are retained separately for future qualification. Install the exact Python 3.12.16, Node 24.22.3, uv 0.13.8, pnpm 12.18.0 and just 1.59.0 pins in `tools/toolchain.json`. Use the published artifact URLs/checksums there, not a floating installer. The initial Python invocation needs Python 3.12 or newer; uv locates the exact project interpreter without silently selecting a different patch.

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
