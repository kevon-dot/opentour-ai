# Read the owner recovery item for a held proposal

Use the existing owner-local `ReviewerAssignments.owner_recovery` operation
after resolving the active organization, engagement, and saved owner
settings from the worker's private configuration. Supply the frozen run ID,
run revision, and candidate artifact hash. Authenticate the caller before
passing its actor binding. The current workflow owner and configured
custodian/operator can read the item. The previous or proposed reviewer
still needs their separate authority to inspect the evidence.

The worker reads the current signed settings head and the exact proposal's
saved reconciliation identity. It returns one opaque recovery reference
for that proposal and generation. The reference remains stable after
the local projector acknowledges the record, including when there were
no provider requests, multiple former requests, or no confirmed owner
notification.

The result identifies the proposal, its saved settings generation, the
held reason and the next owner action. It omits identities, old provider
requests, source information and private evidence. It does not authorize
inspection, approval, notification or delivery. Check the existing
[reviewer replacement reconciliation](pilot-reviewer-replacement-integration.md)
before proceeding with a new reviewer.

A verified historical Zapier decision for the exact proposal suppresses the
recovery item and preserves the original receipt. The worker matches the
original sealed presentation, the unique verified request in the saved
generation, and the corresponding authenticated terminal reservation.
An unavailable, ambiguous or contradictory record leaves the item visible.
The read concerns the approval decision. Destination delivery requires its
own receipt. Successor decisions and the separate native Slack decision
path use their own authority and verification rules.
