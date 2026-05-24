#!/bin/bash
set -e
LAB_TYPE=$1
DEPLOYMENT_ID=$2
USER_ID=$3
TENANT_SUBNET_CIDR=$4

echo "Deploying Lab-2 for user=$USER_ID deployment=$DEPLOYMENT_ID subnet=$TENANT_SUBNET_CIDR"

terraform init -upgrade
terraform apply -auto-approve \
  -var="deployment_id=$DEPLOYMENT_ID" \
  -var="user_id=$USER_ID"

terraform output -json lab_summary
