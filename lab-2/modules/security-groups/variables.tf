variable "vpc_id" {
  description = "VPC ID where the lab subnet is created"
  type        = string
}

variable "subnet_cidr" {
  description = "CIDR block of the lab subnet"
  type        = string
}

variable "lab_id" {
  description = "Unique ID for this lab deployment"
  type        = string
}

variable "additional_tags" {
  description = "Optional additional tags to add to all SGs"
  type        = map(string)
  default     = {}
}