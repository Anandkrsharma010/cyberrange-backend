variable "vpc_id" {
  description = "VPC ID where SGs will be created"
  type        = string
}

variable "lab_cidr" {
  description = "Lab subnet CIDR"
  type        = string
}

variable "lab_id" {
  description = "Unique lab identifier"
  type        = string
}

variable "additional_tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}
