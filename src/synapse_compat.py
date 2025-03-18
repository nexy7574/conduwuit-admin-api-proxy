import asyncio
import re
import typing

import httpx
from fastapi import APIRouter, HTTPException, Body
from fastapi.params import Query
from fastapi.responses import JSONResponse

from src.models import SynapsePutUser

router = APIRouter(
    prefix="/_synapse/admin",
    tags=["Synapse Compatibility"]
)
ROOM_ORDER_BY_REGEX = (
    r"^(?:name|canonical_alias|joined_members)$"
)


@router.get("/v2/users")
@router.get("/v3/users")
async def get_users():
    from .server import session, list_users, ADMIN_ROOM_ID, ACCESS_TOKEN
    user_ids = await list_users()
    if not user_ids:
        return {}
    admin_room_members = await session.get(
        "/_matrix/client/v3/rooms/{}/joined_members".format(ADMIN_ROOM_ID),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}"
        }
    )
    if admin_room_members.status_code != 200:
        raise HTTPException(
            status_code=admin_room_members.status_code,
            detail=admin_room_members.text
        )
    admin_room_members = admin_room_members.json()
    results = []

    tasks: dict[str, dict[str, asyncio.Task[httpx.Response]]] = {}
    _tasks = []
    for user_id in user_ids:
        display_name = admin_room_members.get(user_id, {}).get("displayname", user_id)
        avatar_url = admin_room_members.get(user_id, {}).get("avatar_url")
        if user_id in admin_room_members:
            is_admin = True
        else:
            display_name_task = asyncio.create_task(
                    session.get(
                    "/_matrix/client/v3/profile/{}/displayname".format(user_id),
                    headers={
                        "Authorization": f"Bearer {ACCESS_TOKEN}"
                    }
                )
            )
            avatar_url_task = asyncio.create_task(
                session.get(
                    "/_matrix/client/v3/profile/{}/avatar_url".format(user_id),
                    headers={
                        "Authorization": f"Bearer {ACCESS_TOKEN}"
                    }
                )
            )
            _tasks += [display_name_task, avatar_url_task]
            is_admin = False
            tasks[user_id] = {
                "displayname": display_name_task,
                "avatar_url": avatar_url_task
            }
        results.append(
            {
                "name": user_id,
                "user_type": None,
                "is_guest": False,
                "admin": is_admin,
                "deactivated": False,
                "shadow_banned": False,
                "display_name": display_name,
                "avatar_url": avatar_url,
                "creation_ts": 0,
                "approved": True,
                "erased": False,
                "last_seen_ts": 0,
                "locked": False,
                "user_id": user_id
            }
        )

    await asyncio.gather(*_tasks, return_exceptions=True)
    for user_id, task in tasks.items():
        for task_name, task_obj in task.items():
            # noinspection PyBroadException
            try:
                result = task_obj.result()
            except Exception:
                continue
            if task_name == "displayname":
                result = (result.json().get("displayname") or user_id) if result.status_code == 200 else user_id
            else:
                result = result.json().get("avatar_url") if result.status_code == 200 else None
            for user in results:
                if user["user_id"] == user_id:
                    user["display_name"] = result if task_name == "displayname" else user["display_name"]
                    user["avatar_url"] = result if task_name == "avatar_url" else user["avatar_url"]
                    break
    return {
        "users": results,
        "total": len(results)
    }

@router.get("/v2/users/{user_id}")
async def get_user(user_id: str):
    """https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#query-user-account"""
    from .server import session, ADMIN_ROOM_ID, ACCESS_TOKEN
    admin_room_members = await session.get(
        "/_matrix/client/v3/rooms/{}/joined_members".format(ADMIN_ROOM_ID),
        headers={
            "Authorization": f"Bearer {ACCESS_TOKEN}"
        }
    )
    if admin_room_members.status_code != 200:
        raise HTTPException(
            status_code=admin_room_members.status_code,
            detail=admin_room_members.text
        )
    admin_room_members = admin_room_members.json()
    if user_id in admin_room_members:
        display_name = admin_room_members[user_id].get("displayname", user_id)
        avatar_url = admin_room_members[user_id].get("avatar_url")
        is_admin = True
    else:
        display_name = await session.get(
            "/_matrix/client/v3/profile/{}/displayname".format(user_id),
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}"
            }
        )
        display_name = (display_name.json().get("displayname") or user_id) if display_name.status_code == 200 else user_id
        avatar_url = await session.get(
            "/_matrix/client/v3/profile/{}/avatar_url".format(user_id),
            headers={
                "Authorization": f"Bearer {ACCESS_TOKEN}"
            }
        )
        is_admin = False
        avatar_url = avatar_url.json().get("avatar_url") if avatar_url.status_code == 200 else None
    return {
        "name": user_id,
        "user_type": None,
        "is_guest": False,
        "admin": is_admin,
        "deactivated": False,
        "shadow_banned": False,
        "display_name": display_name,
        "avatar_url": avatar_url,
        "creation_ts": 0,
        "approved": True,
        "erased": False,
        "last_seen_ts": 0,
        "locked": False
    }


@router.put("/v2/users/{user_id}")
async def create_or_update_user(res: JSONResponse, body: SynapsePutUser, user_id: str):
    """https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#create-or-modify-account"""
    from .server import deactivate_user, reset_password
    res.status_code = 200
    res_body = {}
    if body.password is not None:
        _r = await reset_password(user_id, body.password)
        res_body["password_changed"] = _r
    if body.deactivated is True:
        _r = await deactivate_user(user_id, body.deactivated)
        res_body["deactivated"] = _r
    return res_body


@router.post("/v1/deactivate/{user_id}")
async def deactivate_user(user_id: str):
    from .server import deactivate_user
    await deactivate_user(user_id)
    return {"deactivated": True}


@router.get("/v1/whois/{user_id}")
async def get_user_sessions(user_id: str):
    # No data
    return {"user_id": user_id, "devices": {}}


@router.get("/v1/rooms")
async def get_room_list(
        _from: typing.Annotated[int, Query(..., alias="from", ge=0)] = 0,
        limit: typing.Annotated[int, Query(..., gt=0, le=100)] = 100,
        order_by: typing.Annotated[str, Query(..., regex=ROOM_ORDER_BY_REGEX)] = "name",
        direction: typing.Annotated[str, Query(..., regex=r"^(f|b)$", alias="dir")] = "f",
        search_term: str = "",
        empty_rooms: bool | None = None
):
    """
    See: https://element-hq.github.io/synapse/latest/admin_api/rooms.html

    Some order types and parameters are not supported.
    """
    from .server import get_all_rooms
    rooms = await get_all_rooms()

    candidates = []
    for room in rooms:
        if search_term and search_term not in room.name + "\0" + room.room_id:
            continue
        if empty_rooms is not None:
            if empty_rooms and room.members == 0:
                candidates.append(room)
            elif not empty_rooms and room.members > 0:
                candidates.append(room)
            else:
                continue
        candidates.append(room)

    match order_by:
        case "name":
            candidates.sort(key=lambda x: x.name, reverse=direction == "b")
        case "canonical_alias":
            candidates.sort(key=lambda x: x.id, reverse=direction == "b")
        case "joined_members":
            candidates.sort(key=lambda x: x.members, reverse=direction == "b")

    selected = candidates[_from:_from + limit]
    next_batch = _from + limit if _from + limit < len(candidates) else None
    response = {
        "offset": len(selected) - 1,
        "rooms": [
            {
                "name": x.name,
                "room_id": x.room_id,
                "joined_members": x.members,
                "joined_local_members": x.members,
                "canonical_alias": x.room_id,
                "version": "1",
                "creator": "@admin:localhost",
                "encryption": None,
                "federatable": True,
                "public": False,
                "join_rules": "invite" if x.members <= 2 else "public",  # decent guess?
                "guest_access": "forbidden",
                "history_visibility": "shared",
                "state_events": 5 + x.members,
            } for x in selected
        ],
        "total_rooms": len(rooms)
    }
    if next_batch is not None:
        response["next_batch"] = next_batch
    prev_batch = _from - limit if _from - limit >= 0 else None
    if prev_batch is not None:
        response["prev_batch"] = prev_batch
    return response

@router.get("/v1/rooms/{room_id}")
async def get_room_details(room_id: str):
    from .server import get_room_topic, get_room_members
    room_topic = await get_room_topic(room_id)
    room_members = await get_room_members(room_id)
    return {
        "name": room_id,
        "room_id": room_id,
        "joined_members": len(room_members),
        "joined_local_members": len(room_members),
        "canonical_alias": room_id,
        "version": "1",
        "creator": "@admin:localhost",
        "encryption": None,
        "federatable": True,
        "public": False,
        "join_rules": "invite" if len(room_members) <= 2 else "public",  # decent guess?
        "guest_access": "forbidden",
        "history_visibility": "shared",
        "state_events": 6 + len(room_members),
        "topic": room_topic,
        "forgotten": len(room_members) == 0
    }

@router.get("/v1/rooms/{room_id}/members")
async def get_room_members(room_id: str):
    from .server import get_room_members as get_members
    members = await get_members(room_id)
    return {
        "total": len(members),
        "members": [
            x.user_id for x in members
        ]
    }

@router.put("/v1/rooms/{room_id}/block")
async def block_room(room_id: str):
    from .server import ban_room
    await ban_room(room_id, True, True)
    return {"block": True}


@router.put("/v1/rooms/{room_id}/block")
async def get_room_block(room_id: str):
    from .server import get_banned_rooms
    banned_rooms = await get_banned_rooms()
    for room in banned_rooms:
        if room.room_id == room_id:
            return {"block": True}
    return {"block": False}


@router.delete("/v1/rooms/{room_id}")
@router.delete("/v2/rooms/{room_id}")
async def ban_room(room_id: str):
    """Disables a room. Note that unlike with Synapse, this will always evacuate and defederate the room.

    The request body is ignored."""
    from .server import ban_room
    asyncio.create_task(ban_room(room_id, True, True))
    return {
        "kicked_users": [],
        "failed_to_kick_users": [],
        "local_aliases": [],
        "new_room_id": "!invalid:invalid.invalid",
        "delete_id": "N/A"
    }


