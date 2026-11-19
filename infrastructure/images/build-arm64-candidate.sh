#!/usr/bin/env bash
set -euo pipefail

image=$1
work=$2
[[ "$image" == control-api || "$image" == bff ]]
[[ "$(uname -m)" == aarch64 ]]
[[ "$(git rev-parse HEAD)" == "$GITHUB_SHA" ]]

export DOCKER_CONFIG="$work/docker"
export BUILDX_METADATA_PROVENANCE=disabled
trap 'docker buildx rm opentour-candidate >/dev/null 2>&1 || true' EXIT
mkdir -p "$DOCKER_CONFIG/cli-plugins" "$work/source"

# https://github.com/docker/buildx/releases/expanded_assets/v0.39.0
buildx="$DOCKER_CONFIG/cli-plugins/docker-buildx"
curl --fail --silent --show-error --location --retry 3 --max-time 120 \
  --proto '=https' --proto-redir '=https' \
  https://github.com/docker/buildx/releases/download/v0.39.0/buildx-v0.39.0.linux-arm64 \
  --output "$buildx"
printf '%s  %s\n' '722cdcdfdd6f3fb943fa1ba75102d057feb18ba72aab93b1b0a8cf2b2b48a25f' "$buildx" \
  | sha256sum --check --status
chmod 0755 "$buildx"

# BuildKit v0.35.0 Linux ARM64 manifest from the moby/buildkit repository.
docker buildx create --bootstrap --name opentour-candidate --driver docker-container \
  --driver-opt image=docker.io/moby/buildkit@sha256:7dc61a9fab7c7f5a50b1cdbbefa4180829e68c6c0172206eb54ad934de097ddf >&2

git archive "$GITHUB_SHA" | tar -x -C "$work/source"
layout="$work/image"
docker buildx build --builder opentour-candidate --platform linux/arm64 \
  --file "$work/source/infrastructure/images/$image.Dockerfile" \
  --provenance=false --sbom=false \
  --output "type=oci,tar=false,dest=$layout" \
  "$work/source" >&2

digest=$(jq -er '
  select(.schemaVersion == 2 and (.manifests | length == 1))
  | .manifests[0]
  | select(.mediaType == "application/vnd.oci.image.manifest.v1+json")
  | .digest | select(test("^sha256:[a-f0-9]{64}$"))
' "$layout/index.json")
python3 -I tools/verify_release_manifest.py --oci-layout "$layout" --image-digest "$digest" >&2
manifest="$layout/blobs/sha256/${digest#sha256:}"
config_digest=$(jq -er '
  .config.digest | select(test("^sha256:[a-f0-9]{64}$"))
' "$manifest")
jq -e '.os == "linux" and .architecture == "arm64"' \
  "$layout/blobs/sha256/${config_digest#sha256:}" >/dev/null
printf '%s\n' "$digest"
