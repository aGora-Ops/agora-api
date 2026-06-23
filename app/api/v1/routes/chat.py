"""Pipeline Chat — count answers from SQL, everything else from the Investigator Agent.

POST /api/v1/chat
A cheap Nova call first classifies the question as `count` (a literal
counting/ranking question over workflow_runs) or `investigate` (anything
else). `count` answers directly from a SQL GROUP BY. `investigate` hands off
to agora-worker's Investigator Agent (app/agents/investigator.py there) — a
bounded tool-calling loop that searches remediation history and reasons
across multiple past runs, replacing the previous single-shot
embed-then-synthesize RAG call.
"""
import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.base import get_db
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=3, max_length=500)


class ChatResponse(BaseModel):
    answer: str
    # `data` carries the retrieved sources (kept on this field for frontend
    # compatibility with the previous SQL-based response shape). `sql` is always
    # None now and retained only so older clients don't break.
    sql: str | None = None
    data: list[dict] | None = None
    error: str | None = None


_INTENT_PROMPT = """Classify the question below into exactly one category:

COUNT - a literal counting/ranking question over workflow run history, e.g. \
"how many failures yesterday", "which repo has the most failures", "show failure trends".
INVESTIGATE - anything else: root causes, "why" questions, comparisons, what kind of issues, \
recurring patterns, or anything needing more than a single number.

QUESTION: {question}

Respond with exactly one word: COUNT or INVESTIGATE"""


async def _classify_intent(message: str, model_id: str, client) -> str:
    """Nova-based intent check. Defaults to INVESTIGATE on any failure — the
    investigator path degrades gracefully (worst case, an unnecessary search),
    while wrongly routing a real question to the count path returns nothing
    useful at all."""
    try:
        resp = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": _INTENT_PROMPT.format(question=message)}]}],
            inferenceConfig={"maxTokens": 16, "temperature": 0},
        )
        raw = resp["output"]["message"]["content"][0]["text"].strip().upper()
        return "COUNT" if "COUNT" in raw else "INVESTIGATE"
    except Exception as exc:
        logger.warning("Intent classification failed, defaulting to INVESTIGATE: %s", exc)
        return "INVESTIGATE"


async def _answer_with_analytics(db: AsyncSession, message: str) -> ChatResponse:
    """Answer count/ranking/time-window questions directly from workflow_runs."""
    message_lower = message.lower()
    since = None
    if "yesterday" in message_lower:
        now = datetime.now(timezone.utc)
        since = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        until = since + timedelta(days=1)
    elif "last 7" in message_lower or "last week" in message_lower:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        until = datetime.now(timezone.utc)
    elif "last 30" in message_lower or "last month" in message_lower:
        since = datetime.now(timezone.utc) - timedelta(days=30)
        until = datetime.now(timezone.utc)

    filters = ["WorkflowRun.conclusion == 'failure'"]
    params: dict = {}
    where_clause = "WHERE conclusion = 'failure'"
    if since:
        where_clause += " AND created_at >= :since AND created_at < :until"
        params["since"] = since
        params["until"] = until

    repo_rows = (
        await db.execute(
            text(
                f"""
                SELECT repo_name, COUNT(*) AS failures
                FROM workflow_runs
                {where_clause}
                GROUP BY repo_name
                ORDER BY failures DESC, repo_name ASC
                LIMIT 10
                """
            ),
            params,
        )
    ).fetchall()

    if not repo_rows:
        return ChatResponse(
            answer="I couldn't find any workflow failure data for that time window.",
            data=[],
        )

    leader = repo_rows[0]
    answer = (
        f"{leader.repo_name} had the most failures"
        + (" yesterday" if "yesterday" in message_lower else "")
        + f" with {leader.failures} failures."
    )
    sources = [
        {"repo": row.repo_name, "category": "—", "relevance": float(row.failures), "summary": f"{row.failures} failures"}
        for row in repo_rows
    ]
    return ChatResponse(answer=answer, data=sources)


async def _answer_with_investigator(message: str) -> ChatResponse:
    """Hand off to agora-worker's Investigator Agent (synchronous HTTP call,
    not Celery — chat needs a request/response cycle here, not async dispatch)."""
    from app.core.config import settings

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.WORKER_INTERNAL_URL}/internal/investigate",
                json={"question": message},
                headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as exc:
        logger.warning("Investigator call failed: %s", exc)
        return ChatResponse(
            answer="I couldn't investigate that question right now. Please try again.",
            error=str(exc),
        )

    # No sources table here — the answer text itself is the refined,
    # citation-bearing response (the investigator's prompt requires it to
    # name repos/dates in prose, not bare tool calls). A row per MCP tool
    # call would just be internal bookkeeping with nothing a user could act
    # on, unlike the count path's table, which shows real per-repo numbers.
    return ChatResponse(answer=result.get("answer", ""), data=None)


@router.post("/", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Classify the question, then answer via SQL count or the Investigator Agent."""
    import boto3

    from app.core.config import settings
    from app.services.bedrock_client import _bedrock_boto3_kwargs

    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )
    intent = await _classify_intent(req.message, settings.BEDROCK_CHAT_MODEL_ID, client)

    if intent == "COUNT":
        return await _answer_with_analytics(db, req.message)

    return await _answer_with_investigator(req.message)
