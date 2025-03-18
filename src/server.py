import asyncio
import collections
import contextlib
import logging
import os
import random
import re
import typing
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Depends, HTTPException, Body, APIRouter
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware

from .models import *

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER")
assert MATRIX_HOMESERVER is not None, "Please set the $MATRIX_HOMESERVER environment variable."
ADMIN_ROOM_ID = os.getenv("ADMIN_ROOM_ID")
assert ADMIN_ROOM_ID is not None, "Please set the $ADMIN_ROOM_ID environment variable."
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
assert ACCESS_TOKEN is not None, "Please set the $ACCESS_TOKEN environment variable."
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

session = httpx.AsyncClient(base_url=MATRIX_HOMESERVER, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
security = HTTPBearer()


async def is_admin(credentials: HTTPAuthorizationCredentials = Depends(security)) -> bool:
    """Checks if the user is an admin."""
    try:
        response = await session.get(
            f"/_matrix/client/v3/account/whoami",
            headers={"Authorization": f"Bearer {credentials.credentials}"},
            timeout=None
        )
        response.raise_for_status()
        data = response.json()
        response = await session.get(
            f"/_matrix/client/v3/rooms/{ADMIN_ROOM_ID}/joined_members",
            headers={"Authorization": f"Bearer {credentials.credentials}"}
        )
        response.raise_for_status()
        return data["user_id"] in response.json().keys()
    except httpx.HTTPError:
        raise HTTPException(401, detail="Invalid access token.")


@contextlib.asynccontextmanager
async def lifecycle(_):
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("httpcore.http11").setLevel(logging.INFO)
    logging.getLogger("httpcore.connection").setLevel(logging.INFO)
    async with asyncio.TaskGroup() as tg:
        _task = tg.create_task(sync_task_function(), name="syncer")
        yield
        _task.cancel()

app = FastAPI(
    dependencies=[Depends(is_admin, use_cache=True)],
    lifespan=lifecycle,
    title="conduwuit Admin API Proxy",
    summary="A proxy server that translates API calls into admin commands for conduwuit.",
    license_info={
        "name": "AGPL-3.0",
        "url": "https://www.gnu.org/licenses/agpl-3.0.html"
    }
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
log = logging.getLogger(__name__)
pending_events: dict[str, asyncio.Event] = {}
event_cache = collections.deque(maxlen=1000)

conduwuit_router = APIRouter(
    prefix="/_conduwuit/admin",
)


async def sync_task_function() -> None:
    since = None
    failures = 0
    while True:
        query = {"timeout": 60000 if since else 0}
        if since is not None:
            query["since"] = since
        log.debug("Syncing with query: %s", urlencode(query))
        response = await session.get(f"/_matrix/client/v3/sync", params=query, timeout=None)
        if response.status_code != 200:
            failures += 1
            if failures >= 5:
                log.error("Failed to sync 5 times in a row. Will re-start with an initial sync.")
                failures = 0
                since = None
                continue
            else:
                sleep_time = 2 ** failures + random.uniform(0, 1)
                sleep_time = min(60, max(1, sleep_time))
                log.warning(
                    "Failed to sync: HTTP %d %s: %r. Will retry in %.2f seconds (attempt %d/5).",
                    response.status_code,
                    response.reason_phrase,
                    response.text
                )
                await asyncio.sleep(sleep_time)
                continue
        failures = 0
        data = response.json()
        nb = data.get("next_batch")
        if not nb:
            # A soft failure really
            log.warning("There was no next_batch in the sync response. Will re-try with the same token.")
            continue
        since = nb
        data.setdefault("rooms", {})
        data["rooms"].setdefault("join", {})

        for room_id, room_data in data["rooms"]["join"].items():
            if room_id != ADMIN_ROOM_ID:
                continue  # we don't care about rooms other than the admin one.
            timeline_events: list[dict[str, typing.Any]] = room_data.get("timeline", {}).get("events", [])
            for event in timeline_events:
                event_cache.append(event)
                reply = event.get("content", {}).get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id")
                if reply in pending_events:
                    log.info("Received event %s that was pending. Setting the event.", reply)
                    pending_events[reply].set()
                    del pending_events[reply]

        for event in event_cache:
            reply = event.get("content", {}).get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id")
            if reply in pending_events:
                log.info("Received event %s that was pending. Setting the event.", reply)
                pending_events[reply].set()
                del pending_events[reply]


async def send_and_wait(
        message: str,
        access_token: str = None
) -> dict[str, typing.Any]:
    """
    Sends an admin command and waits for the reply.

    :param message: The message text to send
    :param access_token: The access token to use, or None to use the default one.
    :return: The coroutine to await.
    """
    headers = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    txn_id = os.urandom(16).hex()
    r = await session.put(
        f"/_matrix/client/v3/rooms/{ADMIN_ROOM_ID}/send/m.room.message/{txn_id}",
        json={
            "msgtype": "m.text",
            "body": message,
        },
        headers=headers
    )
    r.raise_for_status()
    event_id = r.json()["event_id"]
    event = asyncio.Event()
    pending_events[event_id] = event
    log.debug("Waiting for event %s to be received.", event_id)
    await event.wait()
    for event in event_cache:
        reply = event["content"].get("m.relates_to", {}).get("m.in_reply_to", {}).get("event_id")
        if reply == event_id:
            return event
    raise ValueError("Event not found in cache, even though it was received.")


@conduwuit_router.post("/users/create", tags=["Users"])
async def create_user(payload: CreateUserRequest) -> CreateUserRequest:
    """
    Creates a new user.

    The response will match the input, unless you did not specify a password, in which case it will be generated.
    """
    return payload


@conduwuit_router.post("/users/reset-password", tags=["Users"])
async def reset_password(payload: ResetUserPasswordRequest) -> ResetUserPasswordRequest:
    """
    Resets the password of a user.

    The response will match the input, unless you did not specify a password, in which case it will be generated.

    Note:

        Resetting the password of a user that does not exist will implicitly create the user.
    """
    command_parts = ["!admin", "users", "reset-password", payload.user_id]
    if payload.password is not None:
        command_parts.append(payload.password)

    event = await send_and_wait(" ".join(command_parts))
    content = event["content"]["body"]
    line = content.splitlines()[0]
    if line.starswith("Command failed with error:"):
        for line in content.splitlines():
            if payload.user_id in line:  # this was our error
                if line.endswith("does not belong to our server."):
                    raise HTTPException(
                        400,
                        detail="Attempted to reset a user on another server."
                    )
        raise HTTPException(500, detail="Unknown response error:\n" + content)

    if not line.starswith("Successfully reset the password for user"):
        raise ValueError(f"Unexpected response: {content!r}")
    response_password = line.split()[-1][1:-1]  # Remove the backticks
    return ResetUserPasswordRequest(user_id=payload.user_id, password=response_password)


@conduwuit_router.get("/users/list", tags=["Users"])
async def list_users() -> list[str]:
    """
    Lists all users on the server.

    This will return a list of fully qualified user IDs.
    """
    event = await send_and_wait("!admin users list-users")
    content = event["content"]["body"]
    lines = content.splitlines()
    users = []
    for line in lines:
        if line.startswith("@"):
            users.append(line.strip())
    return users


@conduwuit_router.delete("/users/{user_id}", status_code=204, tags=["Users"])
async def deactivate_user(user_id: str, leave_rooms: bool = True) -> None:
    """
    Deactivates a user's account.

    If `leave_rooms` is False, the user will not leave all rooms during deactivation. It defaults to True.

    Note:

        Deactivating a user that does not exist will implicitly create them.

        You can re-activate a user by resetting their password.
    """

    command_parts = ["!admin", "users", "deactivate", user_id]
    if leave_rooms is False:
        command_parts.insert(3, "--no-leave-rooms")
    event = await send_and_wait(" ".join(command_parts))
    content = event["content"]["body"]
    if "does not belong to our server" in content:
        raise HTTPException(400, detail="Attempted to deactivate a user on another server.")
    if not content.endswith("has been deactivated"):
        raise ValueError(f"Unexpected response: {content!r}")

@conduwuit_router.post("/users/bulk-deactivate", tags=["Users"], status_code=204)
async def bulk_deactivate_users(
        user_ids: typing.Annotated[list[str], Body(...)],
        leave_rooms: bool = True,
        force: bool = False
) -> None:
    """
    Deactivates multiple users at once. The payload body should be an array of fully qualified user IDs.

    If `leave_rooms` is False, the users will not leave all rooms during deactivation. It defaults to True.

    If `force` is True, admin accounts will also be deactivated and will assume leave all rooms too.

    Note:

        You can re-activate a user by resetting their password.
    """
    if not user_ids:
        raise HTTPException(400, detail="No user IDs were provided.")
    lines = [
        "!admin users deactivate-all",
        "```"
    ]
    if force is True:
        lines[0] += " --force"
    if leave_rooms is False:
        lines[0] += " --no-leave-rooms"

    lines.extend(user_ids)
    lines.append("```")
    event = await send_and_wait("\n".join(lines))
    content = event["content"]["body"]
    if content == "Deactivated 0 accounts.":
        raise HTTPException(400, detail="No accounts were deactivated.")

@conduwuit_router.get("/users/{user_id}/rooms", tags=["Users"])
async def get_user_joined_rooms(user_id: str) -> list[JoinedRoom]:
    """Returns a list of rooms that the user is in."""
    command = ["!admin", "users", "list-joined-rooms", user_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if "does not belong to our server." in content:
        raise HTTPException(400, detail="Attempted to list rooms for a user on another server.")
    elif content == "User is not in any rooms.":
        return []

    rooms = []
    for line in content.splitlines():
        if not line.startswith("!"):
            continue
        room_id, members_part, name_part = line.split("\t", 2)
        try:
            members = int(members_part.split()[1])
        except ValueError:
            members = 0
        try:
            name = name_part.split(" ", 1)[1]
        except IndexError:
            name = room_id
        rooms.append(JoinedRoom(room_id=room_id, members=members, name=name))
    return rooms


@conduwuit_router.post("/users/{user_id}/rooms/{room_id}", tags=["Users"], status_code=204)
async def force_user_join_room(user_id: str, room_id: str) -> None:
    """Forces a user to join a room."""
    command = ["!admin", "users", "force-join-room", user_id, room_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if "does not belong to our server." in content:
        raise HTTPException(400, detail="Attempted to force join a user on another server.")
    elif "has been joined to" in content:
        return
    elif content.startswith("Command failed with error:"):
        raise HTTPException(400, detail=content)
    else:
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.delete("/users/{user_id}/rooms/{room_id}", tags=["Users"], status_code=204)
async def force_user_leave_room(user_id: str, room_id: str) -> None:
    """Forces a user to leave a room."""
    command = ["!admin", "users", "force-leave-room", user_id, room_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if "does not belong to our server." in content:
        raise HTTPException(400, detail="Attempted to force leave a user on another server.")
    elif "has been left from" in content:
        return
    elif content.startswith("Command failed with error:"):
        raise HTTPException(400, detail=content)
    else:
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.post("/users/{user_id}/rooms/{room_id}/demote", tags=["Users"], status_code=204)
async def force_user_demote(user_id: str, room_id: str) -> None:
    """
    Forces a user to demote themselves to the room's default power level in the given room, permitted they're able to.
    """
    command = ["!admin", "users", "force-demote", user_id, room_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if "does not belong to our server." in content:
        raise HTTPException(400, detail="Attempted to demote a user on another server.")
    elif content == "User is not allowed to modify their own power levels in the room.":
        raise HTTPException(403, detail="User is not allowed to modify their own power levels in the room.")
    elif "demoted themselves to the room default power level in" in content:
        return
    elif content.startswith("Command failed with error:"):
        raise HTTPException(400, detail=content)
    else:
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.post("/users/{user_id}/make-admin", tags=["Users"], status_code=204)
async def force_user_make_admin(user_id: str) -> None:
    """Make a user a server administrator."""
    command = ["!admin", "users", "make-user-admin", user_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if "does not belong to our server." in content:
        raise HTTPException(400, detail="Attempted to make an admin on another server.")
    elif "has been made an admin." in content:
        return
    elif content.startswith("Command failed with error:"):
        raise HTTPException(400, detail=content)
    else:
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.delete("/users/{event_id}", tags=["Users"], status_code=204)
async def force_redact_event(event_id: str) -> None:
    """
    Attempts to forcefully redact the specified event ID from the author.

    This will only work for local users.
    """
    command = ["!admin", "users", "redact-event", event_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content == "This command only works on local users.":
        raise HTTPException(400, detail="Attempted to redact an event from another server.")
    elif content == "Event does not exist in our database.":
        raise HTTPException(404, detail="Event does not exist in our database.")
    elif "M_FORBIDDEN: Event is not authorized." in content:
        raise HTTPException(403, detail="The event cannot be redacted by the author.")
    elif content.startswith("Command failed with error:"):
        raise HTTPException(400, detail=content)
    elif content.startswith("Successfully redacted event"):
        return
    else:
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.post("/users/bulk-force-join", tags=["Users"])
async def bulk_force_join_users(
        user_ids: typing.Annotated[list[str], Body(...)],
        room_id: str
) -> tuple[int, int]:
    """
    Forces multiple users to join a room at once.
    At least 1 server admin must be in the room to reduce abuse.

    The payload body should be an array of fully qualified user IDs.

    This returns a pair of [successful joins, failed joins].
    """
    if not user_ids:
        raise HTTPException(400, detail="No user IDs were provided.")
    command = ["!admin", "users", "force-join-room", room_id]
    command.extend(user_ids)
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    parts = content.split()
    try:
        count = int(parts[-5])
    except ValueError:
        count = 2 ** 53 - 1
    try:
        failed = int(parts[-3])
    except ValueError:
        failed = 2 ** 53 - 1
    return count, failed


@conduwuit_router.post("/users/bulk-force-join/all", tags=["Users"])
async def bulk_force_join_all_users(
        room_id: str
) -> tuple[int, int]:
    """
    Forces all local users to join a room at once.
    At least 1 server admin must be in the room to reduce abuse.

    This returns a pair of [successful joins, failed joins].
    """

    event = await send_and_wait(f"!admin users force-join-all {room_id}")
    content = event["content"]["body"]
    parts = content.split()
    try:
        count = int(parts[-5])
    except ValueError:
        count = 2 ** 53 - 1
    try:
        failed = int(parts[-3])
    except ValueError:
        failed = 2 ** 53 - 1
    return count, failed


# Rooms
@conduwuit_router.get("/rooms/list", tags=["Rooms"])
async def get_all_rooms(
        page: int = 0,
        exclude_disabled: bool = False,
        exclude_banned: bool = False
) -> list[JoinedRoom]:
    """Fetches all rooms that the server knows about

    If `exclude_disabled` is True, rooms that have federation disabled will not be included.
    Likewise, if `exclude_banned` is True, rooms that have been banned will not be included.

    If the `page` parameter is set, it will return a paginated list of rooms.
    Returns an empty array if no more rooms are found.
    """
    command = ["!admin", "rooms", "list-rooms"]
    if exclude_disabled:
        command.append("--exclude-disabled")
    if exclude_banned:
        command.append("--exclude-banned")
    if page:
        command.append(str(page))

    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content == "No rooms found.":
        return []
    rooms = []
    for line in content.splitlines():
        if not line.startswith("!"):
            continue
        room_id, members_part, name_part = line.split("\t", 2)
        try:
            members = int(members_part.split()[1])
        except ValueError:
            members = 0
        try:
            name = name_part.split(" ", 1)[1]
        except IndexError:
            name = room_id
        rooms.append(JoinedRoom(room_id=room_id, members=members, name=name))

    return rooms


@conduwuit_router.get("/rooms/banned", tags=["Rooms"])
async def get_banned_rooms() -> list[JoinedRoom]:
    """Fetches all rooms that the server knows about

    If `exclude_disabled` is True, rooms that have federation disabled will not be included.
    Likewise, if `exclude_banned` is True, rooms that have been banned will not be included.

    If the `page` parameter is set, it will return a paginated list of rooms.
    Returns an empty array if no more rooms are found.
    """
    command = ["!admin", "rooms", "moderation", "list-banned-rooms"]

    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content == "No rooms found.":
        return []
    rooms = []
    for line in content.splitlines():
        if not line.startswith("!"):
            continue
        room_id, members_part, name_part = line.split("\t", 2)
        try:
            members = int(members_part.split()[1])
        except ValueError:
            members = 0
        try:
            name = name_part.split(" ", 1)[1]
        except IndexError:
            name = room_id
        rooms.append(JoinedRoom(room_id=room_id, members=members, name=name))

    return rooms


@conduwuit_router.get("/rooms/{room_id}/members", tags=["Rooms"])
async def get_room_members(room_id: str) -> list[RoomInfoMember]:
    """Fetches a list of joined members in a room."""
    command = ["!admin", "rooms", "info", "list-joined-members", room_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content == "No members found.":
        return []
    users = []
    for line in content.splitlines():
        if line.startswith("@"):
            try:
                user_id, display_name = line.split(" | ", 1)
            except ValueError:
                user_id = line
                display_name = "ERR_UNABLE_TO_PARSE"
            users.append(RoomInfoMember(user_id=user_id, display_name=display_name))
    return users


@conduwuit_router.get("/rooms/{room_id}/topic", tags=["Rooms"])
async def get_room_topic(room_id: str) -> str:
    """Fetches the topic of a room."""
    command = ["!admin", "rooms", "info", "view-room-topic", room_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content == "Room does not have a room topic set.":
        return ""
    return "\n".join("\n".splitlines()[2:-1])


@conduwuit_router.post("/rooms/{room_id}/ban", tags=["Room Moderation"], status_code=204)
async def ban_room(
        room_id: str,
        force: bool = False,
        disable_federation: bool = False
) -> None:
    """
    Bans a room from the server, evacuating all local users, and preventing further joins.

    By default, admins are not evacuated. If `force` is True, they will be.
    `force` can also be used to ignore any potential errors.

    If `disable_federation` is True, the room will be banned from federation too.
    """
    command = ["!admin", "rooms", "moderation", "ban-room", room_id]
    if force:
        command.insert(4, "--force")
    if disable_federation:
        command.insert(4, "--disable-federation")

    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if not content.startswith("Room banned"):
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.post("/rooms/{room_id}/unban", tags=["Room Moderation"], status_code=204)
async def unban_room(room_id: str, enable_federation: bool = False) -> None:
    """Unbans a room from the server, optionally re-enabling federation."""
    command = ["!admin", "rooms", "moderation", "unban-room", room_id]
    if enable_federation:
        command.insert(4, "--enable-federation")
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if not content.startswith("Room unbanned"):
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.post("/rooms/bulk-ban", tags=["Room Moderation"], status_code=204)
async def bulk_ban_rooms(
        room_ids: typing.Annotated[list[str], Body(...)],
        force: bool = False,
        disable_federation: bool = False
) -> None:
    """
    Bans multiple rooms from the server.

    By default, admins are not evacuated. If `force` is True, they will be.
    `force` can also be used to ignore any potential errors.

    If `disable_federation` is True, the rooms will be banned from federation too.
    """
    if not room_ids:
        raise HTTPException(400, detail="No room IDs were provided.")
    command = ["!admin", "rooms", "moderation", "ban-rooms"]
    if force:
        command.insert(4, "--force")
    if disable_federation:
        command.insert(4, "--disable-federation")
    command.extend(room_ids)
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if not content.startswith("Banned"):
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.post("/rooms/directory/publish/{room_id}", tags=["Room Directory"], status_code=204)
async def publish_room_to_directory(room_id: str) -> None:
    """Publishes a room to the public directory."""
    command = ["!admin", "rooms", "directory", "publish", room_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if not content.startswith("Room published"):
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.delete("/rooms/directory/unpublish/{room_id}", tags=["Room Directory"], status_code=204)
async def unpublish_room_from_directory(room_id: str) -> None:
    """Unpublishes a room from the public directory."""
    command = ["!admin", "rooms", "directory", "unpublish", room_id]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if not content.startswith("Room unpublished"):
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.get("/rooms/directory/list", tags=["Room Directory"])
async def list_rooms_in_directory(page: int = 1) -> list[JoinedRoom]:
    """Lists all rooms in the public directory."""
    command = ["!admin", "rooms", "directory", "list"]
    if page:
        command.append(str(page))
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content == "No rooms found in the directory.":
        return []
    rooms = []
    for line in content.splitlines():
        if not line.startswith("!"):
            continue
        room_id, members, name = line.split(" | ", 2)
        try:
            members = int(members.split()[1])
        except ValueError:
            members = -1
        rooms.append(JoinedRoom(room_id=room_id, members=members, name=name))
    return rooms


@conduwuit_router.post("/rooms/aliases/set", tags=["Room Aliases"])
async def set_room_alias(
        room_id: str,
        alias_name: str,
        force: bool = False
) -> None:
    """
    Sets an alias for a room.
    `alias_name` should be the name part of the alias, without the leading `#`, or server name.

    If `force` is True, the alias will be assigned to the room even if it's already in use.
    """
    command = ["!admin", "rooms", "alias", "set", room_id, alias_name]
    if force:
        command.insert(4, "--force")

    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content.startswith("Refusing to overwrite"):
        raise HTTPException(409, detail="Alias already in use.")
    elif content != "Successfully set alias":
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.delete("/rooms/aliases/{alias_name}", tags=["Room Aliases"], status_code=204)
async def remove_room_alias(alias_name: str) -> None:
    """Removes an alias from the server."""
    command = ["!admin", "rooms", "alias", "remove", alias_name]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content != "Successfully removed alias":
        raise ValueError(f"Unexpected response: {content!r}")


@conduwuit_router.get("/rooms/aliases/{alias_name}", tags=["Room Aliases"])
async def get_room_id_from_alias(alias_name: str) -> str:
    """Fetches the room ID from an alias."""
    command = ["!admin", "rooms", "alias", "which", alias_name]
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content.startswith("Alias isn't in use"):
        raise HTTPException(404, detail="Alias not found.")
    return content


@conduwuit_router.get("/rooms/aliases", tags=["Room Aliases"])
async def get_all_aliases(room_id: str = None) -> dict[str, str]:
    """
    Get all aliases in use.

    - room_id: only list the aliases for this room

    Returns an Object of {alias_name: room_id}
    """
    filter_room_id = room_id
    command = ["!admin", "rooms", "alias", "list"]
    if room_id:
        command.append(room_id)
    event = await send_and_wait(" ".join(command))
    content = event["content"]["body"]
    if content == "No aliases found.":
        return {}

    rooms = {}
    for line in content.splitlines():
        if not line.startswith("- "):
            continue
        _match = re.match(r"- `(?P<room_id>![^:]+:[^`]+)` -> #(?P<alias_name>[^:]+)\S+", line)
        if not _match:
            continue
        room_id = _match.group("room_id")
        if filter_room_id and room_id != filter_room_id:
            continue
        alias_name = _match.group("alias_name")
        rooms[alias_name] = room_id
    return rooms


from .synapse_compat import router as synapse_compatibility_router

app.include_router(conduwuit_router)
app.include_router(synapse_compatibility_router)
