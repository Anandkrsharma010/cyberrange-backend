locals {
  name = "router-${var.deployment_id}"
  tags = merge({
    Name         = "cyberlab-router-${var.deployment_id}"
    Role         = "SubnetRouter"
    Project      = "CyberRange"
    Environment  = "lab"
    DeploymentID = var.deployment_id
    UserID       = var.user_id
    Expiry       = timeadd(timestamp(), "720h")
  }, var.additional_tags)
}

# ── IAM role for the subnet router instance ───────────────────────────────────
# Grants ssm:GetParameter on the scoped /cyberrange/headscale-key/* path only.

resource "aws_iam_role" "router" {
  name = "cyberrange-subnet-router-${var.deployment_id}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "router_ssm" {
  name = "cyberrange-router-ssm-${var.deployment_id}"
  role = aws_iam_role.router.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "FetchHeadscaleKey"
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        # Scoped to the exact parameter for this deployment only
        Resource = "arn:aws:ssm:*:*:parameter/cyberrange/headscale-key/${var.deployment_id}"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "router" {
  name = "cyberrange-subnet-router-${var.deployment_id}"
  role = aws_iam_role.router.name
}

# EC2 RunInstances can race IAM: profile must be visible to the account before launch.
resource "time_sleep" "wait_for_router_instance_profile" {
  depends_on      = [aws_iam_instance_profile.router]
  create_duration = "45s"
}

# ── EC2 instance ──────────────────────────────────────────────────────────────

resource "aws_instance" "this" {
  depends_on = [time_sleep.wait_for_router_instance_profile]

  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = var.vpc_security_group_ids
  key_name                    = var.key_name
  source_dest_check           = false
  associate_public_ip_address = var.associate_public_ip_address
  iam_instance_profile        = aws_iam_instance_profile.router.name

  # T-09: user_data fetches the Headscale key from SSM at boot.
  # var.headscale_ssm_param is just a path string — no secret value in state.
 user_data = <<-EOT
#!/bin/bash
set -euo pipefail

TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

if [ -z "$TOKEN" ]; then
  echo "ERROR: Failed to get IMDSv2 token" >&2
  exit 1
fi

REGION=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/placement/region)

if [ -z "$REGION" ]; then
  echo "ERROR: Failed to get region from IMDS" >&2
  exit 1
fi

echo "Detected region: $REGION" >&2

# Wait for IAM instance profile credentials to propagate (max 30s)
echo "Waiting for IAM credentials..." >&2
for i in $(seq 1 10); do
  aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1 && break
  echo "Attempt $i: credentials not ready, retrying in 3s..." >&2
  sleep 3
done

if ! aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1; then
  echo "ERROR: IAM credentials not available after 30s" >&2
  exit 1
fi

echo "IAM credentials ready" >&2

SSM_PARAM="${var.headscale_ssm_param}"
echo "Fetching Headscale auth key from SSM: $SSM_PARAM" >&2

TS_AUTHKEY=$(aws ssm get-parameter \
  --name "$SSM_PARAM" \
  --with-decryption \
  --region "$REGION" \
  --query "Parameter.Value" \
  --output text)

if [ -z "$TS_AUTHKEY" ]; then
  echo "ERROR: SSM parameter returned empty value" >&2
  exit 1
fi

cat >/etc/subnet-router.env <<ENV
HEADSCALE_URL="${var.headscale_server}"
TS_AUTHKEY="$TS_AUTHKEY"
ADVERTISE_ROUTES="${var.advertised_routes}"
TS_HOSTNAME="${local.name}"
TS_ADVERTISE_TAGS="tag:router"
ENV

chmod 600 /etc/subnet-router.env
unset TS_AUTHKEY
systemctl enable --now subnet-router-join.service
EOT

  root_block_device {
    delete_on_termination = true
    encrypted             = true
  }

    metadata_options {
    http_tokens             = "required"
    http_endpoint           = "enabled"
    http_put_response_hop_limit = 1
  }

  # Avoid transient AWS provider "collecting instance settings: empty result" after create.
  timeouts {
    create = "15m"
  }

  tags = local.tags
}

resource "aws_eip" "this" {
  count    = var.enable_eip ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.this.id

  tags = merge(local.tags, {
    Name = "cyberlab-router-eip-${var.deployment_id}"
  })
}