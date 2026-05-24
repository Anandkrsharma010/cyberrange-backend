#!/bin/bash
set -euo pipefail

LAB_TYPE=$1
DEPLOYMENT_ID=$2
USER_ID=$3
TENANT_SUBNET_CIDR=$4

if [ -z "${LAB_TYPE:-}" ] || [ -z "${DEPLOYMENT_ID:-}" ] || [ -z "${USER_ID:-}" ] || [ -z "${TENANT_SUBNET_CIDR:-}" ]; then
  echo "Usage: HEADSCALE_SSM_PARAM=<param> $0 <lab_type> <deployment_id> <user_id> <tenant_subnet_cidr>" >&2
  exit 1
fi

if [ -z "${HEADSCALE_SSM_PARAM:-}" ]; then
  echo "ERROR: HEADSCALE_SSM_PARAM environment variable is not set." >&2
  exit 1
fi

if [ "$LAB_TYPE" != "windows" ] && [ "$LAB_TYPE" != "wazuh" ]; then
  echo "Unsupported lab type: $LAB_TYPE" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WORKSPACE="ws-${DEPLOYMENT_ID}"

APPLY_TIMEOUT_SECONDS=${APPLY_TIMEOUT_SECONDS:-1800}  # default 30 min, override via env
TF_PARALLELISM=${TF_PARALLELISM:-1}

echo "Deploying lab_type=$LAB_TYPE user=$USER_ID deployment=$DEPLOYMENT_ID workspace=$WORKSPACE subnet=$TENANT_SUBNET_CIDR" >&2
echo "Headscale SSM param: $HEADSCALE_SSM_PARAM" >&2
echo "Apply timeout: ${APPLY_TIMEOUT_SECONDS}s" >&2
echo "Terraform parallelism: ${TF_PARALLELISM}" >&2

terraform init -input=false -upgrade >&2

terraform workspace new "$WORKSPACE" >/dev/null 2>&1 || terraform workspace select "$WORKSPACE" || terraform workspace new "$WORKSPACE" >/dev/null

export TF_VAR_headscale_ssm_param="$HEADSCALE_SSM_PARAM"


timeout --kill-after=30s "${APPLY_TIMEOUT_SECONDS}s" \
  terraform apply -auto-approve \
    -parallelism="${TF_PARALLELISM}" \
    -var="deployment_id=$DEPLOYMENT_ID" \
    -var="user_id=$USER_ID" \
    -var="lab_type=$LAB_TYPE" \
    -var="tenant_subnet_cidr=$TENANT_SUBNET_CIDR" \
    >&2

APPLY_EXIT=$?
if [ $APPLY_EXIT -eq 124 ]; then
  echo "ERROR: terraform apply timed out after ${APPLY_TIMEOUT_SECONDS}s for deployment=$DEPLOYMENT_ID" >&2
  exit 124
fi

unset TF_VAR_headscale_ssm_param

# ONLY JSON on stdout — consumed by the worker
terraform output -json
