from pydantic import BaseModel, Field

__all__ = (
    "CreateUserRequest",
    "ResetUserPasswordRequest",
    "JoinedRoom",
    "RoomInfoMember"
)


class CreateUserRequest(BaseModel):
    username: str
    """The unique username to use (the localpart)"""
    password: str | None = None
    """The password to use, or None to generate one"""


class ResetUserPasswordRequest(BaseModel):
    user_id: str
    """The fully qualified matrix user ID for the user to reset the password for"""
    password: str | None = None
    """The password to use, or None to generate one"""


class JoinedRoom(BaseModel):
    room_id: str
    """The room's internal ID. Starts with !"""
    members: int
    """The number of members in the room"""
    name: str
    """The room's name. Will be the room ID if unknown."""


class RoomInfoMember(BaseModel):
    user_id: str
    """The user's fully qualified matrix user ID"""
    display_name: str = Field("")
    """The user's display name, if available."""
