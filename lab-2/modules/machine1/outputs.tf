output "instance_id" { 
    value = aws_instance.machine.id
}
output "private_ip" { 
    value = aws_instance.machine.private_ip
}
