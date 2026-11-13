"""SQLAlchemy 模型：原件索引 / 来源记录修订 / 计算依据 / 结算修订 分层存储。"""
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# ---- 枚举（字符串常量，避免方言枚举迁移问题）----
ROLE_FINANCE = "finance"          # 平台财务：全部站点、账单确认
ROLE_OPERATOR = "operator"        # 运营：冲突/换表确认
ROLE_PROPERTY = "property"        # 物业：仅分配给自己站点的核对事项

REV_DUPLICATE = "duplicate"            # 同号同内容
REV_APPLIED = "applied"                # 同号内容变化，已生效（原数据未被账单采用）
REV_BLOCKED_ADOPTED = "blocked_adopted"  # 同号内容变化但原数据已被采用，留待下一轮

ENTITY_ACTIVE = "active"
ENTITY_SUPERSEDED = "superseded"
ENTITY_PENDING = "pending_data"    # 缺起止读数等
ENTITY_SUSPENDED = "suspended"     # 回退无依据等原因挂起

JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_SUSPENDED = "suspended"
JOB_FAILED = "failed"

BASIS_MEASURED = "measured"        # 读数差值实测
BASIS_ESTIMATED = "estimated"      # 跨时段无中间计量，明确标注的估算
BASIS_PENDING = "pending"          # 缺起止读数，待补
BASIS_SUSPENDED = "suspended"      # 回退无依据，整段挂起

BILL_DRAFT = "draft"
BILL_SUSPENDED = "suspended"
BILL_CONFIRMED = "confirmed"
BILL_ADJUSTED = "adjusted"

TASK_OPEN = "open"
TASK_SUBMITTED = "submitted"
TASK_RESOLVED = "resolved"

CONFLICT_OPEN = "open"
CONFLICT_RESOLVED = "resolved"

# ---- 分成合同版本状态 ----
CONTRACT_DRAFT = "draft"            # 草稿：可反复编辑，不参与结算
CONTRACT_ACTIVE = "active"          # 已生效：作为后续账单的现行规则
CONTRACT_SUPERSEDED = "superseded"  # 被新版本接替，历史试算/审批仍指向本版本
CONTRACT_ARCHIVED = "archived"      # 停用且无新版本接替

# ---- 追溯试算状态 ----
TRIAL_CALCULATED = "calculated"    # 试算完成且与当前草稿一致，可提交审批
TRIAL_STALE = "stale"              # 草稿在试算后再次编辑，输入已变，失去批准资格（仍可查看）
TRIAL_NEEDS_CLARIFICATION = "needs_clarification"  # 保底与优惠冲突且未声明优先级，停在待澄清
TRIAL_INVALIDATED = "invalidated"  # 审批期间电量更正，相关月份重算，原试算可查看但不可批准
TRIAL_APPROVED = "approved"        # 已在指定期间批准差额
TRIAL_REJECTED = "rejected"        # 审批驳回（留痕，仍可查看）
TRIAL_BLOCKED_OVERLAP = "blocked_overlap"  # 与已批准方案覆盖相同账期且采用关系未确定
TRIAL_SUPERSEDED = "superseded"            # 批准了覆盖相同账期的新方案，本试算留痕不再有效

# ---- 试算行结果状态 ----
LINE_CLARIFY = "needs_clarification"

# ---- 合同审批（按指定期间批准差额） ----
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64))  # IANA 时区，如 Asia/Shanghai
    diff_threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.02"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sites: Mapped[list["UserSite"]] = relationship(back_populates="user")


class UserSite(Base):
    """物业账号与站点的授权范围。"""
    __tablename__ = "user_sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    user: Mapped[User] = relationship(back_populates="sites")
    __table_args__ = (UniqueConstraint("user_id", "site_id"),)


class Meter(Base):
    __tablename__ = "meters"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_no: Mapped[str] = mapped_column(String(64))
    meter_kind: Mapped[str] = mapped_column(String(16))  # master=物业总表 / device=设备表
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_cumulative: Mapped[bool] = mapped_column(Boolean, default=True)  # 累计/增量
    __table_args__ = (UniqueConstraint("site_id", "meter_no"),)


class ImportFile(Base):
    """原件索引：原始文件本身落盘并留痕。"""
    __tablename__ = "import_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))  # reading/order/tariff/public_load/replacement
    original_name: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_by: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportRow(Base):
    """原件逐行留存：文件 id + 行号 + 原始行文本 + 处理结果。"""
    __tablename__ = "import_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_file_id: Mapped[int] = mapped_column(ForeignKey("import_files.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    raw_line: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32))  # inserted/revised/duplicate/blocked_adopted/rejected
    source_record_no: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SourceRecord(Base):
    """按“来源+来源记录号+站点”识别同一条业务记录的所有修订版本。"""
    __tablename__ = "source_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    source_record_no: Mapped[str] = mapped_column(String(96))
    current_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("record_revisions.id", use_alter=True, name="fk_source_current_revision"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revisions: Mapped[list["RecordRevision"]] = relationship(
        order_by="RecordRevision.revision_no",
        foreign_keys="RecordRevision.source_record_id",
    )
    __table_args__ = (UniqueConstraint("site_id", "source_type", "source_record_no"),)


class RecordRevision(Base):
    __tablename__ = "record_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))  # applied / duplicate / blocked_adopted
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    import_row_id: Mapped[int] = mapped_column(ForeignKey("import_rows.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DeviceReplacement(Base):
    """设备/电表更换依据；回退处理必须先查到本记录。"""
    __tablename__ = "device_replacements"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    effective_ts: Mapped[datetime] = mapped_column(DateTime)  # UTC
    old_final_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    new_initial_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    evidence_note: Mapped[str] = mapped_column(Text)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MeterReading(Base):
    """设备/总表累计读数；每次修订产生新行，旧行不可变、不被覆盖。"""
    __tablename__ = "meter_readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    reading_ts: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    reading_ts_local: Mapped[datetime] = mapped_column(DateTime)
    reading_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index("ix_reading_meter_ts", "meter_id", "reading_ts"),)


class ChargingOrder(Base):
    __tablename__ = "charging_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(96), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)       # UTC
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    start_reading_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    end_reading_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    reported_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))  # 设备上报电量，仅作比对
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    conflict_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_conflicts.id", use_alter=True, name="fk_order_conflict"),
        nullable=True,
    )
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Tariff(Base):
    __tablename__ = "tariffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    effective_date: Mapped[date] = mapped_column(Date)   # 站点本地日期起生效
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    segments: Mapped[list["TariffSegment"]] = relationship(
        back_populates="tariff", cascade="all, delete-orphan", order_by="TariffSegment.start_hour"
    )


class TariffSegment(Base):
    """分时段：本地时间 start_hour:00 起、到 end_hour:00（不含），价格 price_per_kwh。"""
    __tablename__ = "tariff_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), index=True)
    segment_name: Mapped[str] = mapped_column(String(32))  # peak/flat/valley
    start_hour: Mapped[int] = mapped_column(Integer)
    end_hour: Mapped[int] = mapped_column(Integer)
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    tariff: Mapped[Tariff] = relationship(back_populates="segments")


class PublicLoad(Base):
    """经确认的公共负荷（如公共照明）；未经确认不得计入。"""
    __tablename__ = "public_loads"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    energy_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    load_name: Mapped[str] = mapped_column(String(128))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReconciliationJob(Base):
    """核算作业：冻结所用读数、电价、订单与分摊规则。"""
    __tablename__ = "reconciliation_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    cycle_start: Mapped[datetime] = mapped_column(DateTime)  # UTC
    cycle_end: Mapped[datetime] = mapped_column(DateTime)
    cycle_start_local: Mapped[datetime] = mapped_column(DateTime)
    cycle_end_local: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=JOB_RUNNING)
    threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    rerun_of_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(16), default="1.0")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    bill: Mapped["Bill | None"] = relationship(back_populates="job", uselist=False)


class JobInput(Base):
    """作业输入快照：重跑沿用相同输入，执行期间的更正不进入本轮。"""
    __tablename__ = "job_inputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    input_kind: Mapped[str] = mapped_column(String(32))  # reading/order/tariff/public_load/replacement
    entity_id: Mapped[int] = mapped_column(Integer)
    revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (UniqueConstraint("job_id", "input_kind", "entity_id"),)


class ReconciliationInterval(Base):
    """计算依据：总表计量区间内各成分电量，财务可逐段下钻。"""
    __tablename__ = "reconciliation_intervals"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    master_reading_start: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    master_reading_end: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    master_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    device_meter_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    charging_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))       # 实测订单电量
    estimated_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))      # 标注估算
    pending_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))        # 待补（不计金额）
    public_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    unexplained_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    suspended_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    order_meter_diff_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    basis_status: Mapped[str] = mapped_column(String(16), default=BASIS_MEASURED)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderIntervalSplit(Base):
    """订单按电价边界拆分后的最小计费段。"""
    __tablename__ = "order_interval_splits"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"), index=True)
    reconciliation_interval_id: Mapped[int | None] = mapped_column(
        ForeignKey("reconciliation_intervals.id"), nullable=True
    )
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    segment_name: Mapped[str] = mapped_column(String(32))
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    basis_status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Bill(Base):
    __tablename__ = "bills"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), unique=True)
    bill_no: Mapped[str] = mapped_column(String(64), unique=True)
    master_total_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    charging_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    estimated_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    pending_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    pending_order_count: Mapped[int] = mapped_column(Integer, default=0)
    public_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unexplained_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    suspended_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    charging_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    public_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), default=BILL_DRAFT)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[ReconciliationJob] = relationship(back_populates="bill")
    adjustments: Mapped[list["BillAdjustment"]] = relationship(back_populates="bill")


class BillAdjustment(Base):
    """已确认账单的差额更正：保留原值，以更正行体现差异，不直接替换。"""
    __tablename__ = "bill_adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    source_job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"))
    component: Mapped[str] = mapped_column(String(32))  # charging/public/unexplained/...
    original_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    corrected_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    delta: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    bill: Mapped[Bill] = relationship(back_populates="adjustments")


class OrderConflict(Base):
    """两个不同订单声称占用同一设备时段。"""
    __tablename__ = "order_conflicts"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    order_a_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"))
    order_b_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"))
    status: Mapped[str] = mapped_column(String(16), default=CONFLICT_OPEN)
    resolved_order_id: Mapped[int | None] = mapped_column(ForeignKey("charging_orders.id"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReviewTask(Base):
    """核对事项：物业只能看到并处理分配给自己站点的条目。"""
    __tablename__ = "review_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    task_kind: Mapped[str] = mapped_column(String(40))
    # meter_rollback / missing_reading / order_conflict / unexplained_diff / public_confirmation / estimated_item
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    assignee: Mapped[str] = mapped_column(String(16))  # property / operator / finance
    status: Mapped[str] = mapped_column(String(16), default=TASK_OPEN)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ============================ 分成合同变更与追溯试算 ============================

class RevenueContract(Base):
    """分成合同的一个版本。

    条款只能保存为结构化规则（rules_json），由固定字段表达：
    固定费用、分段（阶梯）比例、保底额、允许抵扣项、优惠期、生效范围、优先级。
    规则不是可执行代码，试算引擎只解释这些固定字段，无法表达任意逻辑。
    签署时间（signed_at）与实际生效时间（effective_date）分别记录，不得互相代替。
    """
    __tablename__ = "revenue_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    contract_no: Mapped[str] = mapped_column(String(64), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=CONTRACT_DRAFT, index=True)
    # 结构化规则（不可执行），结构见 services/contract_svc.py 的 RULE_SPEC
    rules_json: Mapped[str] = mapped_column(Text)
    rules_hash: Mapped[str] = mapped_column(String(64))
    # 生效范围（站点本地日期，闭区间）；签署时间仅作留痕
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("revenue_contracts.id"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                 onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("contract_no", "version_no"),)


class ContractTrial(Base):
    """一次追溯试算：固定输入（账单版本 + 新旧规则快照），结果不可变、可复算。

    草稿在试算后再编辑会使本试算变 stale；审批期间电量更正会使相关月份 invalidated。
    两种情况下原试算仍可查看，但都失去批准资格。
    """
    __tablename__ = "contract_trials"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    trial_no: Mapped[str] = mapped_column(String(64), unique=True)
    baseline_contract_id: Mapped[int] = mapped_column(ForeignKey("revenue_contracts.id"))
    candidate_contract_id: Mapped[int] = mapped_column(ForeignKey("revenue_contracts.id"))
    # 试算发起时刻的规则快照与哈希；审批只接受与当前草稿一致（hash 仍匹配）的试算
    baseline_rules_json: Mapped[str] = mapped_column(Text)
    candidate_rules_json: Mapped[str] = mapped_column(Text)
    candidate_rules_hash: Mapped[str] = mapped_column(String(64), index=True)
    # 指定试算期间（站点本地年月，YYYY-MM-01）
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default=TRIAL_CALCULATED, index=True)
    # 失效/失配原因留痕
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 输入快照：所用账单及每个账单的依据修订集合（JSON），保证固定输入可核对
    bill_snapshot_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    lines: Mapped[list["ContractTrialLine"]] = relationship(
        back_populates="trial", cascade="all, delete-orphan",
        order_by="ContractTrialLine.period_month",
    )


class ContractTrialLine(Base):
    """试算的逐月结果：原合同结果、新条款结果、差额，以及逐项运算过程与尾差。"""
    __tablename__ = "contract_trial_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey("contract_trials.id"), index=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    period_month: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM（站点本地）
    line_status: Mapped[str] = mapped_column(String(24), default=TRIAL_CALCULATED)
    clarify_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 分段收益（先计）
    base_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    cand_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    # 允许抵扣（再扣）
    base_deduction: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    cand_deduction: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    # 保底比较（最后）
    base_minimum: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    cand_minimum: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    base_minimum_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    cand_minimum_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    # 固定费用与优惠期标记
    base_fixed_fee: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    cand_fixed_fee: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    base_in_promo: Mapped[bool] = mapped_column(Boolean, default=False)
    cand_in_promo: Mapped[bool] = mapped_column(Boolean, default=False)
    # 应付（运算顺序完成后的结果）与差额
    base_payable: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    cand_payable: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    delta: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    # 日级金额汇总到月度的舍入尾差（单独列出，不重复摊到每笔订单）
    base_rounding_diff: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    cand_rounding_diff: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    # 逐项过程明细（分段/抵扣/保底/优惠的日级与月级数字，JSON）
    detail_json: Mapped[str] = mapped_column(Text)
    trial: Mapped[ContractTrial] = relationship(back_populates="lines")


class ContractApproval(Base):
    """对试算按指定期间批准差额；未批准的试算不进入应付统计。

    两份追溯方案覆盖相同账期时，必须先确定采用关系（supersedes_trial_id），
    否则试算停在 blocked_overlap，不能批准。
    """
    __tablename__ = "contract_approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    trial_id: Mapped[int] = mapped_column(ForeignKey("contract_trials.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    decision: Mapped[str] = mapped_column(String(16), default=APPROVAL_APPROVED)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    approved_delta: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 采用关系：本批准替代哪一份已批准/待批方案（覆盖相同账期时必填）
    supersedes_trial_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_trials.id"), nullable=True
    )
    approved_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
