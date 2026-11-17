"""API for the agents on the children's computers."""

from fastapi import APIRouter

from pc_server.api.deps import CurrentDevice, DbSession
from pc_server.services import devices
from pc_shared.protocol import RegisterRequest, RegisterResponse, SyncRequest, SyncResponse

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/register")
async def register(request: RegisterRequest, db: DbSession) -> RegisterResponse:
    device, token = await devices.register_device(db, request)
    return RegisterResponse(device_id=device.id, token=token)


@router.post("/sync")
async def sync(request: SyncRequest, device: CurrentDevice, db: DbSession) -> SyncResponse:
    return await devices.sync_device(db, device, request)
