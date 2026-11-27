# OpenTour implementation rules

Linear's current full issue specification and mandatory linked contracts govern implementation. Begin with the ticket's Build entry prompt, inspect the current parent, prerequisites, accepted commits and exact scope, then claim one isolated worktree/branch. Historical attachment copies do not override current native contracts.

Use `codex/<issue>-<scope>` branches unless an explicitly approved executor workflow requires otherwise. Shared schemas, generated clients, lockfiles, migrations and global routing each have one serialized writer. OPE-122 owns the initial `just setup` scaffold; after its accepted handoff SHA, OPE-123 exclusively owns `justfile` and command wrappers. Do not claim parallel ownership of those files.

All frontend/UI implementation is assigned to Claude under the owner's front-end handoff of 16 November 2026, with independent Codex review. Claude front-end branches use `claude/<issue>-<scope>`. Backend/bootstrap follows its issue assignment. A distinct qualified reviewer may replace unavailable preferred review tooling where the governing planning contract permits that fallback; record the actual reviewer and exact reviewed commit. No self-acceptance.

Before implementation, map every acceptance ID and additional named fixture to positive/adverse tests and evidence. Run actual commands, record exit status and fixture kind. Missing dependencies, credentials, providers, GPU capacity, zero tests and required skipped cases are not PASS. Local/simulated results cannot satisfy provider or customer-evidence gates. Parent completion requires integration, not just closed children.

Use current Figma nodes and approved snapshot for UI states, but provider contracts govern actual APIs, authority and response semantics. A pending request is not necessarily an admitted job. Do not infer success from an unknown result. If a material behavior or architecture contract is contradictory or absent, record the precise gap and stop only dependent work; do not invent the product decision.

Never copy the private knowledge warehouse, contacts, customer records, credentials or full conversations into this public repository. Use generated sentinel fixtures. Inspect the tracked diff and generated artifacts before any public push. First public commit requires independent repository-content review. Do not deploy infrastructure or incur provider spend from setup.

`tools/toolchain.json` pins supported runtimes, tools and bootstrap images. `just setup` validates prerequisites and installs frozen CPU/TypeScript dependencies without provisioning cloud resources. Training has a separate project and lock; GPU qualification and isolated trainer/evaluator execution are owned by later ML/platform issues. Do not install CUDA into the control workspace or treat the bootstrap smoke images as production-qualified images.

Close a ticket only after its complete acceptance, relevant integration, exact-commit independent review and evidence pass. Record branch/worktree, commit, changed paths, command/results, unresolved external gates and next action in Linear. Keep historical evidence bound to its source and commit.
