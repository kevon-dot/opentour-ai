# Track application capture stream completeness

The application-side capture register records evidence metadata under a
current optional grant. The grant binds the authenticated actor, tenant,
engagement, workflow, policy and three independently scoped source roles.
Capture settings start off. The metadata-only setting records event and
attempt references, terminal status, chunk indices and a private keyed
fingerprint for duplicate detection. Keep the fingerprint key in the
customer's private custody.

An application's retry is another observed attempt. An authoritative app
commit carries its own source and commit reference. This preserves the
distinction between an invocation, its terminal marker, and the committed
decision. A missing or conflicting chunk leaves raw replay unavailable. A
terminal marker for indices 0 through 2 with only 0 and 2 present leaves a
gap at 1. An identical delivery fills that gap when it arrives. Competing app
commits for the same decision revision are counted as disputed.

Read results are role-scoped and contain attempt state and gap metadata.
Readers without application-commit permission see unknown decision counts.
The customer-local verified outcome reducer continues to own verified truth
and the current live evidence counts.

The [generated stream tests](../tests/pilot/test_capture_streams.py) exercise
off-to-metadata opt-in, restart with a gap, duplicate and conflicting
chunks, a two-attempt operation with one app commit, competing app commits,
scope denial, and a whitespace-free synthetic content sentinel. Gateway and
historical adapters and authorized exact-content inspection use the native
capture contract on their own entry points.
