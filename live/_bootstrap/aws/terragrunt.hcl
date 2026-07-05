# Zero Trust Advisor Agent — _bootstrap/aws environment
include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules//appops/vectorstore"
}

inputs = {
  environment = "_bootstrap"
  agent_name  = "zero-trust-advisor-agent"
}
