locals {
  tags = merge({
    Name        = "${var.name}-${var.lab_id}"
    Project     = "CyberRange"
    Role        = var.role
    LabID       = var.lab_id
    DeploymentID = var.deployment_id
    UserID      = var.user_id
    Environment = "lab"
    Expiry      = timeadd(timestamp(), "720h")
  }, var.additional_tags)
}

resource "aws_instance" "machine" {
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
  }

  tags = local.tags
}