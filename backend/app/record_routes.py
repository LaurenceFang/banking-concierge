"""Billing organization and personal subscription records (no bank execution)."""

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field


class ManualEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=120)
    date: str
    merchant: str = Field(min_length=1, max_length=160)
    amount_minor: int = Field(strict=True, gt=0, le=9007199254740991)
    transaction_kind: Literal["expense", "income", "refund"]
    category: str = Field(default="", max_length=80)
    tags: list[str] = Field(default_factory=list, max_length=10)
    note: str = Field(default="", max_length=500)
    account_id: str | None = None


class EntryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    date: str | None = None
    merchant: str | None = Field(default=None, min_length=1, max_length=160)
    amount_minor: int | None = Field(default=None, strict=True, ge=-9007199254740991, le=9007199254740991)
    transaction_kind: Literal["expense", "income", "refund", "transfer", "opening"] | None = None
    category: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=500)


class PersonalRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=160)
    amount_minor: int = Field(strict=True, gt=0, le=9007199254740991)
    cycle: Literal["weekly", "monthly", "quarterly", "yearly"]
    next_charge_on: str
    category: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)
    reminder_enabled: bool = Field(default=True, strict=True)
    lead_days: int = Field(default=3, strict=True, ge=0, le=30)
    candidate_id: str | None = None


class PersonalRecordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    amount_minor: int | None = Field(default=None, strict=True, gt=0, le=9007199254740991)
    cycle: Literal["weekly", "monthly", "quarterly", "yearly"] | None = None
    next_charge_on: str | None = None
    category: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    reminder_enabled: bool | None = Field(default=None, strict=True)
    lead_days: int | None = Field(default=None, strict=True, ge=0, le=30)


class CandidateFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=0)
    feedback: Literal["confirmed", "not_subscription", "ignored", "reset"]


def record_router(app):
    router = APIRouter(prefix="/api")

    @router.post("/billing/manual")
    def create_manual(request: ManualEntryRequest):
        values = request.model_dump()
        values["entry_date"] = values.pop("date")
        return app.state.billing.create_manual(**values)

    @router.patch("/billing/entries/{identity}")
    def update_entry(identity: str, request: EntryUpdateRequest):
        return app.state.billing.update_entry(identity, **request.model_dump(exclude_unset=True))

    @router.delete("/billing/entries/{identity}")
    def remove_entry(identity: str, version: int = Query(ge=1)):
        return app.state.billing.remove_entry(identity, version=version)

    @router.get("/billing/import-batches")
    def import_batches():
        return {"batches": app.state.billing.list_import_batches()}

    @router.post("/billing/import-batches/{identity}/undo")
    def undo_import(identity: str):
        return app.state.billing.undo_import_batch(identity)

    @router.post("/subscription-records")
    def create_record(request: PersonalRecordRequest):
        return app.state.subscriptions.create_personal_record(
            **request.model_dump(), current_entries=app.state.billing.entries(),
        )

    @router.patch("/subscription-records/{identity}")
    def update_record(identity: str, request: PersonalRecordUpdate):
        return app.state.subscriptions.update_personal_record(identity, **request.model_dump(exclude_unset=True))

    @router.delete("/subscription-records/{identity}")
    def remove_record(identity: str, version: int = Query(ge=1)):
        return app.state.subscriptions.remove_personal_record(identity, version=version)

    @router.put("/subscription-candidates/{identity}/feedback")
    def candidate_feedback(identity: str, request: CandidateFeedbackRequest):
        return app.state.subscriptions.set_candidate_feedback(
            identity, request.feedback, app.state.billing.entries(), version=request.version,
        )

    return router
