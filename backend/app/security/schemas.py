from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.security.crypto import strong_password
from app.security.policy import PERMISSIONS

JsonUUID = Annotated[UUID, Field(strict=False)]
Reason = Literal["security_response", "account_review", "role_review", "configuration_update"]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class LoginBody(StrictBody):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    password: SecretStr = Field(min_length=1, max_length=128)


class RefreshBody(StrictBody):
    refresh_token: SecretStr = Field(min_length=46, max_length=46)


class StepUpBody(StrictBody):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    reason: Reason


class PasswordBody(StepUpBody):
    new_password: SecretStr = Field(min_length=16, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        if not strong_password(value.get_secret_value()):
            raise ValueError("Password is too weak")
        return value


class CreateAdminBody(PasswordBody):
    user_id: JsonUUID
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    role_ids: list[JsonUUID] = Field(default_factory=list, max_length=10)


class RoleBody(StepUpBody):
    code: str = Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    title: str = Field(min_length=1, max_length=128)
    permissions: list[str] = Field(max_length=64)

    @field_validator("permissions")
    @classmethod
    def known_permissions(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or not set(values) <= PERMISSIONS:
            raise ValueError("Unknown or duplicate permission")
        return values


class GrantBody(StepUpBody):
    role_ids: list[JsonUUID] = Field(max_length=10)

    @field_validator("role_ids")
    @classmethod
    def unique_roles(cls, values: list[UUID]) -> list[UUID]:
        if len(values) != len(set(values)):
            raise ValueError("Duplicate role")
        return values


class StatusBody(StepUpBody):
    status: Literal["active", "blocked", "suspended", "deleted"]


class SettingBody(StepUpBody):
    enabled: bool
