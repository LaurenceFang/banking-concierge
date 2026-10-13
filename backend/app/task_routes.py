"""HTTP entry points for explicitly confirmed demo tasks."""
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["create_aa", "cancel_aa", "schedule_transfer", "cancel_schedule"]
    conversation_id: str
    title: str | None = Field(default=None, max_length=120)
    account_id: str | None = None
    total_minor: int | None = Field(default=None, strict=True)
    participant_ids: list[str] | None = None
    recipient_id: str | None = None
    amount_minor: int | None = Field(default=None, strict=True)
    execute_at: str | None = None
    task_id: str | None = None


class WorkflowItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    quantity: int = Field(strict=True, ge=1, le=5)


class WorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["arrange", "cancel"]
    conversation_id: str
    account_id: str | None = None
    recipient_label: str | None = Field(default=None, max_length=120)
    budget_minor: int | None = Field(default=None, strict=True)
    items: list[WorkflowItem] | None = None
    execute_at: str | None = None
    task_id: str | None = None


class Decision(BaseModel):
    decision: Literal["approve", "decline"]
    conversation_id: str | None = None


class PaymentRequest(BaseModel):
    participant_id: str


class AdvanceRequest(BaseModel):
    minutes: int = Field(strict=True, ge=1, le=525600)


def task_router(app, preview_domain, decide_domain):
    router = APIRouter(prefix="/api")

    @router.get("/tasks")
    def tasks():
        app.state.sync_events()
        return {"aa": app.state.tasks.list_aa(), "schedules": app.state.tasks.list_schedules(),
                "now": app.state.task_clock.now().isoformat()}

    @router.get("/tasks/clock")
    def clock():
        return {"now": app.state.task_clock.now().isoformat(), "timezone": "Asia/Shanghai"}

    @router.post("/tasks/preview")
    async def preview_task(request: TaskRequest):
        return await preview_domain(request.conversation_id, app.state.tasks,
                                    lambda: app.state.tasks.create_action_plan(**request.model_dump()))

    @router.post("/task-plans/{identity}/decision")
    def task_decision(identity: str, request: Decision):
        return decide_domain(identity, app.state.tasks, request)

    @router.post("/tasks/{identity}/demo-payment")
    def payment(identity: str, request: PaymentRequest):
        result = app.state.tasks.simulate_payment(identity, request.participant_id)
        app.state.sync_events()
        return result

    @router.post("/tasks/demo/advance")
    def advance(request: AdvanceRequest):
        app.state.task_clock.advance(request.minutes)
        # Drive enough persisted steps to make the short demo observable immediately.
        for _ in range(8):
            app.state.task_tick()
        return {"now": app.state.task_clock.now().isoformat(), "timezone": "Asia/Shanghai"}

    @router.get("/workflows")
    def workflows():
        app.state.sync_events()
        return {"catalog": app.state.workflows.catalog(), "tasks": app.state.workflows.list_tasks(),
                "now": app.state.task_clock.now().isoformat()}

    @router.post("/workflows/preview")
    async def preview_workflow(request: WorkflowRequest):
        return await preview_domain(request.conversation_id, app.state.workflows,
                                    lambda: app.state.workflows.create_action_plan(**request.model_dump()))

    @router.post("/workflow-plans/{identity}/decision")
    def workflow_decision(identity: str, request: Decision):
        return decide_domain(identity, app.state.workflows, request)

    @router.post("/workflows/{identity}/recover")
    def recover(identity: str):
        result = app.state.workflows.recover(identity)
        app.state.sync_events()
        return result

    return router
