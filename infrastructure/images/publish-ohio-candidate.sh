#!/usr/bin/env bash
set -euo pipefail
umask 077

layout=$1
digest=$2
work=$3
export AWS_PAGER='' AWS_MAX_ATTEMPTS=1 AWS_EC2_METADATA_DISABLED=true
export AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null

region=us-east-2
oci_type=application/vnd.oci.image.manifest.v1+json
[[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || exit 1
[[ "$GITHUB_SHA" =~ ^[a-f0-9]{40}$ && "$GITHUB_RUN_ID" =~ ^[0-9]+$ &&
   "$GITHUB_RUN_ATTEMPT" =~ ^[0-9]+$ ]] || exit 1
manifest="$layout/blobs/sha256/${digest#sha256:}"
printf '%s  %s\n' "${digest#sha256:}" "$manifest" | sha256sum --check --status

role_name=${ROLE_ARN##*/}
session_name="ope212-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-$IMAGE"
tag="candidate-$GITHUB_SHA-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
registry="$ACCOUNT.dkr.ecr.$region.amazonaws.com"
repository_arn="arn:aws:ecr:$region:$ACCOUNT:repository/$REPOSITORY"
auth="$work/registry.json"
trap 'rm -f -- "$auth"' EXIT

call_aws() {
  aws --region "$region" --output json --no-cli-pager --cli-connect-timeout 5 --cli-read-timeout 25 \
    "$@" 2>"$work/aws-error"
}

if ! call_aws sts get-caller-identity |
  jq -e --arg account "$ACCOUNT" \
    --arg expected_arn "arn:aws:sts::$ACCOUNT:assumed-role/$role_name/$session_name" \
    --arg expected_id "$ROLE_ID:$session_name" \
    '.Account == $account and .Arn == $expected_arn and .UserId == $expected_id' >/dev/null; then
  echo 'AWS caller identity did not match the approved publisher role and session.' >&2
  exit 1
fi

if ! call_aws ecr describe-repositories --registry-id "$ACCOUNT" --repository-names "$REPOSITORY" |
  jq -e --arg urn "$repository_arn" --arg uri "$registry/$REPOSITORY" \
    '.repositories | length == 1 and .[0].repositoryArn == $urn and
      .[0].repositoryUri == $uri and .[0].imageTagMutability == "IMMUTABLE"' >/dev/null; then
  echo 'The destination did not match the approved immutable Ohio repository.' >&2
  exit 1
fi

if ! call_aws ecr get-login-password |
  "$work/oras" login --registry-config "$auth" --username AWS --password-stdin "$registry" \
    >"$work/registry-login" 2>&1; then
  echo 'Temporary access to Ohio ECR failed.' >&2
  exit 1
fi

copy_result=0
"$work/oras" cp --from-oci-layout --to-registry-config "$auth" \
  "$layout@$digest" "$registry/$REPOSITORY:$tag" >"$work/registry-copy" 2>&1 || copy_result=$?

# A lost copy response is resolved only by the same immutable tag and exact manifest bytes.
if ! call_aws ecr batch-get-image --registry-id "$ACCOUNT" --repository-name "$REPOSITORY" \
    --image-ids "imageDigest=$digest,imageTag=$tag" --accepted-media-types "$oci_type" |
  jq -jer --arg account "$ACCOUNT" --arg repo "$REPOSITORY" --arg digest "$digest" \
    --arg tag "$tag" --arg media "$oci_type" '
      select(.failures == [] and (.images | length == 1)) | .images[0]
      | select(.registryId == $account and .repositoryName == $repo and .imageId.imageDigest == $digest
          and .imageId.imageTag == $tag and .imageManifestMediaType == $media)
      | .imageManifest | strings
    ' >"$work/registry-manifest" || ! cmp -s "$manifest" "$work/registry-manifest"; then
  echo 'Ohio publication could not be confirmed. Reconcile its immutable tag before retrying.' >&2
  exit 1
fi
if [[ "$copy_result" != 0 ]]; then
  echo 'An interrupted registry copy was reconciled by exact manifest readback.' >&2
fi
printf '%s\n' "$digest"
