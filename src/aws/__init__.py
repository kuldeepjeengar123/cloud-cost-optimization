"""Real-AWS client (boto3), same method shape as ``mock_aws.client.MockAWSClient``.

``AWSExecutor`` (see ``actions.executor``) picks between the two by
``cfg.aws_use_mock`` and otherwise doesn't care which one it's holding.
"""

from .real_client import RealAWSClient

__all__ = ["RealAWSClient"]
