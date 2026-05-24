variable "ami_id" {
  description = "AMI ID for the Wazuh Manager (pre-baked or base Ubuntu)"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for the Wazuh Manager"
  type        = string
  default     = "t3.medium"
}

variable "subnet_id" {
  description = "Subnet ID to launch Wazuh Manager in"
  type        = string
}

variable "vpc_security_group_ids" {
  description = "List of security group IDs to associate with the Wazuh Manager"
  type        = list(string)
}

variable "key_name" {
  description = "SSH key pair for Wazuh Manager access"
  type        = string
}

variable "private_ip" {
  description = "Fixed private IP for the Wazuh Manager"
  type        = string
}

variable "lab_id" {
  description = "Unique identifier for the lab"
  type        = string
}

variable "use_custom_user_data" {
  description = "Flag to use custom user-data script instead of baked AMI"
  type        = bool
  default     = false
}

variable "additional_tags" {
  description = "Additional tags for all resources"
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
