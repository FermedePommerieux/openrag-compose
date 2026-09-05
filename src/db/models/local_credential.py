"""A local authentication method on an existing application user."""

from sqlmodel import Field, SQLModel


class LocalCredential(SQLModel, table=True):
    __tablename__ = "local_credentials"

    user_id: str = Field(foreign_key="users.id", primary_key=True, max_length=64)
    login: str = Field(unique=True, index=True, max_length=64)
    password_hash: str = Field(max_length=512, repr=False)
    version: int = Field(default=1)
    must_change_password: bool = Field(default=False)


class AuthSession(SQLModel, table=True):
    __tablename__ = "auth_sessions"

    id: str = Field(primary_key=True, max_length=64)
    user_id: str = Field(foreign_key="users.id", index=True, max_length=64)
    expires_at: int
    credential_version: int | None = None
