"""运营后台路由汇总（Phase 5）。"""
from fastapi import APIRouter

from . import (
    accounts,
    audit,
    auth,
    config,
    cleanup,
    dicts,
    jobs,
    logs,
    reports,
    resumes,
    recommendation_strategies,
    recommendation_metrics,
    demo,
)

router = APIRouter()
router.include_router(auth.router)
router.include_router(audit.router)
router.include_router(accounts.router)
router.include_router(jobs.router)
router.include_router(resumes.router)
router.include_router(dicts.router)
router.include_router(config.router)
router.include_router(cleanup.router)
router.include_router(reports.router)
router.include_router(logs.router)
router.include_router(recommendation_strategies.router)
router.include_router(recommendation_metrics.router)
router.include_router(demo.router)
