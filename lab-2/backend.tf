terraform {
  backend "s3" {
    bucket         = "cyberrange-tfstate-prod-ap-south-1"
    key            = "lab-2/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "cyberrange-tfstate-lock"
    encrypt        = true
  }
}