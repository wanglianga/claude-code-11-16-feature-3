from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    display_name: str


class SiteOut(ORMModel):
    id: int
    code: str
    name: str
    timezone: str
    diff_threshold_pct: Decimal


class ImportResultOut(BaseModel):
    import_file_id: int
    row_count: int
    summary: dict[str, int]


class ImportFileOut(ORMModel):
    id: int
    site_id: int
    source_type: str
    original_name: str
    sha256: str
    row_count: int
    imported_by: str
    imported_at: datetime


class ImportRowOut(ORMModel):
    id: int
    line_no: int
    raw_line: str
    outcome: str
    source_record_no: str | None
    note: str | None


class JobIn(BaseModel):
    site_id: int
    cycle_start: datetime  # 站点本地时间
    cycle_end: datetime
    rerun_of_job_id: int | None = None


class JobOut(ORMModel):
    id: int
    site_id: int
    cycle_start_local: datetime
    cycle_end_local: datetime
    status: str
    threshold_pct: Decimal
    rerun_of_job_id: int | None
    message: str | None
    started_at: datetime
    finished_at: datetime | None


class IntervalOut(ORMModel):
    id: int
    start_ts_local: datetime
    end_ts_local: datetime
    master_reading_start: Decimal | None
    master_reading_end: Decimal | None
    master_energy_kwh: Decimal | None
    device_meter_kwh: Decimal
    charging_kwh: Decimal
    estimated_kwh: Decimal
    public_kwh: Decimal
    unexplained_kwh: Decimal
    order_meter_diff_kwh: Decimal
    basis_status: str
    note: str | None


class SplitOut(ORMModel):
    id: int
    order_id: int
    start_ts_local: datetime
    end_ts_local: datetime
    segment_name: str
    price_per_kwh: Decimal
    energy_kwh: Decimal | None
    amount: Decimal | None
    basis_status: str
    note: str | None


class BillOut(ORMModel):
    id: int
    bill_no: str
    site_id: int
    job_id: int
    master_total_kwh: Decimal
    charging_kwh: Decimal
    estimated_kwh: Decimal
    pending_kwh: Decimal
    pending_order_count: int
    public_kwh: Decimal
    unexplained_kwh: Decimal
    suspended_kwh: Decimal
    charging_amount: Decimal
    public_amount: Decimal
    status: str
    confirmed_at: datetime | None
    confirmed_by: str | None


class AdjustmentIn(BaseModel):
    component: str
    corrected_value: Decimal
    reason: str


class AdjustmentOut(ORMModel):
    id: int
    bill_id: int
    source_job_id: int
    component: str
    original_value: Decimal
    corrected_value: Decimal
    delta: Decimal
    reason: str
    created_by: str
    created_at: datetime


class TaskOut(ORMModel):
    id: int
    site_id: int
    job_id: int | None
    task_kind: str
    title: str
    detail: str
    assignee: str
    status: str
    response: str | None
    responded_by: str | None
    responded_at: datetime | None


class TaskRespondIn(BaseModel):
    response: str


class ConflictOut(ORMModel):
    id: int
    site_id: int
    device_id: str
    order_a_id: int
    order_b_id: int
    status: str
    resolved_order_id: int | None
    resolution_note: str | None


class ConflictResolveIn(BaseModel):
    resolved_order_id: int
    resolution_note: str


class RevisionOut(ORMModel):
    id: int
    source_record_id: int
    revision_no: int
    status: str
    content_hash: str
    created_at: datetime


class SourceRecordOut(ORMModel):
    id: int
    source_type: str
    source_record_no: str
    current_revision_id: int | None
    revisions: list[RevisionOut] = []


# ============================ 分成合同与追溯试算 ============================

class RevenueTierIn(BaseModel):
    upper_revenue: Decimal | None = None   # 最后一段为 null 表示开放
    rate: Decimal


class DeductionIn(BaseModel):
    name: str | None = None
    monthly_amount: Decimal = Decimal("0")
    start_date: date
    end_date: date | None = None


class PromoPeriodIn(BaseModel):
    start_date: date
    end_date: date


class ContractRulesIn(BaseModel):
    """条款的结构化表达：只接受固定字段，任何自由代码都会被服务端校验拒绝。"""
    fixed_fee: Decimal = Decimal("0")
    minimum_fee: Decimal = Decimal("0")
    revenue_tiers: list[RevenueTierIn]
    deductions: list[DeductionIn] = []
    promo_periods: list[PromoPeriodIn] = []
    priority: str | None = None   # minimum_first / promo_first / 留空(待澄清)

    @model_validator(mode="after")
    def _check_shape(self):
        if not self.revenue_tiers:
            raise ValueError("revenue_tiers 至少需要一个分段")
        if self.priority not in (None, "minimum_first", "promo_first"):
            raise ValueError("priority 只能是 minimum_first / promo_first / 留空")
        return self


class ContractCreateIn(BaseModel):
    site_id: int
    contract_no: str
    name: str
    # 实际生效范围（不是签署时间）
    effective_date: date
    end_date: date | None = None
    signed_at: datetime | None = None
    rules: ContractRulesIn
    # 基于哪个已生效合同发起变更（可空=站点首份合同）
    base_contract_id: int | None = None


class ContractUpdateIn(BaseModel):
    """草稿可反复编辑；一旦生效即不可再改。"""
    name: str | None = None
    effective_date: date | None = None
    end_date: date | None = None
    signed_at: datetime | None = None
    rules: ContractRulesIn | None = None


class ContractOut(ORMModel):
    id: int
    site_id: int
    contract_no: str
    version_no: int
    name: str
    status: str
    rules_json: str
    rules_hash: str
    effective_date: date | None
    end_date: date | None
    signed_at: datetime | None
    superseded_by_id: int | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class TrialCreateIn(BaseModel):
    baseline_contract_id: int
    candidate_contract_id: int
    period_start: date
    period_end: date


class ApprovalIn(BaseModel):
    period_start: date
    period_end: date
    note: str | None = None
    # 覆盖相同账期时必填：声明本方案替代哪一份已批准试算
    supersedes_trial_id: int | None = None


class RejectIn(BaseModel):
    note: str


class TrialLineOut(ORMModel):
    id: int
    bill_id: int | None
    period_month: str
    line_status: str
    clarify_reason: str | None
    base_revenue: Decimal
    cand_revenue: Decimal
    base_deduction: Decimal
    cand_deduction: Decimal
    base_minimum: Decimal
    cand_minimum: Decimal
    base_minimum_applied: bool
    cand_minimum_applied: bool
    base_fixed_fee: Decimal
    cand_fixed_fee: Decimal
    base_in_promo: bool
    cand_in_promo: bool
    base_payable: Decimal
    cand_payable: Decimal
    delta: Decimal
    base_rounding_diff: Decimal
    cand_rounding_diff: Decimal
    detail_json: str


class TrialOut(ORMModel):
    id: int
    site_id: int
    trial_no: str
    baseline_contract_id: int
    candidate_contract_id: int
    period_start: date
    period_end: date
    status: str
    status_reason: str | None
    candidate_rules_hash: str
    created_by: str
    created_at: datetime
    lines: list[TrialLineOut] = []


class ApprovalOut(ORMModel):
    id: int
    trial_id: int
    site_id: int
    decision: str
    period_start: date
    period_end: date
    approved_delta: Decimal
    note: str | None
    supersedes_trial_id: int | None
    approved_by: str
    created_at: datetime
