locals {
  name = "wazuh-${var.lab_id}"
  tags = merge({
    Name        = "cyberlab-wazuh-${var.lab_id}"
    Role        = "WazuhManager"
    Project     = "CyberRange"
    Environment = "lab"
    LabID       = var.lab_id
    DeploymentID  = var.deployment_id
    UserID      = var.user_id
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
    volume_size           = 50
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

    metadata_options {
    http_tokens             = "required"
    http_endpoint           = "enabled"
    http_put_response_hop_limit = 1
  }

  tags = local.tags
}