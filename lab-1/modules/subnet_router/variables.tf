variable "ami_id" {
  description = "Golden AMI ID for the subnet router"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for the subnet router"
  type        = string
  default     = "t3.nano"
}

variable "subnet_id" {
  description = "Subnet ID to place the router in"
  type        = string
}

variable "vpc_security_group_ids" {
  description = "Security group IDs to attach to the router"
  type        = list(string)
}

variable "key_name" {
  description = "SSH key pair name for the router"
  type        = string
}

variable "associate_public_ip_address" {
  description = "Whether to assign a public IP (recommended: true)"
  type        = bool
  default     = true
}

variable "enable_eip" {
  description = "Allocate & attach an Elastic IP to the router"
  type        = bool
  default     = false
}

variable "headscale_server" {
  description = "Headscale server URL (e.g., https://hscystar.dedyn.io)"
  type        = string
}

# T-09: SSM parameter NAME only — never the raw auth key.
# The EC2 instance fetches the actual key from SSM at boot using its IAM role.
variable "headscale_ssm_param" {
  description = "SSM parameter path containing the Headscale auth key for this deployment"
  type        = string
}

variable "advertised_routes" {
  description = "CIDR (or comma-separated CIDRs) to advertise to Headscale"
  type        = string
}

variable "lab_id" {
  description = "Unique lab identifier for tagging/hostname"
  type        = string
}

variable "additional_tags" {
  description = "Extra tags to attach"
  type        = map(string)
  default     = {}
}

variable "hostname_prefix" {
  description = "Optional hostname prefix for the router"
  type        = string
  default     = "router"
}

variable "deployment_id" {
  description = "Unique ID for this deployment"
  type        = string
}

variable "user_id" {
  description = "User ID who owns this deployment"
  type        = string
}
