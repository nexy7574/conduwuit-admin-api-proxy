from fastapi import APIRouter, HTTPException


router = APIRouter(
    prefix="/_synapse/admin",
    tags=["Synapse Compatibility"]
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
    result = []
    for user_id in user_ids:
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
                "/_matrix/client/v3/profile/{}/displayname".format(user_id),
                headers={
                    "Authorization": f"Bearer {ACCESS_TOKEN}"
                }
            )
            is_admin = False
            avatar_url = avatar_url.json().get("avatar_url") if avatar_url.status_code == 200 else None
        result.append(
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
                "locked": False
            }
        )

    return {
        "users": result,
        "total": len(result)
    }
