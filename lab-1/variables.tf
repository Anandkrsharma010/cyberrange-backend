# variables.tf
# Variable definitions for cybersecurity lab infrastructure

variable "aws_region" {
  description = "AWS region for lab deployment"
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "Environment name (e.g., dev, prod, lab)"
  type        = string
  default     = "lab"
}

# VPC Configuration
variable "vpc_id" {
  description = "Existing VPC ID (leave empty to create new VPC)"
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.20.0.0/16"
}

variable "route_table_id" {
  description = "Existing Route Table ID to associate with the lab subnet (leave empty to create a new one)."
  type        = string
  default     = ""
}


# Headscale Configuration
variable "headscale_server" {
  description = "Headscale server URL"
  type        = string
}

variable "headscale_ssm_param" {
  description = "SSM parameter name containing the Headscale auth key (T-09: never the raw key)"
  type        = string
}

# AMI IDs
variable "dc_ami_id" {
  description = "Pre-configured vulnerable Domain Controller AMI ID"
  type        = string
}

variable "client_ami_id" {
  description = "Pre-configured vulnerable Windows Client AMI ID"
  type        = string
}

variable "kali_ami_id" {
  description = "Pre-configured Kali Attack Machine"
  type = string
}

variable "wazuh_ami_id" {
  description = "Pre-configured Wazuh Manager AMI ID"
  type        = string
  default     = ""
}

variable "subnet_router_golden_ami" {
  description = "AMI ID of the golden subnet router image"
  type        = string
}

# Instance Types
variable "subnet_router_instance_type" {
  description = "Instance type for subnet router"
  type        = string
  default     = "t3.micro"
}

variable "dc_instance_type" {
  description = "Instance type for domain controller"
  type        = string
  default     = "t3.medium"
}

variable "client_instance_type" {
  description = "Instance type for domain client"
  type        = string
  default     = "t3.small"
}

variable "kali_instance_type" {
  description = "Instance type for kali attack machine"
  type = string
  default = "t2.large"
}

variable "wazuh_instance_type" {
  description = "Instance type for Wazuh manager"
  type        = string
  default     = "t3.medium"
}

# SSH/RDP Keys
variable "ssh_key_name" {
  description = "AWS SSH key pair name for Linux instances"
  type        = string
}

variable "windows_key_name" {
  description = "AWS key pair name for Windows instances"
  type        = string
}

# Domain Configuration
variable "domain_name" {
  description = "Active Directory domain name"
  type        = string
  default     = "corp.local"
}

# Postgres connection string for Lambda logging
# Tags
variable "additional_tags" {
  description = "Additional tags to apply to all resources"
  type        = map(string)
  default     = {}
}

variable "deployment_id" {
  description = "Unique ID representing a batch of labs deployed for one user"
  type        = string
}

variable "lab_type" {
  description = "Type of lab to deploy"
  type        = string
}

variable "user_id" {
  description = "User ID who owns this deployment"
  type        = string
}

variable "tenant_subnet_cidr" {
  type        = string
  description = "Tenant /24 subnet CIDR allocated by backend (e.g., 10.20.2.0/24)"
}