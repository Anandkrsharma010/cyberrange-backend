variable "ami_id" {
  description = "AMI ID for the domain controller"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for the domain controller"
  type        = string
  default     = "t3.medium"
}

variable "subnet_id" {
  description = "Subnet ID for the domain controller"
  type        = string
}

variable "vpc_security_group_ids" {
  description = "Security groups to attach"
  type        = list(string)
}

variable "key_name" {
  description = "SSH/RDP key pair"
  type        = string
}

variable "private_ip" {
  description = "Fixed private IP for the DC"
  type        = string
}

variable "lab_id" {
  description = "Unique lab identifier"
  type        = string
}

variable "additional_tags" {
  description = "Additional tags for resources"
  type        = map(string)
  default     = {}
}

variable "deployment_id" {
  description = "Unique ID for this deployment"
  type        = string
}

variable "user_id" {
  description = "User ID who owns this deployment"
  type        = string
}
