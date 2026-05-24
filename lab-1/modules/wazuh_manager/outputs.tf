output "instance_id" {
  value       = aws_instance.this.id
  description = "Wazuh Manager instance ID"
}

output "private_ip" {
  value       = aws_instance.this.private_ip
  description = "Private IP address of the Wazuh Manager"
}
