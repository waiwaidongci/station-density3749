from fastapi import APIRouter,Depends
from ..config.database import db_session
from ..schemas.source import ReplayInput
from ..services.auth import current_user,writer
from ..controllers.quarantine import QuarantineController as C
router=APIRouter()
@router.get('/quarantine')
def list_rows(source_id:int|None=None,state:str|None=None,db=Depends(db_session),u=Depends(current_user)):
    return C.list(db,source_id,state)
@router.get('/quarantine/counts')
def counts(db=Depends(db_session),u=Depends(current_user)):
    return C.counts(db)
@router.post('/quarantine/{id}/replay')
def replay(id:int,data:ReplayInput|None=None,db=Depends(db_session),u=Depends(writer)):
    override=data.model_dump(exclude_none=True) if data else None
    return C.replay(db,u,id,override)
@router.post('/quarantine/{id}/discard')
def discard(id:int,db=Depends(db_session),u=Depends(writer)):
    return C.discard(db,u,id)
