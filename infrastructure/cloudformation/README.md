# Environment boundary foundation (OPE-213)

These JSON-subset CloudFormation templates describe empty isolated infrastructure.
They do not deploy from setup, issue application grants, establish provider
credentials, or qualify a production release. AWS change-set/deployment evidence is
still required. OPE-217 owns the independent audit journal; OPE-145 owns workload
identity, OPE-214 owns qualified provider egress/telemetry, and OPE-215 owns migration
plans and rollout/recovery orchestration. This increment does not duplicate them.

## Inputs and sequence

An authorized operator must provide distinct production, nonproduction and audit
accounts; the approved commercial AWS region; a region/account-bound TLS
certificate; VPC address space; database class/version/family/storage/capacity;
connection reserves; the region's independently verified ECR S3 prefix-list ID;
and budget, retention and teardown decisions. No account or capacity is inferred
from local credentials. The supplied examples/tests use generated sentinels.

1. Independently review the exact template SHA-256 and an inventory of approved
   planned/observed resources. `infrastructure.environment_contract.prepare_stack`
   checks closed input fields, account/region/environment and resource kind before
   producing an immutable parameter/digest record. The inventory and image allowlist
   must come from the deployment/release authority, separately from the request.
   `validate_manifest` validates the native closed environment-manifest fields and
   immutable reference digests, including the explicit independent-audit exception.
2. Create an actual AWS change set under the separately approved provisioner, review
   privilege/egress changes and cost, then obtain the authorized execution decision.
   The offline result deliberately says `provider_change_set: NOT_RUN`.
3. Create the control topology with `ApplicationEnabled=false`. Roles/images may
   remain empty and application resources are absent. Ingress returns503. Bind the
   actual outputs to account, region and environment in the reviewed inventory.
4. Create the role-boundary stack against those approved output bindings. Its
   deployment role can update exactly three declared service ARNs; only the exact
   runtime/execution roles can be passed to ECS. The deployment role is not the
   infrastructure provisioner. Log inputs are exact group ARNs without a wildcard;
   the policy adds only the required group-local `log-stream:*` suffix.
5. Application definition creation requires release-qualified immutable image
   digests and approved role bindings. It still creates services with desired count
   zero and suspends automatic scaling. Runtime identity, authentication, audit,
   egress, health and actual provider qualification must precede activation.

The topology has public TLS443 ingress, six /24 subnets across two availability
zones, private application/database routes with no NAT/default internet route,
private PostgreSQL, six interface endpoints and a narrowly scoped ECR S3 gateway
endpoint. The application security group permits only its database, endpoint and
approved S3-prefix destinations. The current profile grants no WorkOS/public
provider egress; OPE-214 must supply and qualify that boundary before application
activation. RDS is encrypted, private, production Multi-AZ, has seven-day automated
backups and retained storage/key resources. No independent audit storage is created
here. CloudWatch content allowlisting remains the application/telemetry owner's
responsibility; resource configuration alone cannot sanitize log content.

`ApiMaxInstances * ApiPoolPerInstance + MigrationConnectionReserve +
RecoveryConnectionReserve + OtherConnectionBudget <= DatabaseMaxConnections` is
checked before a stack is prepared. The default API pool is20. The scalable target
uses the validated maximum; rolling ECS deployment has MaximumPercent100 so it
cannot double that bound. This deliberately trades rolling availability for the
hard pool budget until the rollout owner supplies a qualified strategy. All other
consumers and simultaneous rollout capacity must be included in the explicit
non-API budget. AWS account/organization permissions must preserve the approved
scaling controls; offline checks cannot prevent a privileged out-of-band update.

cfn-lint1.60.1 validates actual provider resource schemas. The three conditional
TaskDefinitions each locally suppress W1030 because inactive role parameters are
empty; CFN's activation rule refuses empty roles, and preflight validates exact
approved complete IAM ARNs. No other rule is suppressed. Owned tests assert actual
role/trust/egress/DB policies and adverse preflight behavior. AWS OIDC condition-key
support and exact emitted token claims still require an actual trust qualification;
the template does not grant fork PR credentials or relax identity requirements.

## Database compatibility seam

`opentour_control.platform.schema_compatibility` owns only its version/phase table
and transactional guards. Identity DDL remains OPE-142's migration; rollout plans
remain OPE-215's. A dedicated idle autocommit connection runs each approved
in-process callback under an exclusive transaction advisory lock. Every integrated
reader must hold the shared lock for its complete query transaction. Readiness
alone is an observation, not a lifetime reader lock. Direct/unintegrated SQL callers
are not certified by this guard.

Expand preserves the legacy read path. Committed backfill batches retain explicit
progress; failed batches roll back their changes and retain the last cursor.
Verification and dual-read precede switching the bound software digest. Contract
requires completed verified backfill, an independently verified backup checkpoint,
and the exclusive lock excluding participating old readers. After contract, the
old version fails readiness and cannot enter a guarded query transaction. Exact
phase retries do not repeat DDL. Internal PostgreSQL revision counters remain
integers; any future API exposure must use the canonical decimal-string serializer.
Actual disposable PostgreSQL tests exercise role/DROP denial, concurrency, rollback,
readiness and checkpoint refusal. They do not establish RDS/IAM acceptance.

## Baseline cost and recovery inventory

| Component | Empty control topology quantity | Required costing input |
| --- | --- | --- |
| ALB |1 | hours, LCU/traffic, certificate/domain |
| RDS |1 logical DB, Multi-AZ in production | instance class, storage, I/O, backup excess |
| NAT |0; no private internet route | explicit zero reason; future egress costs separately |
| VPC endpoints |6 interfaces across2AZ +1 S3 gateway | endpoint hours and processing |
| Logs |3 groups,14-day initial retention | ingestion/storage/query volume |
| Audit storage | external OPE-217 destination | storage, Object Lock retention, head/entry writes |
| Fargate |0 running tasks initially | qualified task CPU/memory/count/hours |
| SQS |1 queue +1 DLQ | operations and encrypted message volume |
| ECR |3 immutable repositories | retained image storage and scanning |
| KMS |1 environment key | key hours, API operations |
| Secrets |1 unconfigured bootstrap secret plus RDS-managed owner secret | storage/rotation calls |
| Backup storage |7-day RDS retention | measured retained volume and encrypted checkpoints |
| Data transfer | region/AZ-dependent | measured traffic and recovery copies |

`validate_cost_inventory` requires every component, quantity/unit and explicit
zero explanation. It does not invent prices or treat unknown external audit costs
as zero. Daily encrypted configuration checkpoints, weekly isolated nonproduction
restores and quarterly authorized recovery drills require operational ownership and
actual receipts. A dev restore must validate newly bound dev keys/roles/networks;
production references are refused even when copied in snapshot configuration.

## Verification and remaining gates

Run the three native paths with `just test-python`, plus `just lint`,
`just typecheck`, `just validate-spec` and the relevant contract checks. The
migration path uses the separately reviewed finite actual-PostgreSQL command
profile; generic Python tests remain simulated. Record exact commit/spec/template
and command digests, fixture kind and cleanup; never use skipped/missing tests as
success. Independent review is required before acceptance.

AC01 covers separate environment resource authority; AC02 covers role separation
and actual runtime DROP denial; AC03 covers production secret/storage injection
refusal. Named cases include foreign KMS ARN, two-AZ topology, exact OIDC trust,
concurrent migration, old reader, failed backfill, restore permission refusal,
pool overflow, privilege/egress diff and fixed-cost inventory. Actual AWS change-set,
provider deployment, both-mode ISO-01/ISO-03/G2 and issuer/journal runtime integration
remain BLOCKED/NOT_RUN until their concrete authority and resources exist.

Provider semantics: [Fn::Cidr](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/intrinsic-function-reference-cidr.html),
[OIDC condition keys](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_iam-condition-keys.html),
[CloudFormation change sets](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/using-cfn-updating-stacks-changesets.html).
