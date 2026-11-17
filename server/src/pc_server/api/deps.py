from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from pc_server.core.exceptions import AuthError
from pc_server.db.session import get_db
from pc_server.models import Device
from pc_server.services import devices

DbSession = Annotated[AsyncSession, Depends(get_db)]


async def current_device(
    db: DbSession, authorization: Annotated[str | None, Header()] = None
) -> Device:
    """Agents authenticate with `Authorization: Bearer <device token>`."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError()
    return await devices.authenticate_device(db, token.strip())


CurrentDevice = Annotated[Device, Depends(current_device)]
