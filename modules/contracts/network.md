# Network Contract (ADR-0001)

## Promise
Every cloud implementation provides an isolated network boundary for Zero Trust Advisor Agent:
- Private subnets with no direct public ingress
- NAT gateway for outbound internet access
- DNS resolution for internal services
- Security groups / NSGs restricting traffic to required ports only
- VPN or Private Link connectivity to on-premises systems

## Interface
| Operation       | Input                    | Output              |
|----------------|--------------------------|----------------------|
| `create_vpc`   | cidr, region, az_count   | vpc_id, subnet_ids  |
| `create_sg`    | vpc_id, rules[]          | security_group_id   |
| `create_endpoint` | vpc_id, service       | endpoint_id         |

## Implementors
- `modules/netops/network/aws/` — VPC with public/private/isolated subnets
- `modules/netops/network/azure/` — VNet with subnet delegation
- `modules/netops/network/gcp/` — VPC with private Google access
