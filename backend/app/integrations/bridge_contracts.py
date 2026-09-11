"""Local Bridge accepts release files only; it cannot execute arbitrary commands."""
from typing import Literal
from uuid import UUID
from pydantic import Field,model_validator
from app.freecad.engineering_contracts import EngineeringContract


class BridgePairRequest(EngineeringContract):
    label:str=Field(min_length=1,max_length=120)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.label.strip():raise ValueError('请填写本地连接名称')
        return self


class BridgeClientInfo(EngineeringContract):
    protocol:Literal['cad-local-bridge.v1']
    version:Literal['1.0.0']
    hostname:str=Field(min_length=1,max_length=255)
    target_label:str=Field(min_length=1,max_length=255)
    capabilities:tuple[Literal['verified_release_files'],...]=('verified_release_files',)


class BridgePairClaim(EngineeringContract):
    pairing_code:str=Field(min_length=100,max_length=240)
    client_info:BridgeClientInfo


class BridgeDeliveryRequest(EngineeringContract):
    bridge_id:UUID


class BridgeLease(EngineeringContract):
    lease_token:UUID


class BridgeReceipt(BridgeLease):
    archive_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    manifest_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    file_count:int=Field(ge=1,le=1000,strict=True)
    total_bytes:int=Field(ge=1,le=128*1024*1024,strict=True)
    directory:str=Field(min_length=1,max_length=2048)
    verified_files:dict[str,str]=Field(max_length=1000)


class BridgeFailure(BridgeLease):
    message:str=Field(min_length=1,max_length=1000)
