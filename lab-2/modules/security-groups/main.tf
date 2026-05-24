resource "aws_security_group" "attack" {
  name        = "lab2-sg-attack-${var.lab_id}"
  description = "SG for Kali attacker"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.subnet_cidr]
  }

  egress { 
    from_port = 0 
    to_port = 0 
    protocol = "-1" 
    cidr_blocks = ["0.0.0.0/0"] 
  }

  tags = {
    Name = "lab2-attack-sg-${var.lab_id}"
    Role = "KaliAttacker"
  }
}

resource "aws_security_group" "target" {
  name        = "lab2-sg-target-${var.lab_id}"
  description = "SG for Linux target"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.subnet_cidr]
  }

  egress { 
    from_port = 0 
    to_port = 0 
    protocol = "-1" 
    cidr_blocks = ["0.0.0.0/0"] 
  }

  tags = {
    Name = "lab2-target-sg-${var.lab_id}"
    Role = "LinuxTarget"
  }
}
