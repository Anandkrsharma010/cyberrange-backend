# modules/security_groups/main.tf

# ---------------------------
# SECURITY GROUP MODULE
# ---------------------------

# Subnet Router SG
resource "aws_security_group" "subnet_router" {
  name        = "cyberlab-sg-router-${var.lab_id}"
  description = "Allows Headscale connectivity and SSH from lab subnet"
  vpc_id      = var.vpc_id

  ingress {
    description = "Allow all inbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.lab_cidr]
  }
  ingress {
  description = "Allow SSH from my IP"
  from_port   = 22
  to_port     = 22
  protocol    = "tcp"
  cidr_blocks = ["0.0.0.0/0"]
  }


  egress {
    description = "Allow all outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge({
    Name  = "cyberlab-router-sg-${var.lab_id}"
    Role  = "SubnetRouter"
    LabID = var.lab_id
  }, var.additional_tags)
}

# Domain Controller SG
resource "aws_security_group" "domain_controller" {
  name        = "cyberlab-sg-dc-${var.lab_id}"
  description = "Allow RDP, Kerberos, LDAP from lab subnet"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.lab_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge({
    Name  = "cyberlab-dc-sg-${var.lab_id}"
    Role  = "DomainController"
    LabID = var.lab_id
  }, var.additional_tags)
}

# Domain Client SG
resource "aws_security_group" "domain_client" {
  name        = "cyberlab-sg-client-${var.lab_id}"
  description = "Allow RDP and Wazuh agent traffic"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.lab_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge({
    Name  = "cyberlab-client-sg-${var.lab_id}"
    Role  = "DomainClient"
    LabID = var.lab_id
  }, var.additional_tags)
}

resource "aws_security_group" "kali_machine" {
  name        = "cyberlab-sg-kali-${var.lab_id}"
  description = "Allow RDP and Wazuh agent traffic"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.lab_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge({
    Name  = "cyberlab-kali-sg-${var.lab_id}"
    Role  = "KaliAttack"
    LabID = var.lab_id
  }, var.additional_tags)
}

# Wazuh Manager SG
resource "aws_security_group" "wazuh" {
  name        = "cyberlab-sg-wazuh-${var.lab_id}"
  description = "Allow Wazuh API/UI within lab subnet"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.lab_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge({
    Name  = "cyberlab-wazuh-sg-${var.lab_id}"
    Role  = "WazuhManager"
    LabID = var.lab_id
  }, var.additional_tags)
}