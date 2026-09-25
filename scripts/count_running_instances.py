"""Count running EC2 instances using credentials from .env."""

import os
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main() -> None:
    region = os.environ.get("AWS_DEFAULT_REGION", "eu-north-1")
    ec2 = boto3.client("ec2", region_name=region)

    paginator = ec2.get_paginator("describe_instances")
    running = []
    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
    ):
        for reservation in page["Reservations"]:
            running.extend(reservation["Instances"])

    print(f"Region: {region}")
    print(f"Running instances: {len(running)}")
    for instance in running:
        name = next(
            (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
            "(unnamed)",
        )
        print(f"  - {instance['InstanceId']} ({instance['InstanceType']}) {name}")


if __name__ == "__main__":
    main()
