"""Pipeline Chat — RAG over past pipeline failures.

POST /api/v1/chat
Embeds the user's question (Titan), retrieves the most semantically similar
remediation context from log_embeddings (pgvector cosine search), and has Nova
synthesize an answer grounded in that retrieved context. No SQL generation —
answers come from the indexed corpus of real failures + fixes.
"""
import logging

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
