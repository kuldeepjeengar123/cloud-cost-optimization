"""Real-AWS client (boto3) used by ``actions.executor.AWSExecutor`` for every
AWS-backed action, plus the shared on-demand pricing table."""

from .real_client import RealAWSClient

__all__ = ["RealAWSClient"]
