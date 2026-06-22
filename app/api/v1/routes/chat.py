"""Pipeline Chat — RAG over past pipeline failures.

POST /api/v1/chat
Embeds the user's question (Titan), retrieves the most semantically similar
remediation context from log_embeddings (pgvector cosine search), and has Nova
synthesize an answer grounded in that retrieved context. No SQL generation —
answers come from the indexed corpus of real failures + fixes.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

import boto3
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


_TOP_K = 6

_ANSWER_PROMPT = """You are aGorA's CI/CD assistant. Answer the user's question using ONLY the \
CONTEXT below — it contains real past pipeline failures from their repositories, each with the \
root cause and the fix that was suggested. Be specific: reference repositories and failure \
categories when relevant. If the context does not contain enough to answer, say so plainly \
instead of guessing.

CONTEXT:
{context}

QUESTION: {question}

Answer in 1-4 sentences:"""


def _synthesize(question: str, context: str, model_id: str, client) -> str:
    resp = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": _ANSWER_PROMPT.format(context=context, question=question)}]}],
        inferenceConfig={"maxTokens": 512, "temperature": 0},
    )
    return resp["output"]["message"]["content"][0]["text"].strip()


def _looks_like_aggregate_question(message: str) -> bool:
    text = message.lower()
    patterns = [
        r"\bmost\b",
        r"\btop\b",
        r"\bhow many\b",
        r"\bcount\b",
        r"\btrend\b",
        r"\byesterday\b",
        r"\blast (day|week|month|7 days|30 days)\b",
        r"\bwhich repo\b",
        r"\bwhat repo\b",
    ]
    return any(re.search(p, text) for p in patterns)


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


@router.post("/", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    """Answer a question via retrieval-augmented generation over past failures."""
    from app.core.config import settings
    from app.services.bedrock_client import _bedrock_boto3_kwargs
    from app.services.embeddings import embed_text, to_pgvector

    if _looks_like_aggregate_question(req.message):
        return await _answer_with_analytics(db, req.message)

    # 1. Embed the question.
    try:
        qvec = to_pgvector(embed_text(req.message))
    except Exception as exc:
        logger.warning("Chat embedding failed for user %s: %s", user.id, exc)
        return ChatResponse(
            answer="I couldn't process that question right now. Please try again.",
            error=str(exc),
        )

    # 2. Retrieve the top-k most similar chunks (cosine distance via pgvector).
    rows = (
        await db.execute(
            text(
                """
                SELECT repo_name, failure_category, chunk_text,
                       1 - (embedding <=> CAST(:qvec AS vector)) AS score
                FROM log_embeddings
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :k
                """
            ),
            {"qvec": qvec, "k": _TOP_K},
        )
    ).fetchall()

    if not rows:
        return ChatResponse(
            answer=(
                "I don't have any indexed pipeline data yet. Once some failures have been "
                "analyzed, I'll be able to answer questions about them."
            ),
            data=[],
        )

    context = "\n\n---\n\n".join(r.chunk_text for r in rows)
    sources = [
        {
            "repo": r.repo_name or "—",
            "category": r.failure_category or "—",
            "relevance": round(float(r.score), 3),
            "summary": (r.chunk_text or "").replace("\n", " ")[:120],
        }
        for r in rows
    ]

    # 3. Synthesize a grounded answer.
    client = boto3.client(
        "bedrock-runtime",
        region_name=settings.AWS_REGION,
        **_bedrock_boto3_kwargs(),
    )
    try:
        answer = _synthesize(req.message, context, settings.BEDROCK_CHAT_MODEL_ID, client)
    except client.exceptions.ThrottlingException:
        logger.warning("Bedrock throttled chat synthesis for user %s", user.id)
        return ChatResponse(
            answer=(
                "The AI service is rate-limited right now. Here are the most relevant items "
                "I found — try again shortly for a written summary."
            ),
            data=sources,
            error="Bedrock throttling.",
        )
    except Exception as exc:
        logger.warning("Chat synthesis failed for user %s: %s", user.id, exc)
        answer = f"I found {len(rows)} related items but couldn't generate a summary right now."

    return ChatResponse(answer=answer, data=sources)
