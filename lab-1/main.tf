# main.tf
# Multi-tenant Cybersecurity Lab Infrastructure
# Each deployment creates an isolated subnet with DC, Client, Wazuh, and Subnet Router

terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 5.89.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
    time = {
      source = "hashicorp/time"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Owner = "pratik@iitm"
    }
  }
}

# Generate unique lab ID for this deployment
resource "random_string" "lab_id" {
  length  = 6
  special = false
  upper   = false
}

# VPC - Using existing or create new
data "aws_vpc" "main" {
  count = var.vpc_id != "" ? 1 : 0
  id    = var.vpc_id
}

resource "aws_vpc" "main" {
  count                = var.vpc_id == "" ? 1 : 0
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge({
    Name        = "cyberlab-vpc-${random_string.lab_id.result}"
    Environment = var.environment
    LabID       = random_string.lab_id.result
  }, var.additional_tags)
}

locals {
  vpc_id      = var.vpc_id != "" ? data.aws_vpc.main[0].id : aws_vpc.main[0].id
  subnet_cidr = var.tenant_subnet_cidr
}

# Internet Gateway
data "aws_internet_gateway" "main" {
  count = var.vpc_id != "" ? 1 : 0

  filter {
    name   = "attachment.vpc-id"
    values = [local.vpc_id]
  }
}

resource "aws_internet_gateway" "main" {
  count  = var.vpc_id == "" ? 1 : 0
  vpc_id = local.vpc_id

  tags = merge({
    Name        = "cyberlab-igw-${random_string.lab_id.result}"
    Environment = var.environment
    LabID       = random_string.lab_id.result
  }, var.additional_tags)
}

locals {
  igw_id = var.vpc_id != "" ? data.aws_internet_gateway.main[0].id : aws_internet_gateway.main[0].id
}

module "lab_security_groups" {
  source          = "./modules/security_groups"
  vpc_id          = local.vpc_id
  lab_cidr        = local.subnet_cidr
  lab_id          = random_string.lab_id.result
  additional_tags = var.additional_tags
}

# Lab Subnet - Each deployment gets its own isolated subnet
resource "aws_subnet" "lab" {
  vpc_id                  = local.vpc_id
  cidr_block              = local.subnet_cidr
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = merge({
    Name        = "cyberlab-subnet-${random_string.lab_id.result}"
    Environment = var.environment
    LabID       = random_string.lab_id.result
    LabSubnet   = local.subnet_cidr
  }, var.additional_tags)
}

# Route Table
resource "aws_route_table" "lab" {
  count  = var.route_table_id == "" ? 1 : 0
  vpc_id = local.vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = local.igw_id
  }

  tags = merge({
    Name        = "cyberlab-rt-${random_string.lab_id.result}"
    Environment = var.environment
    LabID       = random_string.lab_id.result
  }, var.additional_tags)
}

locals {
  route_table_id_final = var.route_table_id != "" ? var.route_table_id : aws_route_table.lab[0].id
}

resource "aws_route_table_association" "lab" {
  subnet_id      = aws_subnet.lab.id
  route_table_id = local.route_table_id_final
}

# Data source for availability zones
data "aws_availability_zones" "available" {
  state = "available"
}

# Subnet Router Instance
module "subnet_router" {
  source = "./modules/subnet_router"

  ami_id                      = var.subnet_router_golden_ami # NEW var in variables.tf
  instance_type               = var.subnet_router_instance_type
  subnet_id                   = aws_subnet.lab.id
  vpc_security_group_ids      = [module.lab_security_groups.sg_subnet_router_id]
  key_name                    = var.ssh_key_name
  associate_public_ip_address = true
  enable_eip                  = false

  headscale_server   = var.headscale_server
  headscale_ssm_param = var.headscale_ssm_param
  advertised_routes  = local.subnet_cidr
  lab_id             = random_string.lab_id.result
  user_id            = var.user_id
  deployment_id      = var.deployment_id
  additional_tags    = var.additional_tags
  hostname_prefix    = "router"

  # Serialize router after the other lab instances so the AWS provider
  # is not reading multiple new aws_instance resources in parallel
  # (works around "collecting instance settings: empty result").
  depends_on = [
    module.wazuh_manager,
    module.domain_client,
    module.kali_machine,
    aws_route_table_association.lab,
  ]
}

# Temporary wait barrier to reduce AWS eventual-consistency race
# before creating the domain controller instance.
resource "time_sleep" "before_domain_controller" {
  depends_on = [
    module.subnet_router,
    aws_route_table_association.lab,
  ]

  create_duration = "45s"
}

# Domain Controller Instance
module "domain_controller" {
  source                 = "./modules/domain_controller"
  ami_id                 = var.dc_ami_id
  instance_type          = var.dc_instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [module.lab_security_groups.sg_domain_controller_id]
  key_name               = var.windows_key_name
  private_ip             = cidrhost(local.subnet_cidr, 10)
  lab_id                 = random_string.lab_id.result
  user_id            = var.user_id
  deployment_id      = var.deployment_id
  additional_tags        = var.additional_tags

  depends_on = [time_sleep.before_domain_controller]
}

#Domain Client Instance
module "domain_client" {
  source                 = "./modules/domain_client"
  ami_id                 = var.client_ami_id
  instance_type          = var.client_instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [module.lab_security_groups.sg_domain_client_id]
  key_name               = var.windows_key_name
  private_ip             = cidrhost(local.subnet_cidr, 20)
  lab_id                 = random_string.lab_id.result
  user_id                = var.user_id
  deployment_id          = var.deployment_id
  additional_tags        = var.additional_tags
}

# Wazuh Manager Instance
module "wazuh_manager" {
  source                 = "./modules/wazuh_manager"
  ami_id                 = var.wazuh_ami_id
  instance_type          = var.wazuh_instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [module.lab_security_groups.sg_wazuh_id]
  key_name               = var.ssh_key_name
  private_ip             = cidrhost(local.subnet_cidr, 30)
  lab_id                 = random_string.lab_id.result
  user_id                = var.user_id
  deployment_id          = var.deployment_id
  additional_tags        = var.additional_tags
}

#Kali Attack Instance
module "kali_machine" {
  source                 = "./modules/kali_machine"
  ami_id                 = var.kali_ami_id
  instance_type          = var.kali_instance_type
  subnet_id              = aws_subnet.lab.id
  vpc_security_group_ids = [module.lab_security_groups.sg_kali_id]
  key_name               = var.ssh_key_name
  private_ip             = cidrhost(local.subnet_cidr, 40)
  lab_id                 = random_string.lab_id.result
  user_id                = var.user_id
  deployment_id          = var.deployment_id
  additional_tags        = var.additional_tags

  depends_on             = [module.lab_security_groups]
}

# Data source for latest Ubuntu AMI (fallback)
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}