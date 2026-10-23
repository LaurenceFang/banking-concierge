"""API for explicit personal preferences that affect billing classification."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .billing.service import BillingError

CategoryKind = Literal["expense", "refund", "income", "transfer", "opening"]


class CategoryRuleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=120)
    merchant: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    effective_from: str
    transaction_kind: CategoryKind | None = None
    note_contains: str | None = Field(default=None, max_length=500)


class CategoryRuleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(strict=True, ge=1)
    merchant: str | None = Field(default=None, min_length=1, max_length=160)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    effective_from: str | None = None
    transaction_kind: CategoryKind | None = None
    note_contains: str | None = Field(default=None, max_length=500)


def _raise_category_rule_http(error: BillingError) -> None:
    if error.code == "CATEGORY_RULE_NOT_FOUND":
        status_code = 404
    elif error.code in {"VERSION_CONFLICT", "REQUEST_ID_REUSED", "CATEGORY_RULE_EXISTS"}:
        status_code = 409
    else:
        status_code = 422
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.code, "message": error.message},
    ) from error


def preference_router(app):
    """Register the minimal CRUD API used by the agent or a future settings view."""
    router = APIRouter(prefix="/api/billing/category-rules")

    @router.get("")
    def list_category_rules():
        return {"rules": app.state.billing.list_category_rules()}

    @router.post("")
    def create_category_rule(request: CategoryRuleCreateRequest):
        try:
            return app.state.billing.create_category_rule(**request.model_dump())
        except BillingError as error:
            _raise_category_rule_http(error)

    @router.patch("/{identity}")
    def update_category_rule(identity: str, request: CategoryRuleUpdateRequest):
        values = request.model_dump(exclude_unset=True)
        version = values.pop("version")
        try:
            return app.state.billing.update_category_rule(identity, version, **values)
        except BillingError as error:
            _raise_category_rule_http(error)

    @router.delete("/{identity}")
    def delete_category_rule(identity: str, version: int = Query(ge=1)):
        try:
            return app.state.billing.delete_category_rule(identity, version)
        except BillingError as error:
            _raise_category_rule_http(error)

    return router
