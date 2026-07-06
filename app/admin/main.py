"""
FastAPI admin panel.

Endpoints:
  GET  /admin/users          — list users with usage stats
  GET  /admin/users/{id}     — user detail
  POST /admin/users/{id}/ban — ban/unban user
  POST /admin/users/{id}/plan — change subscription plan
  GET  /admin/projects        — recent projects
  GET  /admin/stats           — platform statistics
  GET  /admin/health          — health check
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models.project import Project, ProjectStatus
from app.models.user import SubscriptionPlan, User

app = FastAPI(title="Testova Admin", version="1.0.0", docs_url="/admin/docs")


def require_admin(x_admin_secret: str = Header(...)) -> None:
    if x_admin_secret != settings.ADMIN_API_SECRET:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/admin/health")
async def health() -> dict:
    return {"status": "ok"}


# ── Users ─────────────────────────────────────────────────────────────────────

@app.get("/admin/users", dependencies=[Depends(require_admin)])
async def list_users(
    page: int = 1,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict:
    offset = (page - 1) * limit
    result = await session.execute(
        select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
    )
    users = result.scalars().all()
    count = await session.scalar(select(func.count(User.id)))
    return {
        "total": count,
        "page": page,
        "users": [
            {
                "id": str(u.id),
                "telegram_id": u.telegram_id,
                "username": u.username,
                "full_name": u.full_name,
                "plan": u.subscription_plan.value,
                "daily_used": u.daily_projects_used,
                "total": u.total_projects,
                "is_banned": u.is_banned,
                "created_at": u.created_at.isoformat(),
            }
            for u in users
        ],
    }


@app.post("/admin/users/{telegram_id}/ban", dependencies=[Depends(require_admin)])
async def ban_user(
    telegram_id: int,
    ban: bool = True,
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_banned = ban
    await session.commit()
    return {"telegram_id": telegram_id, "is_banned": ban}


@app.post("/admin/users/{telegram_id}/plan", dependencies=[Depends(require_admin)])
async def set_plan(
    telegram_id: int,
    plan: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        plan_enum = SubscriptionPlan(plan)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {plan}")

    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.subscription_plan = plan_enum
    await session.commit()
    return {"telegram_id": telegram_id, "plan": plan}


# ── Projects ──────────────────────────────────────────────────────────────────

@app.get("/admin/projects", dependencies=[Depends(require_admin)])
async def list_projects(
    page: int = 1,
    limit: int = 50,
    session: AsyncSession = Depends(get_session),
) -> dict:
    offset = (page - 1) * limit
    result = await session.execute(
        select(Project).order_by(Project.created_at.desc()).offset(offset).limit(limit)
    )
    projects = result.scalars().all()
    return {
        "projects": [
            {
                "id": str(p.id),
                "name": p.name,
                "status": p.status.value,
                "question_count": p.question_count,
                "created_at": p.created_at.isoformat(),
            }
            for p in projects
        ]
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/admin/stats", dependencies=[Depends(require_admin)])
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    total_users = await session.scalar(select(func.count(User.id)))
    total_projects = await session.scalar(select(func.count(Project.id)))
    completed = await session.scalar(
        select(func.count(Project.id)).where(Project.status == ProjectStatus.COMPLETED)
    )
    pro_users = await session.scalar(
        select(func.count(User.id)).where(User.subscription_plan == SubscriptionPlan.PRO)
    )
    center_users = await session.scalar(
        select(func.count(User.id)).where(User.subscription_plan == SubscriptionPlan.CENTER)
    )

    return {
        "users": {"total": total_users, "pro": pro_users, "center": center_users},
        "projects": {"total": total_projects, "completed": completed},
    }
