# OPE-437 evidence mapping

Native OPE-437 was read in full, with its parent and linked implementation
contract, before this isolated change. Worker contract base is PR #114
`7a1a5281e1a6c9b6542c8ca27f4002b9e2aee530`. This package supplies part of the issue;
**OPE-437 cannot close until actual publication/installation evidence exists.**

| Native clause / adverse case | Software evidence | Remaining installed gate |
| --- | --- | --- |
| Only discover_sources, submit_intake, get_run | `index.js`; exact-three-actions test; all three exercised against shipped worker TLS | Each actual installed private action in Zap History |
| Fixed owner-bound origin/credential capability | `adapter.js` connection-only origin/audience/token; invalid action override/URL tests; actual wrong-audience/swapped-token denial | Signed owner binding, actual scoped connection/install identity and expiry |
| App/connector version pinned; validate | Package 0.1.0, core/CLI 19.3.0, local lock; actual CLI `validate --without-style` | Provider publishing validation and hosted Node 22 compatibility |
| Historical, starter and live field help | Versioned fields/help in `index.js`; positive shape tests; live cannot supply correction_revision | Published editor fields and enabled feed capability on authorized worker |
| Opaque grant-filtered catalog paging | Page-size/cursor validation; strict ordinary/feed response shapes | Actual scoped discovery history, including enabled polling if configured separately |
| Unknown/foreign references and swapped connection | Actual two-tenant TLS test: both own positives, bilateral foreign denials, unknown ref, swapped token; no disclosed record | Independent second-account connection with authorized bilateral probes |
| Revoked token, rotate, unreachable origin | Actual worker TLS old-key denial/new-key success, wrong audience, trust failure and closed endpoint; no redirected/retried request | Actual provider connection rotation/revocation/reconnect and Zap History |
| No auth values/raw responses in outputs/errors/logs | Strict schemas, bounded generic errors, password field, native HTTPS + documented skipHttpPatch; SDK/test assertions | Inspector's installed log/task/export samples, not just local source inspection |
| Versioned export and installation guide | This directory, local build ZIP, README pin/repin procedure | Actual app ID, published version/invitation, independently installed connection and rollback receipt |
| Reuse same worker/stdio admission behavior | Actual shipped gateway + immutable zipapp; repeat intent/new trigger ID returns same receipt; restart returns original state, one run/one workflow/no materialization per tenant | Combined ten-profile and stdio/HTTPS installed matrix remains OPE-435 |

Package-local commands and exit results belong to the exact reviewed commit.
The final task handoff records those commands and artifact digests. No existing
repository CI job is relabeled as executing this new package suite.

Publication, invitation, live Zap activation, provider installation, live source
access, provider logs, customer callbacks/destination receipt and paid resources
were **not executed**. Generated tests earn zero customer acceptances.
