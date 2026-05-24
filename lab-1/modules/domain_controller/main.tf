locals {
  name = "controller-${var.lab_id}"
  tags = merge({
    Name        = "cyberlab-controller-${var.lab_id}"
    Role        = "DomainController"
    Project     = "CyberRange"
    LabID       = var.lab_id
    DeploymentID = var.deployment_id
    UserID      = var.user_id
    Environment = "lab"
    Expiry      = timeadd(timestamp(), "720h")
  }, var.additional_tags)
}

resource "aws_instance" "this" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = var.vpc_security_group_ids
  key_name               = var.key_name
  private_ip             = var.private_ip
  associate_public_ip_address = false
  source_dest_check      = true

  root_block_device {
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_tokens             = "required"
    http_endpoint           = "enabled"
    http_put_response_hop_limit = 1
  }

  # Helps avoid transient provider read-after-create issues.
  timeouts {
    create = "15m"
  }

  tags = local.tags
}