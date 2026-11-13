"""分成合同、追溯试算与审批的 HTTP 接口。

写操作（草稿编辑、生效、试算、审批）仅平台财务；运营/物业只读且受站点授权约束。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, require_roles
from .. import models, schemas
from ..services import contract_svc as svc

router = APIRouter(prefix="/api/contracts", tags=["contracts"])

READ_ROLES = (models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY)


def _load_contract(db: Session, contract_id: int, user: models.User) -> models.RevenueContract:
    contract = db.get(models.RevenueContract, contract_id)
    if contract is None:
        raise HTTPException(404, "合同版本不存在")
    ensure_site_access(db, user, contract.site_id)
    return contract


def _load_trial(db: Session, trial_id: int, user: models.User) -> models.ContractTrial:
    trial = db.get(models.ContractTrial, trial_id)
    if trial is None:
        raise HTTPException(404, "试算不存在")
    ensure_site_access(db, user, trial.site_id)
    return trial


# ------------------------------- 合同版本 -------------------------------

@router.get("", response_model=list[schemas.ContractOut])
def list_contracts(site_id: int | None = None, contract_no: str | None = None,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(require_roles(*READ_ROLES))):
    stmt = select(models.RevenueContract).order_by(
        models.RevenueContract.contract_no, models.RevenueContract.version_no
    )
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.RevenueContract.site_id == site_id)
    if contract_no:
        stmt = stmt.where(models.RevenueContract.contract_no == contract_no)
    rows = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(
            db.scalars(
                select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
            ).all()
        )
        rows = [r for r in rows if r.site_id in allowed]
    return rows


@router.post("", response_model=schemas.ContractOut)
def create_contract(body: schemas.ContractCreateIn, db: Session = Depends(get_db),
                    user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    ensure_site_access(db, user, body.site_id)
    site = db.get(models.Site, body.site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    if body.end_date and body.end_date < body.effective_date:
        raise HTTPException(400, "生效结束日期不能早于开始日期")
    try:
        rules = svc.validate_rules(body.rules.model_dump(mode="json"))
    except svc.ContractRuleError as exc:
        raise HTTPException(400, f"规则校验失败: {exc}") from exc

    # 版本号：同一合同号递增；首份无基线，变更须基于已生效版本
    existing = db.scalars(
        select(models.RevenueContract).where(
            models.RevenueContract.contract_no == body.contract_no
        ).order_by(models.RevenueContract.version_no.desc())
    ).all()
    if existing:
        base = db.get(models.RevenueContract, body.base_contract_id) if body.base_contract_id else None
        if base is None or base.contract_no != body.contract_no or base.status not in (
            models.CONTRACT_ACTIVE, models.CONTRACT_SUPERSEDED
        ):
            raise HTTPException(400, "合同变更必须基于同一合同号下已生效的版本")
        version_no = existing[0].version_no + 1
    else:
        if body.base_contract_id is not None:
            raise HTTPException(400, "站点首份合同不能指定基线版本")
        version_no = 1

    contract = models.RevenueContract(
        site_id=body.site_id,
        contract_no=body.contract_no,
        version_no=version_no,
        name=body.name,
        status=models.CONTRACT_DRAFT,
        rules_json=svc.canonical_rules(rules),
        rules_hash=svc.rules_hash(rules),
        effective_date=body.effective_date,
        end_date=body.end_date,
        signed_at=body.signed_at,
        created_by=user.username,
    )
    db.add(contract)
    db.commit()
    db.refresh(contract)
    return contract


@router.get("/trials", response_model=list[schemas.TrialOut])
def list_trials(site_id: int | None = None, contract_id: int | None = None,
                db: Session = Depends(get_db),
                user: models.User = Depends(require_roles(*READ_ROLES))):
    # 静态路径必须在 GET /{contract_id} 之前注册，否则 "trials" 会被当作合同 id 解析
    stmt = select(models.ContractTrial).order_by(models.ContractTrial.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.ContractTrial.site_id == site_id)
    if contract_id is not None:
        stmt = stmt.where(
            (models.ContractTrial.baseline_contract_id == contract_id)
            | (models.ContractTrial.candidate_contract_id == contract_id)
        )
    rows = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(
            db.scalars(
                select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
            ).all()
        )
        rows = [r for r in rows if r.site_id in allowed]
    return rows


@router.get("/{contract_id}", response_model=schemas.ContractOut)
def get_contract(contract_id: int, db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(*READ_ROLES))):
    return _load_contract(db, contract_id, user)


@router.patch("/{contract_id}", response_model=schemas.ContractOut)
def update_contract(contract_id: int, body: schemas.ContractUpdateIn,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    contract = _load_contract(db, contract_id, user)
    if contract.status != models.CONTRACT_DRAFT:
        raise HTTPException(409, "只有草稿可以反复编辑；生效版本不可修改，只能发起新版本变更")
    if body.name is not None:
        contract.name = body.name
    if body.effective_date is not None:
        contract.effective_date = body.effective_date
    if body.end_date is not None:
        contract.end_date = body.end_date
    if body.signed_at is not None:
        contract.signed_at = body.signed_at
    if body.rules is not None:
        try:
            rules = svc.validate_rules(body.rules.model_dump(mode="json"))
        except svc.ContractRuleError as exc:
            raise HTTPException(400, f"规则校验失败: {exc}") from exc
        contract.rules_json = svc.canonical_rules(rules)
        contract.rules_hash = svc.rules_hash(rules)
    if contract.end_date and contract.effective_date and contract.end_date < contract.effective_date:
        raise HTTPException(400, "生效结束日期不能早于开始日期")
    db.commit()
    db.refresh(contract)
    return contract


@router.post("/{contract_id}/activate", response_model=schemas.ContractOut)
def activate_contract(contract_id: int, db: Session = Depends(get_db),
                      user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    """生效草稿：接替同合同号的当前生效版本；生效时间以实际生效日为准，与签署时间分别留痕。"""
    contract = _load_contract(db, contract_id, user)
    if contract.status != models.CONTRACT_DRAFT:
        raise HTTPException(409, "只有草稿可以生效")
    if contract.effective_date is None:
        raise HTTPException(400, "缺少实际生效日期，不能生效")
    current = db.scalars(
        select(models.RevenueContract).where(
            models.RevenueContract.contract_no == contract.contract_no,
            models.RevenueContract.status == models.CONTRACT_ACTIVE,
        )
    ).all()
    for cur in current:
        cur.status = models.CONTRACT_SUPERSEDED
        cur.superseded_by_id = contract.id
    contract.status = models.CONTRACT_ACTIVE
    db.commit()
    db.refresh(contract)
    return contract


# ------------------------------- 追溯试算 -------------------------------

@router.post("/trials", response_model=schemas.TrialOut)
def create_trial(body: schemas.TrialCreateIn, db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    baseline = db.get(models.RevenueContract, body.baseline_contract_id)
    candidate = db.get(models.RevenueContract, body.candidate_contract_id)
    if baseline is None or candidate is None:
        raise HTTPException(404, "原合同或新条款不存在")
    ensure_site_access(db, user, candidate.site_id)
    if body.period_end < body.period_start:
        raise HTTPException(400, "试算结束期间不能早于开始期间")
    site = db.get(models.Site, candidate.site_id)
    try:
        trial = svc.create_trial(
            db, site, baseline, candidate,
            body.period_start, body.period_end, user.username,
        )
        db.commit()
    except svc.ContractTrialError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.refresh(trial)
    return trial


@router.get("/trials/{trial_id}", response_model=schemas.TrialOut)
def get_trial(trial_id: int, db: Session = Depends(get_db),
              user: models.User = Depends(require_roles(*READ_ROLES))):
    trial = _load_trial(db, trial_id, user)
    # 读取时按固定输入的当前状态刷新可批准性（试算数字本身不变，仍可查看）
    svc.classify_trial(db, trial)
    db.commit()
    db.refresh(trial)
    return trial


@router.post("/trials/{trial_id}/refresh", response_model=schemas.TrialOut)
def refresh_trial(trial_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(require_roles(*READ_ROLES))):
    trial = _load_trial(db, trial_id, user)
    svc.classify_trial(db, trial)
    db.commit()
    db.refresh(trial)
    return trial


@router.post("/trials/{trial_id}/approve", response_model=schemas.ApprovalOut)
def approve_trial(trial_id: int, body: schemas.ApprovalIn,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    trial = _load_trial(db, trial_id, user)
    try:
        approval = svc.approve_trial(
            db, trial, body.period_start, body.period_end,
            body.note, body.supersedes_trial_id, user.username,
        )
        db.commit()
    except svc.ContractTrialError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    db.refresh(approval)
    return approval


@router.post("/trials/{trial_id}/reject", response_model=schemas.TrialOut)
def reject_trial(trial_id: int, body: schemas.RejectIn,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    trial = _load_trial(db, trial_id, user)
    try:
        svc.reject_trial(db, trial, body.note, user.username)
        db.commit()
    except svc.ContractTrialError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    db.refresh(trial)
    return trial


@router.get("/trials/{trial_id}/approvals", response_model=list[schemas.ApprovalOut])
def trial_approvals(trial_id: int, db: Session = Depends(get_db),
                    user: models.User = Depends(require_roles(*READ_ROLES))):
    trial = _load_trial(db, trial_id, user)
    return db.scalars(
        select(models.ContractApproval)
        .where(models.ContractApproval.trial_id == trial.id)
        .order_by(models.ContractApproval.id)
    ).all()


# ------------------------------- 账单视图与应付统计 -------------------------------

@router.get("/bills/{bill_id}/view")
def bill_contract_view(bill_id: int, db: Session = Depends(get_db),
                       user: models.User = Depends(require_roles(*READ_ROLES))):
    """原合同结果 / 新条款结果 / 实际批准调整额三数分列。"""
    bill = db.get(models.Bill, bill_id)
    if bill is None:
        raise HTTPException(404, "账单不存在")
    ensure_site_access(db, user, bill.site_id)
    return svc.bill_contract_view(db, bill)


@router.get("/sites/{site_id}/payable-stats")
def payable_stats(site_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(require_roles(*READ_ROLES))):
    ensure_site_access(db, user, site_id)
    return svc.site_payable_stats(db, site_id)
