#!/bin/bash
set -euo pipefail

LAB_TYPE=$1
DEPLOYMENT_ID=$2

if [ -z "${LAB_TYPE:-}" ] || [ -z "${DEPLOYMENT_ID:-}" ]; then
  echo "Usage: $0 <lab_type> <deployment_id>" >&2
  exit 1
fi

if [ "$LAB_TYPE" != "windows" ]; then
  echo "Unsupported lab type: $LAB_TYPE" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WORKSPACE="ws-${DEPLOYMENT_ID}"

DESTROY_TIMEOUT_SECONDS=${DESTROY_TIMEOUT_SECONDS:-1200}  # default 20 min, override via env

echo "Destroying deployment=$DEPLOYMENT_ID workspace=$WORKSPACE" >&2
echo "Destroy timeout: ${DESTROY_TIMEOUT_SECONDS}s" >&2

terraform init -input=false >&2

# Check if workspace exists
if ! terraform workspace list | grep -q "$WORKSPACE"; then
  echo "Workspace $WORKSPACE not found. Assuming already destroyed." >&2
  exit 0
fi

terraform workspace select "$WORKSPACE" >/dev/null

# Run terraform destroy under timeout. See deploy-lab.sh for rationale.
timeout --kill-after=30s "${DESTROY_TIMEOUT_SECONDS}s" \
  terraform destroy -auto-approve \
    -var="deployment_id=$DEPLOYMENT_ID" \
    -var="lab_type=$LAB_TYPE" \
    -var="user_id=destroy" \
    -var="tenant_subnet_cidr=10.0.0.0/24" \
    -var="headscale_server=https://destroy.invalid" \
    -var="headscale_ssm_param=destroy" \
    -var="dc_ami_id=ami-00000000000000000" \
    -var="client_ami_id=ami-00000000000000000" \
    -var="kali_ami_id=ami-00000000000000000" \
    -var="subnet_router_golden_ami=ami-00000000000000000" \
    -var="ssh_key_name=destroy" \
    -var="windows_key_name=destroy" \
    >&2

DESTROY_EXIT=$?
if [ $DESTROY_EXIT -eq 124 ]; then
  echo "ERROR: terraform destroy timed out after ${DESTROY_TIMEOUT_SECONDS}s for deployment=$DEPLOYMENT_ID" >&2
  echo "WARNING: AWS resources for workspace=$WORKSPACE may still be running. Manual cleanup required." >&2
  exit 124
fi

terraform workspace select default >/dev/null

terraform workspace delete "$WORKSPACE" >/dev/null || true

rm -rf "terraform.tfstate.d/$WORKSPACE" || true

echo "Destroy complete" >&2
