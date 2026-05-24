locals {
  name = "kali-${var.lab_id}"   # was "controller-${var.lab_id}" — copy-paste from domain_controller module
  tags = merge({
    Name         = "kali-machine-${var.lab_id}"
    Role         = "KaliAttackMachine"
    Project      = "CyberRange"
    LabID        = var.lab_id
    DeploymentID = var.deployment_id
    UserID       = var.user_id
    Environment  = "lab"
    Expiry       = timeadd(timestamp(), "720h")
  }, var.additional_tags)
}

resource "aws_instance" "this" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = var.vpc_security_group_ids
  key_name                    = var.key_name
  private_ip                  = var.private_ip
  associate_public_ip_address = true
  source_dest_check           = true

  root_block_device {
    delete_on_termination = true
    encrypted             = true
  }

    metadata_options {
    http_tokens             = "required"
    http_endpoint           = "enabled"
    http_put_response_hop_limit = 1
  }

  tags = local.tags
}

resource "aws_eip" "this" {
  count    = var.enable_eip ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.this.id

  tags = merge(local.tags, {
    Name = "cyberlab-kali-eip-${var.lab_id}"
  })
}