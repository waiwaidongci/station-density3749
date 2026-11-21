from ..services import ingestion
from ..models.quarantine import Quarantine
class QuarantineController:
    list=staticmethod(ingestion.list_quarantine)
    replay=staticmethod(ingestion.replay_quarantine)
    discard=staticmethod(ingestion.discard_quarantine)
    @staticmethod
    def counts(db):
        from sqlalchemy import select,func
        open_count=db.scalar(select(func.count()).select_from(Quarantine).where(Quarantine.state=='open'))
        total=db.scalar(select(func.count()).select_from(Quarantine))
        return {'open':int(open_count or 0),'total':int(total or 0)}
