from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    cal_api_key: str
    cal_api_base: str
    llm_provider: str
    anthropic_model: str
    bedrock_model: str
    aws_region: str
    aws_profile: str | None
    effort: str
    access_passcode: str
    sessions_table: str | None

    @property
    def model(self) -> str:
        return self.bedrock_model if self.llm_provider == "bedrock" else self.anthropic_model


SECRETS = ("CAL_API_KEY", "ANTHROPIC_API_KEY", "ACCESS_PASSCODE")


def _load_ssm(prefix: str) -> None:
    """On AWS the secrets live in SSM Parameter Store (SecureString), not in the Lambda's env vars."""
    import boto3

    prefix = prefix.rstrip("/") + "/"
    resp = boto3.client("ssm").get_parameters(Names=[prefix + n for n in SECRETS], WithDecryption=True)
    for p in resp["Parameters"]:
        os.environ[p["Name"].removeprefix(prefix)] = p["Value"]


def load_settings() -> Settings:
    load_dotenv()
    if os.environ.get("SSM_PREFIX"):
        _load_ssm(os.environ["SSM_PREFIX"])
    return Settings(
        cal_api_key=os.environ.get("CAL_API_KEY", ""),
        cal_api_base=os.environ.get("CAL_API_BASE", "https://api.cal.com/v2"),
        llm_provider=os.environ.get("LLM_PROVIDER", "anthropic").lower(),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        bedrock_model=os.environ.get("BEDROCK_MODEL", "anthropic.claude-sonnet-5-5"),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        aws_profile=os.environ.get("AWS_PROFILE") or None,
        effort=os.environ.get("LLM_EFFORT", "medium"),
        # Gate for the hosted URL (it can change your calendar). Empty = no gate, for local dev.
        access_passcode=os.environ.get("ACCESS_PASSCODE", ""),
        # DynamoDB table for sessions on Lambda; unset = in-memory, for local dev.
        sessions_table=os.environ.get("SESSIONS_TABLE") or None,
    )
