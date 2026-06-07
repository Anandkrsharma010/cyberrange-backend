variable "aws_region" { 
    default = "ap-south-1" 
}
variable "vpc_id" { 
    default = "" 
}
variable "vpc_cidr" { 
    default = "10.30.0.0/16" 
}
variable "ssh_key_name" { 
    type = string 
    default = "cyberrange-key"
}
variable "instance_type" { 
    default = "t3.small" 
}
variable "deployment_id" { 
    type = string 
}
variable "user_id" { 
    type = string 
}
variable "kali_ami_id" { 
    type = string 
    default = "ami-0b92e59275bfcc75a"
}
variable "target_ami_id" { 
    type = string 
    default = "ami-03f4878755434977f"
}
variable "additional_tags" { 
    type = map(string) 
    default = {} 
}
