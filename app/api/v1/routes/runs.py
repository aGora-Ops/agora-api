import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import decrypt_token
from app.db.base import get_db
from app.models.user import User
from app.models.workflow_run import WorkflowRun
from app.schemas.workflow import WorkflowRunList, WorkflowRunResponse
from app.services.github import GitHubService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=WorkflowRunList)
async def list_recent_runs(
    org_login: str | None = Query(default=None, max_length=255),
    repo_name: str | None = Query(default=None, max_length=255),
    run_status: str | None = Query(default=None, alias="status", max_length=64),
    conclusion: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkflowRunList:
    """List persisted workflow runs with optional filters and pagination."""
    query = select(WorkflowRun)
    count_query = select(func.count()).select_from(WorkflowRun)

    if org_login:
        query = query.where(WorkflowRun.org_login == org_login)
        count_query = count_query.where(WorkflowRun.org_login == org_login)
    if repo_name:
        query = query.where(WorkflowRun.repo_name == repo_name)
        count_query = count_query.where(WorkflowRun.repo_name == repo_name)
    if run_status:
        query = query.where(WorkflowRun.status == run_status)
        count_query = count_query.where(WorkflowRun.status == run_status)
    if conclusion:
        query = query.where(WorkflowRun.conclusion == conclusion)
        count_query = count_query.where(WorkflowRun.conclusion == conclusion)

    total = (await db.execute(count_query)).scalar_one()
    query = query.order_by(WorkflowRun.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    runs = result.scalars().all()

    return WorkflowRunList(
        runs=[WorkflowRunResponse.model_validate(r) for r in runs],
        total=total,
    )


@router.get("/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get a workflow run by its database UUID."""
    result = await db.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
    db_run = result.scalar_one_or_none()
    if not db_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return WorkflowRunResponse.model_validate(db_run).model_dump()


@router.get("/{run_id}/logs")
async def get_run_logs(
    run_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the scrubbed log text for a workflow run (fetched live from GitHub)."""
    from app.core.scrubber import scrub

    result = await db.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
    db_run = result.scalar_one_or_none()
    if not db_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    github = GitHubService(decrypt_token(user.access_token_encrypted))
    try:
        raw_logs = await github.get_run_logs_text(
            db_run.org_login, db_run.repo_name, db_run.github_run_id
        )
        return {"logs": scrub(raw_logs)}
    except Exception as exc:
        logger.warning("Failed to fetch logs for run %s: %s", run_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to fetch logs from GitHub. Logs may have expired (GitHub keeps them ~90 days).",
        )
    finally:
        await github.aclose()
