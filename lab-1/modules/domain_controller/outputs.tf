output "instance_id" {
  value       = aws_instance.this.id
  description = "Domain Controller instance ID"
}

output "private_ip" {
  value       = aws_instance.this.private_ip
  description = "Private IP of the Domain Controller"
}