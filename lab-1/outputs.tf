# -----------------------------
# LAB SUMMARY (single JSON object)
# -----------------------------
output "lab_summary" {
  description = "Summary of deployed lab resources for API"
  value = {
    lab_id      = random_string.lab_id.result
    subnet_cidr = local.subnet_cidr
    vpc_id      = local.vpc_id
    subnet_id   = aws_subnet.lab.id

    instances = {
      domain_controller = {
        id         = module.domain_controller.instance_id
        private_ip = module.domain_controller.private_ip
      }
      domain_client = {
        id         = module.domain_client.instance_id
        private_ip = module.domain_client.private_ip
      }
      wazuh_manager = {
        id         = module.wazuh_manager.instance_id
        private_ip = module.wazuh_manager.private_ip
      }
      subnet_router = {
        id          = module.subnet_router.instance_id
        private_ip  = module.subnet_router.private_ip
        public_ip   = module.subnet_router.public_ip
      }
      kali_machine  = {
        id          = module.kali_machine.instance_id
        private_ip  = module.kali_machine.private_ip
        public_ip   = module.kali_machine.public_ip
      }
    }

    headscale_server = var.headscale_server
  }
}