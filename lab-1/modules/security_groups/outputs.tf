output "sg_subnet_router_id" {
  value = aws_security_group.subnet_router.id
}

output "sg_domain_controller_id" {
  value = aws_security_group.domain_controller.id
}

output "sg_domain_client_id" {
  value = aws_security_group.domain_client.id
}

output "sg_wazuh_id" {
  value = aws_security_group.wazuh.id
}

output "sg_kali_id" {
  value = aws_security_group.kali_machine.id
}
