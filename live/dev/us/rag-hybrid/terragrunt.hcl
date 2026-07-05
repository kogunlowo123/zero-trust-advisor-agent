# Zero Trust Advisor Agent — dev/us/rag-hybrid environment
include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules//appops/vectorstore"
}

inputs = {
  environment = "dev"
  agent_name  = "zero-trust-advisor-agent"
}
