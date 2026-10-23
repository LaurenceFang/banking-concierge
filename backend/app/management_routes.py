"""User-managed contacts and simulated service catalogs."""

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=120)
    bank_name: str | None = Field(default=None, max_length=120)
    account_last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    client_request_id: str = Field(min_length=1, max_length=120)


class ContactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=120)
    bank_name: str | None = Field(default=None, max_length=120)
    account_last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")


class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    merchant_name: str = Field(min_length=1, max_length=60)
    price_minor: int = Field(strict=True, gt=0, le=9007199254740991)
    unit: str = Field(default="份", min_length=1, max_length=12)
    description: str = Field(default="", max_length=240)
    cancel_supported: bool = Field(default=True, strict=True)
    client_request_id: str = Field(min_length=1, max_length=120)


class ServiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(strict=True, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=60)
    merchant_name: str | None = Field(default=None, min_length=1, max_length=60)
    price_minor: int | None = Field(default=None, strict=True, gt=0, le=9007199254740991)
    unit: str | None = Field(default=None, min_length=1, max_length=12)
    description: str | None = Field(default=None, max_length=240)
    cancel_supported: bool | None = Field(default=None, strict=True)


def management_router(app, *, workflows_enabled: bool):
    router = APIRouter(prefix="/api")

    @router.get("/contacts")
    def contacts(query: str = Query(default="", max_length=120)):
        return {"contacts": app.state.bank.list_contacts(query)}

    @router.post("/contacts")
    def create_contact(request: ContactCreate):
        return app.state.bank.create_contact(**request.model_dump())

    @router.patch("/contacts/{identity}")
    def update_contact(identity: str, request: ContactUpdate):
        return app.state.bank.update_contact(identity, **request.model_dump(exclude_unset=True))

    @router.delete("/contacts/{identity}")
    def remove_contact(identity: str, version: int = Query(ge=1)):
        result = app.state.bank.remove_contact(identity, version=version)
        return {**result, "removed": True, "pending_tasks": result["pending_schedule_count"]}

    if workflows_enabled:
        @router.get("/workflow-services")
        def services(query: str = Query(default="", max_length=120)):
            return {"services": app.state.workflows.list_services(query)}

        @router.post("/workflow-services")
        def create_service(request: ServiceCreate):
            return app.state.workflows.create_service(**request.model_dump())

        @router.patch("/workflow-services/{identity}")
        def update_service(identity: str, request: ServiceUpdate):
            return app.state.workflows.update_service(identity, **request.model_dump(exclude_unset=True))

        @router.delete("/workflow-services/{identity}")
        def remove_service(identity: str, version: int = Query(ge=1)):
            item = app.state.workflows.remove_service(identity, version=version)
            pending = sum(
                task["status"] in {"scheduled", "running", "unknown"}
                and any(step["product_id"] == identity for step in task["steps"])
                for task in app.state.workflows.list_tasks()
            )
            return {"service": item, "removed": True, "pending_tasks": pending}

    return router
