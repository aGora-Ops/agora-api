import boto3

from app.core.config import settings


def _guardrail_config() -> dict:
    """Return guardrailConfig kwarg for converse() if a guardrail is configured."""
    gid = settings.BEDROCK_GUARDRAIL_ID
    gver = settings.BEDROCK_GUARDRAIL_VERSION
    if gid and gver:
        return {"guardrailConfig": {"guardrailIdentifier": gid, "guardrailVersion": gver}}
    return {}


def _bedrock_boto3_kwargs() -> dict:
    """Return boto3 keyword args for Bedrock clients.

    If BEDROCK_CROSS_ACCOUNT_ROLE_ARN is configured, assumes that role first
    (cross-account access to company Bedrock) and returns short-lived credentials.
    Otherwise returns an empty dict so boto3 uses the pod's IRSA role directly.
    """
    if not settings.BEDROCK_CROSS_ACCOUNT_ROLE_ARN:
        return {}
    sts = boto3.client("sts", region_name=settings.AWS_REGION)
    assumed = sts.assume_role(
        RoleArn=settings.BEDROCK_CROSS_ACCOUNT_ROLE_ARN,
        RoleSessionName="agora-api-bedrock",
        DurationSeconds=3600,
    )
    creds = assumed["Credentials"]
    return {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
    }
