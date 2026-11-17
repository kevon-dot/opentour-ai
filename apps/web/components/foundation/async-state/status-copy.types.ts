import type { NoticeProps } from '../tokens/contracts';
import type { ToastState } from '../tokens/feedback.contracts';

/** Native UI-TX Notice instances and the corrected v4 action snapshot. */
export const statusFixtures = {
  'UI-TX-SAVE/dirty_valid': { // I24:176;12:3
    tone: 'info', title: "Save changes",
    body: "The reviewed draft has valid inputs. Save creates one intent against the displayed revision.",
  },
  'UI-TX-SAVE/submitting': { // I24:184;12:3
    tone: 'info', title: "Saving changes…",
    body: "Keep the draft and original intent while awaiting the authoritative response. Repeated clicks do not create another save.",
  },
  'UI-TX-SAVE/saved': { // I24:192;12:6
    tone: 'success', title: "Changes saved",
    body: "The server confirms this revision. Clear the dirty marker and preserve the returned revision; saving does not start training.",
  },
  'UI-TX-SAVE/failed': { // I24:200;12:12
    tone: 'failure', title: "Could not save changes",
    body: "The save failed. Keep safe draft values and named field errors. No automatic overwrite or repeated submission.",
  },
  'UI-TX-SAVE/conflict': { // I24:209;12:9
    tone: 'hold', title: "This configuration changed. Review the latest version.",
    body: "This item changed. Load the current revision, show differences and require review before a new save intent.",
  },
  'UI-TX-CANCEL/running': { // I24:221;12:3
    tone: 'info', title: "Cancel run",
    body: "The job is running under its current reservation. Cancellation may still incur bounded cleanup and already-accrued cost.",
  },
  'UI-TX-CANCEL/cancel_requested': { // I24:229;12:9
    tone: 'hold', title: "Cancellation requested",
    body: "Cancellation was accepted. Work may still be running; show the original job and do not claim resources are released.",
  },
  'UI-TX-CANCEL/stopping': { // I24:237;12:9
    tone: 'hold', title: "Stopping",
    body: "The provider is stopping work. Track draining tasks, exact allocated resources and continuing cost until final receipt.",
  },
  'UI-TX-CANCEL/cancelled': { // I24:245;12:3
    tone: 'info', title: "Cancelled",
    body: "The authoritative job receipt confirms cancellation. Show final incurred cost and any retained artifacts or cleanup exceptions.",
  },
  'UI-TX-CANCEL/outcome_unknown': { // I24:254;12:15
    tone: 'unknown', title: "Could not confirm cancellation",
    body: "Stop acknowledgment is missing. Reconcile the same job and provider resources. Do not launch replacement work or claim cancellation.",
  },
  'UI-TX-REVOKE/control_blocked': { // I24:266;12:9
    tone: 'hold', title: "Blocking new work",
    body: "Control has stopped issuing new authority. Existing cell leases and in-flight effects may remain until their barriers settle.",
  },
  'UI-TX-REVOKE/pending_cells': { // I24:274;12:9
    tone: 'hold', title: "Waiting for cells",
    body: "One or more cells have not acknowledged the new revocation epoch. Keep enforcement pending and name the unresolved cells.",
  },
  'UI-TX-REVOKE/draining': { // I24:282;12:9
    tone: 'hold', title: "Draining actions",
    body: "Affected cells acknowledge revocation, but admitted effects are still draining. Enforcement is not yet confirmed.",
  },
  'UI-TX-REVOKE/confirmed': { // I24:290;12:6
    tone: 'success', title: "Enforcement confirmed",
    body: "Every required cell barrier is acknowledged and no effect remains unresolved. Protected caches and subscriptions have been cleared.",
  },
  'UI-TX-REVOKE/reconciliation_required': { // I24:299;12:15
    tone: 'unknown', title: "Reconciliation required",
    body: "A cell or effect cannot be verified. Keep new work blocked; inspect the recorded frontier and reconcile before declaring enforcement.",
  },
  'UI-TX-PROMOTE/hold': { // I24:311;12:3
    tone: 'info', title: "Promote candidate",
    body: "Required release gates are not met. Promotion is disabled; incumbent r0 remains active. A support role cannot bypass this result.",
  },
  'UI-TX-PROMOTE/authorized': { // I24:319;12:3
    tone: 'info', title: "Promote candidate",
    body: "Current policy permits the exact candidate and scope. Authorization is not deployment; bind the candidate, baseline and finite lease.",
  },
  'UI-TX-PROMOTE/staging': { // I24:327;12:3
    tone: 'info', title: "Staging candidate",
    body: "The exact candidate is loading into a private replica. Verify digests and probes. Incumbent r0 still serves customer traffic.",
  },
  'UI-TX-PROMOTE/promotion_requested': { // I24:335;12:3
    tone: 'info', title: "Promotion requested",
    body: "Promotion intent was accepted. The desired pointer is changing under a fence; actual traffic remains unconfirmed.",
  },
  'UI-TX-PROMOTE/verifying_traffic': { // I24:344;12:3
    tone: 'info', title: "Verifying traffic",
    body: "Compare desired and observed route identity and probe evidence. Do not show Active until the current runtime receipt agrees.",
  },
  'UI-TX-PROMOTE/active': { // I24:352;12:6
    tone: 'success', title: "Candidate active",
    body: "The observed runtime receipt confirms candidate r1 is serving in the authorized scope. Keep tested incumbent r0 available for fallback.",
  },
  'UI-TX-PROMOTE/runtime_unknown': { // I24:360;12:15
    tone: 'unknown', title: "Runtime unknown",
    body: "The serving identity is unconfirmed. Show the last observation and initiate reconciliation. Follow the authorized fallback or safe-stop policy.",
  },
  'UI-TX-DELETE/preview': { // I24:372;12:3
    tone: 'info', title: "Confirm deletion",
    body: "Review the exact affected assets, derived dependencies, fallback protection and retained exceptions. Confirm this impact digest only.",
  },
  'UI-TX-DELETE/requested': { // I24:380;12:9
    tone: 'hold', title: "Deletion requested",
    body: "Deletion execution was requested. New use is blocked by the tombstone policy; physical deletion is not yet confirmed.",
  },
  'UI-TX-DELETE/partial': { // I24:388;12:9
    tone: 'hold', title: "Partially deleted",
    body: "Some locations have confirmed deletion while others remain pending or retained. Show per-location counts and exact reasons.",
  },
  'UI-TX-DELETE/confirmed': { // I24:396;12:6
    tone: 'success', title: "Deletion confirmed",
    body: "All required location receipts confirm the deletion scope. Separately list legally retained exceptions and copies already delivered outside control.",
  },
  'UI-TX-DELETE/unverifiable': { // I24:405;12:15
    tone: 'unknown', title: "Verification pending",
    body: "At least one location cannot confirm the result. Keep the request open with verification pending; absence of a response is not proof of deletion.",
  },
  'UI-ARTIFACTS-A01/ready': { // 21:3459
    tone: 'info', title: "Begin upload",
    body: "Available only with current scope, required permission and validated prerequisites.",
  },
  'UI-ARTIFACTS-A01/pending': { // 21:3467
    tone: 'info', title: "Saving changes…",
    body: "Keep this request and any operation ID actually returned. Prevent duplicate submission; the outcome is unconfirmed. A pending request alone does not mean a background job exists.",
  },
  'UI-ARTIFACTS-A01/confirmed': { // 21:3475
    tone: 'success', title: "Changes saved",
    body: "The confirmed revision replaces the displayed revision.",
  },
  'UI-ARTIFACTS-A01/refused': { // 21:3484
    tone: 'hold', title: "Action unavailable",
    body: "Permission, scope or prerequisite check failed. Show the exact safe reason. No new effect was admitted.",
  },
  'UI-ARTIFACTS-A01/failure': { // 21:3492
    tone: 'failure', title: "Could not begin upload",
    body: "Preserve safe input. For a changed revision: “This item changed. Review the latest version.” Do not overwrite or resubmit automatically.",
  },
  'UI-ARTIFACTS-A01/unknown': { // 21:3500
    tone: 'unknown', title: "Outcome not confirmed",
    body: "Keep the last authorized observation explicitly dated. A missing acknowledgment is not proof of failure or success.",
  },
  'UI-ARTIFACTS-A01/recovery': { // 21:3509
    tone: 'info', title: "Review another change",
    body: "Create a new reviewed revision. Historical records remain immutable.",
  },
} as const satisfies Record<string, NoticeProps>;

export type StatusFixture = keyof typeof statusFixtures;

/** These fixtures alone have a supplementary Toast presentation. */
export const toastVariants = {
  'UI-TX-CANCEL/cancel_requested': 'accepted',
  'UI-TX-SAVE/saved': 'confirmed',
  'UI-TX-SAVE/failed': 'failed',
  'UI-TX-CANCEL/outcome_unknown': 'unknown',
} as const satisfies Partial<Record<StatusFixture, ToastState>>;

export type StatusCopyProps =
  | { presentation: 'notice'; fixture: StatusFixture; urgent?: never }
  | { presentation: 'toast'; fixture: keyof typeof toastVariants; urgent?: false }
  | { presentation: 'toast'; fixture: 'UI-TX-SAVE/failed'; urgent: true };
