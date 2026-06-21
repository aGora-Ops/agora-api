"""Natural Language Pipeline Chat — Feature 3.

POST /api/v1/chat
Accepts a plain-English question about the user's CI/CD data, converts it to
safe read-only SQL via Bedrock, executes it against the database, and returns
a conversational answer plus the raw data rows.
"""
import json
import logging
import re
import time

import boto3
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.base import get_db
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Schema ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=3, max_length=500)


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None
    data: list[dict] | None = None
    error: str | None = None


# ── DB Schema description passed to Bedrock ───────────────────────────────────

_DB_SCHEMA = """
PostgreSQL database tables (READ-ONLY access — SELECT only):

TABLE: workflow_runs
  id UUID PK, github_run_id BIGINT, org_login TEXT, repo_name TEXT,
  workflow_name TEXT, workflow_file TEXT, branch TEXT, head_sha TEXT,
  status TEXT (lifecycle state ONLY: queued|in_progress|completed — this is NOT the outcome),
  conclusion TEXT (the OUTCOME: success|failure|cancelled|skipped|null),
  started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, html_url TEXT, created_at TIMESTAMPTZ
  -- IMPORTANT: a run FAILED when conclusion = 'failure'. Never use status for outcome.

TABLE: remediations
  id UUID PK, workflow_run_id UUID FK→workflow_runs.id,
  org_login TEXT, repo_name TEXT, workflow_file TEXT,
  root_cause TEXT, suggested_yaml TEXT,
  status TEXT (values: pending|analyzing|analyzed|pr_raised|helpful|failed),
  pr_url TEXT, pr_number INT, pr_branch TEXT,
  bedrock_model TEXT, error_message TEXT,
  confidence_score INT (0-100, null if not yet scored),
  confidence_reasoning TEXT,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ

TABLE: organizations
  id UUID PK, github_org_id BIGINT, login TEXT, name TEXT,
  sync_status TEXT, created_at TIMESTAMPTZ

TABLE: users
  id UUID PK, github_id BIGINT, login TEXT, name TEXT, created_at TIMESTAMPTZ
"""

_SYSTEM_PROMPT = f"""You are an AI assistant for aGorA, a CI/CD remediation platform.
The user will ask questions about their GitHub Actions workflow data.
You convert questions into PostgreSQL SELECT queries and explain the results.

DATABASE SCHEMA:
{_DB_SCHEMA}

RULES:
1. Output ONLY a JSON object: {{"sql": "<SELECT query>", "explanation": "<what query does>"}}
2. ONLY use SELECT. Never INSERT, UPDATE, DELETE, DROP, or any DDL.
3. If the question cannot be answered with SQL (e.g. it's a greeting), output:
   {{"sql": null, "explanation": "<direct answer>"}}
4. Limit results to 50 rows unless the user specifies otherwise.
5. Use ILIKE for case-insensitive text matching.
6. Timestamps are UTC. Use NOW() - INTERVAL '7 days' for "last week" etc.
7. "Failures" / "failed runs" mean workflow_runs.conclusion = 'failure'. The
   `status` column is the lifecycle state (queued/in_progress/completed), never
   the outcome — do not filter failures on `status`.
"""


# ── Bedrock helper ────────────────────────────────────────────────────────────

def _extract_json_object(text: str) -> dict | None:
    """Pull the first JSON object out of a model response.

    Smaller models (e.g. nova-lite) often wrap the JSON in markdown fences or
    surrounding prose ("Here is the query: ```json {...} ```"), which breaks a
    naive json.loads. Try a fenced block first, then the first balanced-looking
    {...} span, then the whole string.
    """
    candidates = []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    candidates.append(text)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _call_bedrock_for_sql(question: str, model_id: str, client) -> tuple[str | None, str]:
    """Ask Bedrock to convert a question to SQL. Returns (sql, explanation)."""
    messages = [{"role": "user", "content": [{"text": question}]}]
    raw = ""
    for attempt in range(3):
        try:
            response = client.converse(
                modelId=model_id,
                system=[{"text": _SYSTEM_PROMPT}],
                messages=messages,
                inferenceConfig={"maxTokens": 512, "temperature": 0},
            )
        except client.exceptions.ThrottlingException:
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise

        raw = response["output"]["message"]["content"][0]["text"].strip()
        parsed = _extract_json_object(raw)
        if parsed is not None:
            return parsed.get("sql"), parsed.get("explanation", "")
        # Model returned a bare SQL statement without the JSON wrapper.
        if raw.lstrip().upper().startswith("SELECT"):
            return raw.rstrip(";"), "Generated query."
        logger.warning("Bedrock chat: could not parse model output as JSON | raw=%r", raw)
        return None, "I couldn't generate a query for that question."
    return None, "Service unavailable."


_FORBIDDEN_SQL_KEYWORDS = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
                           "TRUNCATE", "GRANT", "REVOKE", "EXECUTE", "CALL", "MERGE", "COPY"]


def _is_safe_sql(sql: str) -> bool:
    """Reject any SQL that isn't a single, plain SELECT statement."""
    normalized = re.sub(r"\s+", " ", sql.strip()).rstrip(";").strip()
    upper = normalized.upper()
    if not upper.startswith("SELECT"):
        return False
    # Reject stacked statements (e.g. "SELECT 1; DELETE FROM users")
    if ";" in normalized:
        return False
    # Word-boundary match so legitimate identifiers like created_at / updated_at
    # don't trip the "CREATE"/"UPDATE" substrings.
    return not any(re.search(rf"\b{kw}\b", upper) for kw in _FORBIDDEN_SQL_KEYWORDS)


# ── Route ─────────────────────────────────────────────────────────────────────

@router.post("/", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """
    Natural Language Pipeline Chat.

    Convert a plain-English question about CI/CD data into SQL via Bedrock,
    execute it safely, and return a human-readable answer with the raw rows.
    """
    from app.services.bedrock_client import _bedrock_boto3_kwargs
    from app.core.config import settings

    bedrock_client = boto3.client(
        "bedrock-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )
    model_id = settings.BEDROCK_CHAT_MODEL_ID

    # Generate SQL. A Bedrock throttle here is surfaced as a friendly message
    # (not a 500) so the UI can tell the user to retry later.
    try:
        sql, explanation = _call_bedrock_for_sql(req.message, model_id, bedrock_client)
    except bedrock_client.exceptions.ThrottlingException:
        logger.warning("Bedrock throttled chat request for user %s", user.id)
        return ChatResponse(
            answer="The AI service is rate-limited right now (daily token quota reached). Please try again later.",
            sql=None, data=None,
            error="Bedrock throttling — daily token quota reached.",
        )
    except Exception as exc:
        logger.warning("Chat SQL generation failed for user %s: %s", user.id, exc)
        return ChatResponse(
            answer="I couldn't process that question right now. Please try again.",
            sql=None, data=None, error=str(exc),
        )

    # No SQL needed — just a direct explanation (greetings, meta questions)
    if not sql:
        return ChatResponse(answer=explanation, sql=None, data=None)

    if not _is_safe_sql(sql):
        logger.warning("Bedrock generated unsafe SQL for user %s: %r", user.id, sql)
        return ChatResponse(
            answer="I can only run read-only queries against your pipeline data.",
            sql=None, data=None,
            error="Generated SQL was not a safe SELECT statement.",
        )

    try:
        result = await db.execute(text(sql))
        rows = [dict(row._mapping) for row in result.fetchmany(50)]
    except Exception as exc:
        logger.warning("Chat SQL execution failed: %s | sql=%r", exc, sql)
        return ChatResponse(
            answer="I ran into an error executing that query.",
            sql=sql,
            data=None,
            error=str(exc),
        )

    # Serialize rows — convert UUIDs/datetimes to strings
    serializable_rows = [
        {
            k: str(v) if not isinstance(v, (int, float, bool, type(None), str)) else v
            for k, v in row.items()
        }
        for row in rows
    ]

    # Summarize the results in plain English — best-effort. If Bedrock is
    # throttled or errors here, we still return the query + data rather than
    # losing a successful result to a summary failure.
    try:
        summary_prompt = (
            f"Question: {req.message}\n"
            f"SQL: {sql}\n"
            f"Results ({len(rows)} rows): {str(rows[:10])}\n\n"
            "Summarize the results in 1-3 plain English sentences."
        )
        summary_response = bedrock_client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": summary_prompt}]}],
            inferenceConfig={"maxTokens": 256},
        )
        answer = summary_response["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:
        logger.warning("Chat summary failed (returning raw rows): %s", exc)
        answer = f"Found {len(rows)} row(s) for your query. (AI summary unavailable right now.)"

    return ChatResponse(answer=answer, sql=sql, data=serializable_rows)
