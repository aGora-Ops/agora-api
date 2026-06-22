import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import decrypt_token
from app.db.base import get_db
from app.models.fix_memory import FixMemory
from app.models.remediation import Remediation
from app.models.user import User
from app.models.workflow_run import WorkflowRun
from app.schemas.remediation import RemediationDetail, RemediationList, RemediationResponse
from app.services.github import GitHubService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", response_model=RemediationList)
async def list_remediations(
    org_login: str | None = Query(default=None, max_length=255),
    repo_name: str | None = Query(default=None, max_length=255),
    remediation_status: str | None = Query(default=None, alias="status", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RemediationList:
    """List remediations with optional filters. Paginated."""
    query = select(Remediation)
    count_query = select(func.count()).select_from(Remediation)

    if org_login:
        query = query.where(Remediation.org_login == org_login)
        count_query = count_query.where(Remediation.org_login == org_login)
    if repo_name:
        query = query.where(Remediation.repo_name == repo_name)
        count_query = count_query.where(Remediation.repo_name == repo_name)
    if remediation_status:
        query = query.where(Remediation.status == remediation_status)
        count_query = count_query.where(Remediation.status == remediation_status)

    total = (await db.execute(count_query)).scalar_one()
    offset = (page - 1) * page_size
    query = query.order_by(Remediation.created_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(query)
    remediations = result.scalars().all()

    return RemediationList(
        remediations=[RemediationResponse.model_validate(r) for r in remediations],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/{remediation_id}/mark-helpful", response_model=RemediationDetail)
async def mark_helpful(
    remediation_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RemediationDetail:
    """
    Mark a remediation as helpful (fix accepted and merged by the user).
    Queues the fix for ingestion into the Bedrock Knowledge Base so future
    analyses of similar failures benefit from this accepted solution.
    """
    result = await db.execute(select(Remediation).where(Remediation.id == remediation_id))
    remediation = result.scalar_one_or_none()
    if not remediation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Remediation not found")
    if remediation.status not in ("pr_raised", "analyzed"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only analyzed or pr_raised remediations can be marked helpful",
        )

    remediation.status = "helpful"

    if remediation.suggested_yaml:
        memory = FixMemory(
            org_login=remediation.org_login,
            repo_name=remediation.repo_name,
            workflow_file=remediation.workflow_file,
            failure_category=remediation.failure_category or "UNKNOWN",
            root_cause=remediation.root_cause,
            original_yaml="",
            fixed_yaml=remediation.suggested_yaml,
            remediation_id=remediation.id,
        )
        db.add(memory)
        logger.info(
            "Fix memory stored for remediation %s (category=%s)",
            remediation_id,
            memory.failure_category,
        )

    await db.commit()
    await db.refresh(remediation)

    logger.info(
        "Remediation %s marked helpful - fix memory ingested (org=%s, repo=%s)",
        remediation_id,
        remediation.org_login,
        remediation.repo_name,
    )

    return RemediationDetail.model_validate(remediation)


@router.get("/search", response_model=list[RemediationResponse])
async def search_remediations(
    q: str,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RemediationResponse]:
    """
    Semantic search over remediation history via Bedrock Knowledge Base + OpenSearch.
    Falls back to simple text match when Knowledge Base is not configured.
    """
    from sqlalchemy import or_

    result = await db.execute(
        select(Remediation)
        .where(
            or_(
                Remediation.root_cause.ilike(f"%{q}%"),
                Remediation.repo_name.ilike(f"%{q}%"),
                Remediation.workflow_file.ilike(f"%{q}%"),
            )
        )
        .order_by(Remediation.created_at.desc())
        .limit(20)
    )
    return [RemediationResponse.model_validate(r) for r in result.scalars().all()]


@router.get("/{remediation_id}/similar", response_model=list[RemediationResponse])
async def get_similar_remediations(
    remediation_id: uuid.UUID,
    limit: int = Query(default=5, ge=1, le=20),
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RemediationResponse]:
    """
    Feature 4 - Multi-Repo Correlation.
    Return up to `limit` remediations from OTHER repos that share the same
    failure_category and a similar root cause.
    """
    from sqlalchemy import or_

    result = await db.execute(select(Remediation).where(Remediation.id == remediation_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Remediation not found")

    words = [w for w in source.root_cause.split() if len(w) > 4][:6]
    if not words:
        return []

    keyword_conditions = [Remediation.root_cause.ilike(f"%{w}%") for w in words]

    similar_result = await db.execute(
        select(Remediation)
        .where(
            Remediation.id != remediation_id,
            Remediation.repo_name != source.repo_name,
            or_(*keyword_conditions),
            Remediation.status.in_(["analyzed", "pr_raised", "helpful"]),
        )
        .order_by(Remediation.created_at.desc())
        .limit(limit)
    )
    return [RemediationResponse.model_validate(r) for r in similar_result.scalars().all()]


@router.get("/{remediation_id}", response_model=RemediationDetail)
async def get_remediation(
    remediation_id: uuid.UUID,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RemediationDetail:
    """Get a single remediation including suggested_yaml."""
    result = await db.execute(select(Remediation).where(Remediation.id == remediation_id))
    remediation = result.scalar_one_or_none()
    if not remediation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Remediation not found")
    return RemediationDetail.model_validate(remediation)


@router.post("/{remediation_id}/raise-pr", response_model=RemediationDetail)
async def raise_pr(
    remediation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RemediationDetail:
    """
    User-triggered PR creation for an analyzed remediation.
    """
    result = await db.execute(select(Remediation).where(Remediation.id == remediation_id))
    remediation = result.scalar_one_or_none()
    if not remediation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Remediation not found")

    if remediation.status == "pr_raised":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="PR already raised")

    if not remediation.suggested_yaml:
        detail = (
            "AI analysis is ready, but no valid YAML suggestion was produced for this remediation."
            if remediation.status == "analyzed"
            else "No suggested fix available yet - analysis may still be running"
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        )

    run_result = await db.execute(
        select(WorkflowRun).where(WorkflowRun.id == remediation.workflow_run_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow run not found")

    from app.services.github_app import get_installation_token, github_app_configured

    if github_app_configured():
        token = await get_installation_token(run.org_login)
        logger.info("Using GitHub App token for PR creation (fine-grained permissions)")
    else:
        token = decrypt_token(user.access_token_encrypted)
        logger.warning(
            "GitHub App not configured - using user OAuth token for PR creation. "
            "Set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY to use the more secure GitHub App path."
        )

    github = GitHubService(token)
    fix_branch = f"agora/fix-{run.github_run_id}"
    try:
        await github.create_fix_branch(run.org_login, run.repo_name, run.head_sha, fix_branch)

        current_sha = await github.get_file_sha(
            run.org_login, run.repo_name, remediation.workflow_file, run.head_sha
        )

        await github.commit_fix(
            owner=run.org_login,
            repo=run.repo_name,
            branch=fix_branch,
            path=remediation.workflow_file,
            content=remediation.suggested_yaml,
            message=f"fix: aGorA AI-suggested fix for workflow run #{run.github_run_id}",
            current_sha=current_sha,
        )

        pr_body = (
            f"## Root Cause\n{remediation.root_cause}\n\n"
            "## Changes\nThis fix was suggested by aGorA (AWS Bedrock - Amazon Nova)."
            " Please review before merging.\n\n"
            "> Generated by [aGorA](https://github.com/aGora-Ops)"
        )
        pr_data = await github.create_pr(
            owner=run.org_login,
            repo=run.repo_name,
            head=fix_branch,
            base=run.branch,
            title=f"fix: AI remediation for {run.workflow_name} (run #{run.github_run_id})",
            body=pr_body,
        )

        remediation.pr_url = pr_data.get("html_url")
        remediation.pr_number = pr_data.get("number")
        remediation.pr_branch = fix_branch
        remediation.status = "pr_raised"
        await db.commit()
        await db.refresh(remediation)
        return RemediationDetail.model_validate(remediation)

    except Exception as exc:
        logger.exception("Failed to raise PR for remediation %s: %s", remediation_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create PR on GitHub.",
        )
    finally:
        await github.aclose()
