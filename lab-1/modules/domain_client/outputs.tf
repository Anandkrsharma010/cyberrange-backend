output "instance_id" {
  value       = aws_instance.this.id
  description = "Domain Client instance ID"
}

output "private_ip" {
  value       = aws_instance.this.private_ip
  description = "Private IP of the Domain Client"
}