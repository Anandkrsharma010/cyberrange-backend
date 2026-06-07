terraform {
  required_version = "~> 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "aws" { region = var.aws_region }

resource "random_string" "lab_id" {
  length  = 6
  special = false
}

# --- Existing VPC or new ---
data "aws_vpc" "main" {
  count = var.vpc_id != "" ? 1 : 0
  id    = var.vpc_id
}

resource "aws_vpc" "main" {
  count      = var.vpc_id == "" ? 1 : 0
  cidr_block = var.vpc_cidr
}

locals {
  vpc_id      = var.vpc_id != "" ? data.aws_vpc.main[0].id : aws_vpc.main[0].id
  user_number = try(tonumber(regexreplace(var.user_id, "[^0-9]", "")), 1)
  subnet_cidr = cidrsubnet(var.vpc_cidr, 8, local.user_number)
}

resource "aws_subnet" "lab" {
  vpc_id            = local.vpc_id
  cidr_block        = local.subnet_cidr
  availability_zone = data.aws_availability_zones.available.names[0]

  tags = {
    Name = "lab2-subnet-${random_string.lab_id.result}"
  }
}

# --- SGs ---
module "security_groups" {
  source      = "./modules/security-groups"
  vpc_id      = local.vpc_id
  subnet_cidr = local.subnet_cidr
  lab_id      = random_string.lab_id.result
}

# --- Machines ---
module "attack_machine" {
  source                 = "./modules/machine1"
  ami_id                 = var.kali_ami_id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [module.security_groups.sg_attack_id]
  key_name               = var.ssh_key_name
  private_ip             = cidrhost(local.subnet_cidr, 10)
  lab_id                 = random_string.lab_id.result
  deployment_id          = var.deployment_id
  user_id                = var.user_id
  name                   = "kali"
  role                   = "KaliAttacker"
}

module "linux_target" {
  source                 = "./modules/machine1"
  ami_id                 = var.target_ami_id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [module.security_groups.sg_target_id]
  key_name               = var.ssh_key_name
  private_ip             = cidrhost(local.subnet_cidr, 20)
  lab_id                 = random_string.lab_id.result
  deployment_id          = var.deployment_id
  user_id                = var.user_id
  name                   = "target"
  role                   = "LinuxTarget"
}

data "aws_availability_zones" "available" {
  state = "available"
}

# Outputs
output "lab_summary" {
  value = {
    lab_id      = random_string.lab_id.result
    subnet_cidr = local.subnet_cidr
    subnet_id   = aws_subnet.lab.id

    instances = {
      attacker = {
        id         = module.attack_machine.instance_id
        private_ip = module.attack_machine.private_ip
      }
      target = {
        id         = module.linux_target.instance_id
        private_ip = module.linux_target.private_ip
      }
    }
  }
}
