from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://agora:password@postgres:5432/agora"
    REDIS_URL: str = "redis://redis:6379/0"

    GITHUB_CLIENT_ID: str = ""
    GITHUB_CLIENT_SECRET: str = ""
    GITHUB_WEBHOOK_SECRET: str = ""
    GITHUB_REDIRECT_URI: str = "http://localhost:3000/api/auth/callback"

    AWS_REGION: str = "us-east-1"
    SQS_QUEUE_URL: str = "https://sqs.us-east-1.amazonaws.com/123456789/pipelineiq-webhooks"
    BEDROCK_MODEL_ID: str = "amazon.nova-pro-v1:0"

    # Pipeline Chat model. nova-pro produces more accurate text-to-SQL than the
    # lighter models; throttles are handled gracefully and the daily token quota
    # is large, so the better SQL quality is worth it. Override per-env if a
    # cheaper model is preferred.
    BEDROCK_CHAT_MODEL_ID: str = "amazon.nova-pro-v1:0"

    # Cross-account Bedrock access (Bedrock account). When set, the API assumes
    # this role before Bedrock calls (Pipeline Chat). Empty = use the pod's IRSA
    # role directly (same account). Mirrors the worker's setting.
    BEDROCK_CROSS_ACCOUNT_ROLE_ARN: str = ""

    ENVIRONMENT: str = "development"

    SECRET_KEY: str = INSECURE_DEFAULT_SECRET
    TOKEN_ENCRYPTION_KEY: str = ""

    FRONTEND_URL: str = "http://localhost:3000"

    ACCESS_TOKEN_EXPIRE_DAYS: int = 30
    ALGORITHM: str = "HS256"

    COOKIE_SECURE: bool = False

    GITHUB_APP_ID: str = ""
    GITHUB_APP_PRIVATE_KEY: str = ""

    # Shared secret checked on /internal/* routes — these are reachable only
    # from inside the cluster (ClusterIP), but the header still distinguishes
    # a legitimate agora-mcp-github call from any other in-cluster pod.
    INTERNAL_API_KEY: str = ""

    # Investigator Agent's entry point (agora-worker's health server, see
    # agora-worker/app/core/health.py) — called synchronously from chat.py.
    WORKER_INTERNAL_URL: str = "http://agora-worker-agora-worker.agora.svc.cluster.local:8080"

    # Bedrock Knowledge Base ID — used by Pipeline Chat's RetrieveAndGenerate path.
    BEDROCK_KB_ID: str = ""

    # Bedrock Guardrail — applied to all converse() calls.
    BEDROCK_GUARDRAIL_ID: str = ""
    BEDROCK_GUARDRAIL_VERSION: str = ""

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"prod", "production"}

    @property
    def cookie_secure(self) -> bool:
        return self.COOKIE_SECURE or self.is_production

settings = Settings()
