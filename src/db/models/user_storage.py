"""Filesystem locators attached to existing principals, never document ACLs."""

from sqlmodel import Field, SQLModel


class UserStorage(SQLModel, table=True):
    __tablename__ = "user_storage"

    user_id: str = Field(foreign_key="users.id", primary_key=True, max_length=64)
    directory: str = Field(unique=True, max_length=64)


class SourceArchiveLocation(SQLModel, table=True):
    __tablename__ = "source_archive_locations"

    source_id: str = Field(primary_key=True, max_length=161)
    user_id: str = Field(foreign_key="user_storage.user_id", index=True, max_length=64)
