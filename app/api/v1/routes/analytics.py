from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.base import get_db
from app.models.remediation import Remediation
from app.models.user import User
from app.models.workflow_run import WorkflowRun

router = APIRouter()

@router.get("/")
async def get_analytics(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return aggregate pipeline statistics.

    Response shape matches the frontend ``AnalyticsData`` contract: ``success_rate``
    and ``failure_rate`` are fractions in [0, 1], ``top_failing_repos`` is a list of
    ``{repo, count}``, and ``run_trend`` is a daily series over the last 30 days.
    """
    total_runs = (
        await db.execute(select(func.count()).select_from(WorkflowRun))
    ).scalar_one() or 0

    failed_runs = (
        await db.execute(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.conclusion == "failure")
        )
    ).scalar_one() or 0

    success_runs = (
        await db.execute(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.conclusion == "success")
        )
    ).scalar_one() or 0

    failure_rate = round(failed_runs / total_runs, 4) if total_runs else 0.0
    success_rate = round(success_runs / total_runs, 4) if total_runs else 0.0

    top_failing_result = await db.execute(
        select(WorkflowRun.repo_name, func.count().label("count"))
        .where(WorkflowRun.conclusion == "failure")
        .group_by(WorkflowRun.repo_name)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_failing_repos = [
        {"repo": row.repo_name, "count": row.count} for row in top_failing_result.all()
    ]

    since = datetime.now(timezone.utc) - timedelta(days=30)
    day = func.date(WorkflowRun.created_at)
    trend_result = await db.execute(
        select(
            day.label("date"),
            func.sum(case((WorkflowRun.conclusion == "success", 1), else_=0)).label("success"),
            func.sum(case((WorkflowRun.conclusion == "failure", 1), else_=0)).label("failed"),
        )
        .where(WorkflowRun.created_at >= since)
        .group_by(day)
        .order_by(day)
    )
    run_trend = [
        {"date": str(row.date), "success": int(row.success or 0), "failed": int(row.failed or 0)}
        for row in trend_result.all()
    ]

    # A fix is "raised" once a PR has been opened for it. The Remediation
    # status enum is pending|analyzing|analyzed|pr_raised|helpful|failed —
    # there is no "completed", so the old check always returned 0.
    remediations_raised = (
        await db.execute(
            select(func.count())
            .select_from(Remediation)
            .where(Remediation.status.in_(["pr_raised", "helpful"]))
        )
    ).scalar_one() or 0

    return {
        "total_runs": total_runs,
        "failure_rate": failure_rate,
        "success_rate": success_rate,
        "remediations_raised": remediations_raised,
        "top_failing_repos": top_failing_repos,
        "run_trend": run_trend,
    }
