"""Request bodies. Unknown fields are rejected (extra="forbid"), so server-
owned fields -- id, user_id, space_id, version, seq, role, timestamps and
the lease -- can never be mass-assigned. Types are strict: no coercion of
numbers to strings or strings to numbers."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator

from .config import (
    CONVERSATION_TITLE_MAX,
    SPACE_INSTRUCTIONS_MAX,
    SPACE_NAME_MAX,
    USER_MESSAGE_MAX,
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _trimmed(value: str, maximum: int) -> str:
    value = value.strip()
    if not 1 <= len(value) <= maximum:
        raise ValueError("length")
    return value


def _instructions(value: str) -> str:
    if len(value) > SPACE_INSTRUCTIONS_MAX:
        raise ValueError("length")
    return value


def _version(value: int) -> int:
    if value < 1:
        raise ValueError("version")
    return value


class CreateSpace(_Body):
    name: StrictStr
    instructions: StrictStr = ""

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return _trimmed(value, SPACE_NAME_MAX)

    @field_validator("instructions")
    @classmethod
    def _instr(cls, value):
        return _instructions(value)


class RenameSpace(_Body):
    name: StrictStr
    expected_version: StrictInt

    @field_validator("name")
    @classmethod
    def _name(cls, value):
        return _trimmed(value, SPACE_NAME_MAX)

    @field_validator("expected_version")
    @classmethod
    def _ver(cls, value):
        return _version(value)


class SetInstructions(_Body):
    instructions: StrictStr
    expected_version: StrictInt

    @field_validator("instructions")
    @classmethod
    def _instr(cls, value):
        return _instructions(value)

    @field_validator("expected_version")
    @classmethod
    def _ver(cls, value):
        return _version(value)


class CreateConversation(_Body):
    title: StrictStr | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value):
        return None if value is None else _trimmed(value, CONVERSATION_TITLE_MAX)


class RenameConversation(_Body):
    title: StrictStr
    expected_version: StrictInt

    @field_validator("title")
    @classmethod
    def _title(cls, value):
        return _trimmed(value, CONVERSATION_TITLE_MAX)

    @field_validator("expected_version")
    @classmethod
    def _ver(cls, value):
        return _version(value)


class SendMessage(_Body):
    message: StrictStr
    client_request_id: StrictStr

    @field_validator("message")
    @classmethod
    def _message(cls, value):
        if not value.strip():
            raise ValueError("empty_message")
        if len(value) > USER_MESSAGE_MAX:
            raise ValueError("message_too_long")
        return value

    @field_validator("client_request_id")
    @classmethod
    def _request_id(cls, value):
        parsed = uuid.UUID(value)          # ValueError for anything else
        if str(parsed) != value.lower():
            raise ValueError("request_id")
        return str(parsed)


def error_code(errors) -> str:
    """The stable code for a validation failure: the two message codes the
    chat already uses, otherwise invalid_request."""
    for error in errors:
        if tuple(error.get("loc", ())) == ("message",):
            text = str(error.get("msg", ""))
            if "empty_message" in text:
                return "empty_message"
            if "message_too_long" in text:
                return "message_too_long"
    return "invalid_request"
