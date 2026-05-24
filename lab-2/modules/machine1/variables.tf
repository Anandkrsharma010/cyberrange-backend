variable "ami_id" {
     type = string
}
variable "instance_type" { 
    type = string 
}
variable "subnet_id" { 
    type = string 
}
variable "vpc_security_group_ids" { 
    type = list(string) 
}
variable "key_name" { 
    type = string 
}
variable "private_ip" { 
    type = string 
}
variable "lab_id" { 
    type = string 
}
variable "deployment_id" { 
    type = string 
}
variable "user_id" { 
    type = string 
}
variable "name" { 
    type = string 
}
variable "role" { 
    type = string 
}
variable "additional_tags" {
  type    = map(string)
  default = {}
}