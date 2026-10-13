"""Pin the same persistent conversation across global and scoped views."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .conversations import ConversationError


class PinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: bool = Field(strict=True)


def conversation_pin_router(app):
    router = APIRouter(prefix="/api/conversations")

    @router.patch("/{identity}/pin")
    def pin(identity: str, request: PinRequest):
        try:
            return app.state.store.set_pinned(identity, request.pinned)
        except ConversationError as error:
            if error.code in {"CANNOT_PIN_MAIN", "INVALID_PIN"}:
                raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error
            raise

    return router
