output "instance_id" {
  description = "Router instance ID"
  value       = aws_instance.this.id
}

output "private_ip" {
  description = "Router private IP"
  value       = aws_instance.this.private_ip
}

output "public_ip" {
  description = "Router public IP (EIP if enabled, else ephemeral)"
  value       = try(aws_eip.this[0].public_ip, aws_instance.this.public_ip)
}

output "name" {
  description = "Router hostname"
  value       = "${var.hostname_prefix}-${var.lab_id}"
}
