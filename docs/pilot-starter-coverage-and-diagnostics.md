# Starter coverage receipt and post-miss diagnostics: partial OPE-513

This slice adds two backend cores beside the reviewed starter case store. It does
not change `starter_cases.py`, the map-confirmation adapter, scoring, Slack views
or the kit. It adds no owner action, UI, inference call, fit or release.

The arithmetic and lineage rules come from the OPE-513 decisions of the overnight
sprint of 2026-11-28 (owner delegation, reversible by the owner). They are recorded
under "Decisions (overnight sprint 2026-11-28)" in the shared implementation
contract.

Before-code acceptance mapping (current native OPE-513 and the shared contract):

| Check | Native requirement | Positive and adverse evidence |
| --- | --- | --- |
| B1 | Per-branch numerator/denominator | Hand-written expected counts for the core seven-family pack, the P1 seven-family pack, the five-family purchase pack and the 100-paraphrase pack |
| B2 | Removing both missing-PO families | REVIEW is 0 of 2 reserved, uncovered, held and rate-unavailable (not 0%); no all-branches verdict; zero training credit |
| B3 | 100 paraphrases count one family | APPROVE is 1 of 1 families with 100 reserved cases; 99 duplicates collapsed |
| B4 | Dispositions for omitted and duplicate cases | Every case is reserved_evaluation, development, excluded_unresolved or excluded_by_owner; accept/edit/unresolved kept; origin per case and family |
| B5 | Split and audit receipt | Digest-sealed projection; replay detects edited receipts and packs that disagree with their freeze; reads write nothing |
| B6 | Reserved references handed to OPE-539 | The handoff holds the four P1 references and totals only: no branch, target, suggestion, input or per-action count |
| D1 | Exposed miss goes to a diagnostic set | Evaluator-only placement from the persisted, digest-checked result; misses and missing outputs recorded; no-miss, tampered, foreign and revoked cases hold |
| D2 | Revised choice on unexposed, independent families | Pre-registration on a successor freeze before any run; exposed inputs under a new family, a used execution, an earlier run, a plan that does not admit the choice, a different workflow and owner-only reserved families all hold |
| D3 | Old manifests and evidence labels kept | The predecessor pack, its coverage receipt, its freeze replay and its scored result stay byte-identical; every label stays expert_reviewed_generated_starter |

## Coverage receipt

`owner_coverage_receipt(cases, owner, work)` and `session_coverage_receipt(session)`
read a frozen pack under the named owner's current review authority. The receipt
is a deterministic projection of the immutable freeze. It is recomputed on demand,
not stored. It re-derives every count, cell, reserved reference and held branch,
and it holds if any differs from the freeze.

For each confirmed branch:

- **Numerator:** independent reserved families whose owner target is one of the
  branch's material coverage actions.
- **Denominator:** independent families authored for the branch, of every origin,
  split and status.

A family counts once, however many cases it holds. `reserved_cases` keeps the
case-level scoring denominator visible. Each (branch, action) cell repeats the
freeze's reserved-family count. A branch with an empty cell is uncovered or
partially covered and names `collect_independent_reserved_family`. A critical
branch with an empty cell is held. Only fully covered branches are in the verdict
scope, so an all-branches verdict needs every material cell covered. Per-branch
rates are unavailable when a branch has no reserved case. Training credit and
live recurrence are always zero.

The full receipt is named-owner only. `handoff(receipt)` is the evaluator-safe
projection for OPE-539 and OPE-526. Branch predicates plus per-branch counts
would reveal owner labels to anyone holding the evaluation inputs, so the handoff
carries only references, totals and the receipt digest. `verify_receipt` replays
the projection for an independent check.

## Post-miss diagnostics

`StarterDiagnostics(directory, cases)` uses the evaluator's `StarterCases`.
In the installed evaluator this is `StarterEvaluationAuthority.cases`; wiring it
there is follow-up integration.

- `place_exposure(evaluator, work, execution_ref=...)` reads the persisted scored
  result. It checks the result digest, reference, metrics, population and answer
  key against the current freeze. A miss is an exact-action mismatch under the
  shared rule (REVIEW status counts as REVIEW). A missing output is recorded as
  missing. Both arms are checked. Every reserved family in the result is exposed;
  the misses form the diagnostic set. The sealed record is published only after
  a final authority check. A result with no miss is not a post-miss exposure.
- `place_reassessment(evaluator, predecessor, successor, exposure_ref=...,
  execution_ref=..., revised=RevisedChoice(arm, reference, revision))`
  pre-registers the only claim-bearing run of a revised choice. The successor
  must be a different freeze in the same tenant, workflow and epoch. Its plan
  must admit the choice. Its reserved families must share no family ID or
  decision-input fingerprint with an exposed family. The check that no
  evaluation has started on the successor runs under the scoring journal's own
  directory lock. Owner counterexamples in the successor's reserved families
  are exposure-informed. They are excluded from the protected population, and
  the registration holds when no generated family remains.
- `assessment(evaluator, work, execution_ref=...)` labels a scored result:
  - `protected_reassessment`: only the registered execution with the exact
    registered choice. Its claim uses the shared metrics on the protected
    families only.
  - `original_exposure`: the original result, unchanged.
  - `exposed_diagnostic`: a later run on an exposed freeze, reporting each
    diagnostic case as fixed or still missed, with no metrics claim.
  - `unregistered_choice` or `unplaced`: no claim.

Every exposure and registration is an immutable, sealed private record. Reads
re-derive and compare it. A predecessor that is no longer current still binds
its successor through the sealed exposure. The same evaluator must be able to
read both freezes; a split evaluator assignment holds.

## Open gates

- Installed wiring of the diagnostics into the evaluator session, and an owner
  readout of the receipt and lineage (UI: GPT Astra assignment).
- The owner case-entry action for counterexamples (design and UI gates).
- An owner attestation that would let post-exposure counterexamples count as
  protected evidence.
- OPE-539 scored readout and arithmetic replay, OPE-526 audit consumption of the
  handoff, and the OPE-515 end-to-end witness.

Generated fixtures establish software behavior only. They supply no live
recurrence, customer accuracy, fit or release evidence.
