"""Source assertions for the inactive Ohio dev private identity network."""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "cloudformation"
NETWORK = json.loads((TEMPLATES / "private-identity-network.yaml").read_text())
VPC = {"Ref": "PrivateVpcId"}
SUBNETS = [{"Ref": "PrivateSubnetAId"}, {"Ref": "PrivateSubnetBId"}]
BFF = {"Ref": "BffSecurityGroupId"}
BACKEND = {"Ref": "PrivateListenerSecurityGroupId"}
SHARED = {"Ref": "SharedApplicationSecurityGroupId"}
DEFAULT_GROUPS = {"Fn::ValueOfAll": ["AWS::EC2::VPC::Id", "DefaultSecurityGroup"]}
ENDPOINT_GROUP = {"Fn::GetAtt": ["EndpointSecurityGroup", "GroupId"]}
LINK_GROUP = {"Fn::GetAtt": ["VpcLinkSecurityGroup", "GroupId"]}
NLB_GROUP = {"Fn::GetAtt": ["NetworkLoadBalancerSecurityGroup", "GroupId"]}


def test_approved_ohio_inputs_and_placement_guards() -> None:
    parameters = NETWORK["Parameters"]
    assert set(parameters) == {
        "ExpectedProjectId",
        "PrivateVpcId",
        "PrivateSubnetAId",
        "PrivateSubnetBId",
        "BffSecurityGroupId",
        "PrivateListenerSecurityGroupId",
        "SharedApplicationSecurityGroupId",
    }
    assert all("Default" not in parameter for parameter in parameters.values())
    assert parameters["ExpectedProjectId"]["AllowedPattern"] == "[0-9]{12}"
    assert parameters["PrivateVpcId"]["Type"] == "AWS::EC2::VPC::Id"
    assert all(parameters[subnet["Ref"]]["Type"] == "AWS::EC2::Subnet::Id" for subnet in SUBNETS)
    assert all(
        parameters[group["Ref"]]["Type"] == "AWS::EC2::SecurityGroup::Id"
        for group in (BFF, BACKEND, SHARED)
    )
    rules = NETWORK["Rules"]
    assert [item["Assert"] for item in rules["ApprovedOhioProject"]["Assertions"]] == [
        {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-east-2"]},
        {"Fn::Equals": [{"Ref": "AWS::AccountId"}, {"Ref": "ExpectedProjectId"}]},
    ]
    assert [item["Assert"] for item in rules["PrivateSubnetPlacement"]["Assertions"]] == [
        {"Fn::EachMemberEquals": [{"Fn::ValueOfAll": ["AWS::EC2::Subnet::Id", "VpcId"]}, VPC]},
        {"Fn::Not": [{"Fn::Equals": SUBNETS}]},
    ]
    assert [item["Assert"] for item in rules["DistinctWorkloadGroups"]["Assertions"]] == [
        *[
            {"Fn::Not": [{"Fn::Equals": [left, right]}]}
            for left, right in ((BFF, BACKEND), (BFF, SHARED), (BACKEND, SHARED))
        ],
        *[{"Fn::Not": [{"Fn::Contains": [DEFAULT_GROUPS, group]}]} for group in (BFF, BACKEND)],
    ]


def test_endpoint_starts_with_explicit_deny_all_and_private_dns_off() -> None:
    endpoint = NETWORK["Resources"]["ExecuteApiEndpoint"]["Properties"]
    assert endpoint == {
        "VpcEndpointType": "Interface",
        "VpcId": VPC,
        "ServiceName": {"Fn::Sub": "com.amazonaws.${AWS::Region}.execute-api"},
        "SubnetIds": SUBNETS,
        "SecurityGroupIds": [ENDPOINT_GROUP],
        "IpAddressType": "ipv4",
        "PrivateDnsEnabled": False,
        "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DenyAllUntilApprovedAdmission",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "*",
                    "Resource": "*",
                }
            ],
        },
    }


def test_security_group_edges_are_exact_and_target_only_owned_groups() -> None:
    resources = NETWORK["Resources"]
    groups = {
        name: resource["Properties"]
        for name, resource in resources.items()
        if resource["Type"] == "AWS::EC2::SecurityGroup"
    }
    assert set(groups) == {
        "EndpointSecurityGroup",
        "VpcLinkSecurityGroup",
        "NetworkLoadBalancerSecurityGroup",
    }
    assert all(group["VpcId"] == VPC for group in groups.values())
    inline_ingress = {
        name: [
            {k: v for k, v in rule.items() if k != "Description"}
            for rule in group.get("SecurityGroupIngress", [])
        ]
        for name, group in groups.items()
    }
    assert inline_ingress == {
        "EndpointSecurityGroup": [
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "SourceSecurityGroupId": BFF}
        ],
        "VpcLinkSecurityGroup": [],
        "NetworkLoadBalancerSecurityGroup": [],
    }
    inline_egress = {
        name: [
            {k: v for k, v in rule.items() if k != "Description"}
            for rule in group["SecurityGroupEgress"]
        ]
        for name, group in groups.items()
    }
    assert inline_egress == {
        "EndpointSecurityGroup": [{"IpProtocol": "-1", "CidrIp": "127.0.0.1/32"}],
        "VpcLinkSecurityGroup": [
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "DestinationSecurityGroupId": NLB_GROUP,
            }
        ],
        "NetworkLoadBalancerSecurityGroup": [
            {
                "IpProtocol": "tcp",
                "FromPort": 8443,
                "ToPort": 8443,
                "DestinationSecurityGroupId": BACKEND,
            }
        ],
    }
    standalone_rules = [
        (resource["Type"], {k: v for k, v in resource["Properties"].items() if k != "Description"})
        for resource in resources.values()
        if resource["Type"] in {"AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"}
    ]
    assert standalone_rules == [
        (
            "AWS::EC2::SecurityGroupIngress",
            {
                "GroupId": NLB_GROUP,
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "SourceSecurityGroupId": LINK_GROUP,
            },
        )
    ]


def test_nlb_passes_tcp_to_the_unregistered_8443_target_group() -> None:
    resources = NETWORK["Resources"]
    assert resources["NetworkLoadBalancer"]["Properties"] == {
        "Scheme": "internal",
        "Type": "network",
        "IpAddressType": "ipv4",
        "Subnets": SUBNETS,
        "SecurityGroups": [NLB_GROUP],
        "EnforceSecurityGroupInboundRulesOnPrivateLinkTraffic": "on",
    }
    assert resources["PrivateIdentityTargetGroup"]["Properties"] == {
        "VpcId": VPC,
        "TargetType": "ip",
        "IpAddressType": "ipv4",
        "Protocol": "TCP",
        "Port": 8443,
        "HealthCheckEnabled": True,
        "HealthCheckProtocol": "TCP",
        "HealthCheckPort": "8443",
        "TargetGroupAttributes": [{"Key": "preserve_client_ip.enabled", "Value": "false"}],
    }
    assert resources["PrivateTlsPassthroughListener"]["Properties"] == {
        "LoadBalancerArn": {"Ref": "NetworkLoadBalancer"},
        "Protocol": "TCP",
        "Port": 443,
        "DefaultActions": [
            {"Type": "forward", "TargetGroupArn": {"Ref": "PrivateIdentityTargetGroup"}}
        ],
    }


def test_scope_and_outputs_match_the_staged_gateway_handoff() -> None:
    assert {name: r["Type"] for name, r in NETWORK["Resources"].items()} == {
        "EndpointSecurityGroup": "AWS::EC2::SecurityGroup",
        "ExecuteApiEndpoint": "AWS::EC2::VPCEndpoint",
        "VpcLinkSecurityGroup": "AWS::EC2::SecurityGroup",
        "NetworkLoadBalancerSecurityGroup": "AWS::EC2::SecurityGroup",
        "VpcLinkToNlbIngress": "AWS::EC2::SecurityGroupIngress",
        "NetworkLoadBalancer": "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "PrivateIdentityTargetGroup": "AWS::ElasticLoadBalancingV2::TargetGroup",
        "PrivateTlsPassthroughListener": "AWS::ElasticLoadBalancingV2::Listener",
    }
    outputs = NETWORK["Outputs"]
    assert {name: value["Value"] for name, value in outputs.items()} == {
        "PrivateVpcId": VPC,
        "PrivateSubnetAId": SUBNETS[0],
        "PrivateSubnetBId": SUBNETS[1],
        "BffSecurityGroupId": BFF,
        "PrivateListenerSecurityGroupId": BACKEND,
        "EndpointId": {"Ref": "ExecuteApiEndpoint"},
        "EndpointSecurityGroupId": ENDPOINT_GROUP,
        "VpcLinkSecurityGroupId": LINK_GROUP,
        "NetworkLoadBalancerSecurityGroupId": NLB_GROUP,
        "NetworkLoadBalancerArn": {"Ref": "NetworkLoadBalancer"},
        "NetworkLoadBalancerNameAndId": {
            "Fn::Join": [
                "/",
                [
                    {"Fn::GetAtt": ["NetworkLoadBalancer", "LoadBalancerName"]},
                    {
                        "Fn::Select": [
                            2,
                            {
                                "Fn::Split": [
                                    "/",
                                    {"Fn::GetAtt": ["NetworkLoadBalancer", "LoadBalancerFullName"]},
                                ]
                            },
                        ]
                    },
                ],
            ]
        },
        "NetworkLoadBalancerListenerArn": {"Ref": "PrivateTlsPassthroughListener"},
        "PrivateIdentityTargetGroupArn": {"Ref": "PrivateIdentityTargetGroup"},
    }
    assert all("Export" not in output for output in outputs.values())
    gateway = json.loads((TEMPLATES / "network-secrets-policy.yaml").read_text())
    for parameter in ("VpcLinkV2Id", "EndpointId", "NetworkLoadBalancerNameAndId"):
        assert "Default" not in gateway["Parameters"][parameter]
    assert "VpcLinkV2Id" not in outputs
