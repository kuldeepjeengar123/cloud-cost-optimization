"""Resize a single EC2 instance: stop -> modify instance type -> start."""

import sys
import time
from pathlib import Path

import boto3
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def main(instance_id: str, new_type: str, region: str) -> None:
    ec2 = boto3.client("ec2", region_name=region)

    print(f"Stopping {instance_id} ...")
    ec2.stop_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_stopped").wait(InstanceIds=[instance_id])
    print("Stopped.")

    print(f"Changing instance type to {new_type} ...")
    ec2.modify_instance_attribute(
        InstanceId=instance_id, InstanceType={"Value": new_type}
    )

    print(f"Starting {instance_id} ...")
    ec2.start_instances(InstanceIds=[instance_id])
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    print("Started.")

    resp = ec2.describe_instances(InstanceIds=[instance_id])
    inst = resp["Reservations"][0]["Instances"][0]
    print(f"Final state: {inst['State']['Name']}, type: {inst['InstanceType']}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: resize_instance.py <instance_id> <new_type> <region>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
