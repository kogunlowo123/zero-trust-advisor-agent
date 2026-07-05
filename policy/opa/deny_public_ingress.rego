package terraform.deny_public_ingress

import rego.v1

deny contains msg if {
    resource := input.resource_changes[_]
    resource.type == "aws_security_group_rule"
    resource.change.after.type == "ingress"
    resource.change.after.cidr_blocks[_] == "0.0.0.0/0"
    msg := sprintf("Public ingress denied on %s", [resource.address])
}
