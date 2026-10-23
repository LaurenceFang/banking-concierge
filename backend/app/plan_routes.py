"""API routes for revising an unconfirmed workflow preview."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.bank.errors import BankError


class WorkflowRevisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=80)
    quantity: int = Field(strict=True, ge=1, le=5)


class WorkflowPlanRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(
        default=None,
        max_length=160,
        description="省略时保留原名称；显式传 null 时清除自定义名称。",
    )
    recipient_label: str | None = Field(default=None, max_length=120)
    budget_minor: int | None = Field(default=None, strict=True)
    account_id: str | None = Field(default=None, max_length=64)
    execute_at: str | None = Field(
        default=None,
        max_length=80,
        description="省略时保留原时间；显式传 null 时使用当前模拟时间。",
    )
    items: list[WorkflowRevisionItem] | None = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="完整替换原服务列表；移除服务时提交修改后的完整列表。",
    )


def plan_revision_router(app, preview_domain=None) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/workflow-plans/{identity}/revise")
    async def revise_workflow_plan(identity: str, request: WorkflowPlanRevisionRequest) -> dict[str, Any]:
        try:
            changes = request.model_dump(exclude_unset=True, exclude={"conversation_id"})
            if preview_domain is not None:
                return await preview_domain(
                    request.conversation_id,
                    app.state.workflows,
                    lambda: app.state.workflows.revise_action_plan(identity, request.conversation_id, changes),
                )
            return app.state.workflows.revise_action_plan(
                identity,
                request.conversation_id,
                changes,
            )
        except BankError as error:
            raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error

    return router
