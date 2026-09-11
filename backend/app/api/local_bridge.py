"""Separate device credentials grant only the assigned release delivery protocol."""
from uuid import UUID
from pathlib import Path
from fastapi import APIRouter,Depends,Request,Response,Query
from app.api.auth import get_durable_principal,rate_limiter
from app.api.documents import public_error
from app.domain.identity import PrincipalContext
from app.integrations.bridge_contracts import BridgePairRequest,BridgePairClaim,BridgeDeliveryRequest,BridgeLease,BridgeReceipt,BridgeFailure
from app.services import local_bridge as service

router=APIRouter(tags=['local-bridge'])


@router.get('/api/local-bridge/client')
async def download_client(principal:PrincipalContext=Depends(get_durable_principal)):
    path=Path(__file__).resolve().parents[1]/'integrations/local_bridge_client.py'
    return Response(path.read_bytes(),media_type='text/x-python',headers={
        'Content-Disposition':'attachment; filename="cad_local_bridge.py"','Cache-Control':'no-store'})


def credential(request:Request):
    value=request.headers.get('Authorization','')
    if not value.startswith('Bridge '):raise public_error(PermissionError('需要本地连接凭据'))
    return value[7:]


@router.post('/api/documents/{document_id}/bridges/pair',status_code=201)
async def pair(document_id:UUID,body:BridgePairRequest,request:Request,principal:PrincipalContext=Depends(get_durable_principal)):
    await rate_limiter.check(request,str(principal.principal_id))
    try:return await service.create_pairing(principal,document_id,body.label)
    except Exception as exc:raise public_error(exc) from exc


@router.get('/api/documents/{document_id}/bridges')
async def bridges(document_id:UUID,principal:PrincipalContext=Depends(get_durable_principal)):
    try:return await service.list_bridges(principal,document_id)
    except Exception as exc:raise public_error(exc) from exc


@router.delete('/api/documents/{document_id}/bridges/{bridge_id}',status_code=204)
async def revoke(document_id:UUID,bridge_id:UUID,principal:PrincipalContext=Depends(get_durable_principal)):
    try:await service.revoke_bridge(principal,document_id,bridge_id)
    except Exception as exc:raise public_error(exc) from exc
    return Response(status_code=204)


@router.post('/api/documents/{document_id}/releases/{release_id}/deliveries',status_code=202)
async def deliver(document_id:UUID,release_id:UUID,body:BridgeDeliveryRequest,principal:PrincipalContext=Depends(get_durable_principal)):
    try:return await service.queue_delivery(principal,document_id,release_id,body.bridge_id)
    except Exception as exc:raise public_error(exc) from exc


@router.post('/api/local-bridge/pair')
async def claim_pair(body:BridgePairClaim,request:Request):
    await rate_limiter.check(request,None)
    try:return await service.claim_pairing(body)
    except Exception as exc:raise public_error(exc) from exc


@router.post('/api/local-bridge/poll')
async def poll(token:str=Depends(credential)):
    try:return await service.claim_delivery(token)
    except Exception as exc:raise public_error(exc) from exc


@router.get('/api/local-bridge/deliveries/{delivery_id}/archive')
async def archive(delivery_id:UUID,lease_token:UUID=Query(),token:str=Depends(credential)):
    try:return Response(await service.delivery_archive(token,delivery_id,lease_token),media_type='application/zip',headers={'Cache-Control':'no-store'})
    except Exception as exc:raise public_error(exc) from exc


@router.post('/api/local-bridge/deliveries/{delivery_id}/renew')
async def renew(delivery_id:UUID,body:BridgeLease,token:str=Depends(credential)):
    try:return await service.renew_delivery(token,delivery_id,body.lease_token)
    except Exception as exc:raise public_error(exc) from exc


@router.post('/api/local-bridge/deliveries/{delivery_id}/ack')
async def acknowledge(delivery_id:UUID,body:BridgeReceipt,token:str=Depends(credential)):
    try:return await service.acknowledge_delivery(token,delivery_id,body)
    except Exception as exc:raise public_error(exc) from exc


@router.post('/api/local-bridge/deliveries/{delivery_id}/fail')
async def fail(delivery_id:UUID,body:BridgeFailure,token:str=Depends(credential)):
    try:return await service.fail_delivery(token,delivery_id,body)
    except Exception as exc:raise public_error(exc) from exc
