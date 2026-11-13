"""分成合同规则引擎与追溯试算。

设计约束（对应题目）：
1. 条款只能保存为结构化规则（固定费用 / 分段比例 / 保底额 / 抵扣项 / 优惠期 / 生效范围 / 优先级），
   规则里只允许数字、字符串、布尔、日期与这些类型组成的列表/字典，引擎只解释固定字段，
   全流程不出现 eval/exec，条款无法写成任意执行代码。
2. 试算读取“指定账单版本”的冻结拆分段，按明确顺序运算：
      先计分段（阶梯）收益 → 再扣允许抵扣 → 最后比较保底；
   保底与优惠在同一月互相覆盖且合同未写优先级时，该月停在 needs_clarification，
   程序不猜测对谁更有利。
3. 日级金额汇总到月度时统一处理舍入：月度金额按月度合计一次性舍入，
   并把“月度舍入值 − 逐日先舍入再合计”的尾差单独列出，不把误差重复摊到每笔订单。
4. 试算固定输入（账单版本 + 依据修订快照 + 规则快照）；草稿再编辑 → stale，
   审批期间电量更正/账单版本变化 → invalidated；原试算均可查看但失去批准资格。
5. 两份追溯方案覆盖相同账期必须先确定采用关系（supersedes），否则 blocked_overlap。
6. 未批准的试算不进入应付统计；账单最终可分别查到原合同结果、新条款结果与实际批准调整额。
"""
from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .billing import check_new_revisions
from .timeutil import money, quantize

PRIORITY_MINIMUM = "minimum_first"   # 保底优先：优惠不豁免保底
PRIORITY_PROMO = "promo_first"       # 优惠优先：重叠期豁免保底
VALID_PRIORITY = {PRIORITY_MINIMUM, PRIORITY_PROMO}

# 规则允许的标量：原生 JSON 标量 + Decimal/date/datetime（Pydantic 导出值），
# 明确不含函数、类、集合等任何可执行/不可序列化内容
_ALLOWED_SCALAR = (str, int, float, bool, type(None), Decimal, date, datetime)


class ContractRuleError(ValueError):
    pass


class ContractTrialError(RuntimeError):
    pass


# --------------------------- 规则规范化与校验 ---------------------------

def _is_plain_structure(value) -> bool:
    """递归确认规则只由基础类型组成（杜绝函数/代码等任意可执行内容）。"""
    if isinstance(value, _ALLOWED_SCALAR):
        return True
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_plain_structure(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_is_plain_structure(v) for v in value)
    return False


def _as_decimal(value, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ContractRuleError(f"{field} 必须是数值") from exc


def _as_date(value, field: str) -> date:
    if value in (None, ""):
        raise ContractRuleError(f"{field} 不能为空")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ContractRuleError(f"{field} 日期格式应为 YYYY-MM-DD") from exc


def _opt_date(value, field: str) -> date | None:
    if value in (None, ""):
        return None
    return _as_date(value, field)


def canonical_rules(rules: dict) -> str:
    return json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rules_hash(rules: dict) -> str:
    return hashlib.sha256(canonical_rules(rules).encode("utf-8")).hexdigest()


def validate_rules(raw: dict) -> dict:
    """把前端/API 传入的规则校验并规范化为固定结构；非法结构直接拒绝。"""
    if not isinstance(raw, dict):
        raise ContractRuleError("规则必须是结构化对象，不接受自由文本或代码")
    if not _is_plain_structure(raw):
        raise ContractRuleError("规则只允许数值/文本/布尔/日期及其列表、字典，不接受可执行内容")
    unknown = set(raw) - {
        "fixed_fee", "revenue_tiers", "minimum_fee", "deductions",
        "promo_periods", "priority",
    }
    if unknown:
        raise ContractRuleError(f"存在未知规则字段: {sorted(unknown)}")

    fixed_fee = _as_decimal(raw.get("fixed_fee", 0), "fixed_fee")
    if fixed_fee < 0:
        raise ContractRuleError("fixed_fee 不能为负")

    minimum_fee = _as_decimal(raw.get("minimum_fee", 0), "minimum_fee")
    if minimum_fee < 0:
        raise ContractRuleError("minimum_fee 不能为负")

    tiers_raw = raw.get("revenue_tiers")
    if not isinstance(tiers_raw, list) or not tiers_raw:
        raise ContractRuleError("revenue_tiers 至少需要一个分段")
    tiers: list[dict] = []
    last_upper: Decimal | None = None
    for idx, t in enumerate(tiers_raw):
        if not isinstance(t, dict):
            raise ContractRuleError(f"第 {idx + 1} 个分段必须是对象")
        rate = _as_decimal(t.get("rate"), f"revenue_tiers[{idx}].rate")
        if not (Decimal("0") <= rate <= Decimal("1")):
            raise ContractRuleError(f"第 {idx + 1} 个分段比例必须在 0~1 之间")
        upper = t.get("upper_revenue")
        upper_d = None if upper in (None, "") else _as_decimal(upper, f"revenue_tiers[{idx}].upper_revenue")
        if upper_d is not None:
            if upper_d <= 0:
                raise ContractRuleError("分段上限必须为正数（最后一段用 null 表示开放）")
            if last_upper is not None and upper_d <= last_upper:
                raise ContractRuleError("分段上限必须严格递增，最后一段为 null")
        elif idx != len(tiers_raw) - 1:
            raise ContractRuleError("只有最后一个分段可以使用 null 开放上限")
        tiers.append({"upper_revenue": (str(upper_d) if upper_d is not None else None),
                      "rate": str(rate)})
        last_upper = upper_d

    deductions: list[dict] = []
    for idx, d in enumerate(raw.get("deductions", []) or []):
        if not isinstance(d, dict):
            raise ContractRuleError(f"第 {idx + 1} 个抵扣项必须是对象")
        amount = _as_decimal(d.get("monthly_amount", 0), f"deductions[{idx}].monthly_amount")
        if amount < 0:
            raise ContractRuleError("抵扣金额不能为负")
        start = _as_date(d.get("start_date"), f"deductions[{idx}].start_date")
        end = _opt_date(d.get("end_date"), f"deductions[{idx}].end_date")
        if end and end < start:
            raise ContractRuleError("抵扣项结束日期不能早于开始日期")
        deductions.append({
            "name": str(d.get("name", f"抵扣{idx + 1}"))[:64],
            "monthly_amount": str(amount),
            "start_date": start.isoformat(),
            "end_date": end.isoformat() if end else None,
        })

    promos: list[dict] = []
    for idx, p in enumerate(raw.get("promo_periods", []) or []):
        if not isinstance(p, dict):
            raise ContractRuleError(f"第 {idx + 1} 个优惠期必须是对象")
        start = _as_date(p.get("start_date"), f"promo_periods[{idx}].start_date")
        end = _as_date(p.get("end_date"), f"promo_periods[{idx}].end_date")
        if end < start:
            raise ContractRuleError("优惠期结束日期不能早于开始日期")
        promos.append({"start_date": start.isoformat(), "end_date": end.isoformat()})

    priority = raw.get("priority")
    if priority is not None and priority not in VALID_PRIORITY:
        raise ContractRuleError(f"priority 只能是 {sorted(VALID_PRIORITY)} 或留空")

    return {
        "fixed_fee": str(fixed_fee),
        "minimum_fee": str(minimum_fee),
        "revenue_tiers": tiers,
        "deductions": deductions,
        "promo_periods": promos,
        "priority": priority,
    }


# --------------------------- 日期/覆盖范围工具 ---------------------------

def month_bounds(month: str) -> tuple[date, date]:
    year, mon = (int(x) for x in month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last)


def _intersect_days(lo_a: date, hi_a: date, lo_b: date | None, hi_b: date | None) -> int:
    lo = max(lo_a, lo_b or lo_a)
    hi = min(hi_a, hi_b or hi_a)
    return max(0, (hi - lo).days + 1)


def _windows(raw_items: list[dict]) -> list[tuple[date, date | None, dict]]:
    return [(_as_date(i["start_date"], "start"),
             _opt_date(i.get("end_date"), "end"), i) for i in raw_items]


# --------------------------- 指定账单版本的固定输入 ---------------------------

@dataclass
class MonthInput:
    month: str
    bill_id: int
    job_id: int
    daily_revenue: dict[date, Decimal]   # 来自账单冻结拆分段的日级充电金额
    revision_ids: list[int]


def _bill_month(db: Session, bill: models.Bill) -> str:
    job = db.get(models.ReconciliationJob, bill.job_id)
    return f"{job.cycle_start_local.year:04d}-{job.cycle_start_local.month:02d}"


def gather_month_inputs(db: Session, site_id: int, period_start: date,
                        period_end: date) -> list[MonthInput]:
    """按站点本地账期读取账单版本：同一月取最近一次作业的账单，金额只来自其冻结拆分段。"""
    bills = db.scalars(
        select(models.Bill)
        .join(models.ReconciliationJob, models.ReconciliationJob.id == models.Bill.job_id)
        .where(models.Bill.site_id == site_id)
    ).all()
    lo, hi = period_start.strftime("%Y-%m"), period_end.strftime("%Y-%m")
    by_month: dict[str, models.Bill] = {}
    for bill in bills:
        month = _bill_month(db, bill)
        if month < lo or month > hi:
            continue
        old = by_month.get(month)
        if old is None or bill.job_id > old.job_id:
            by_month[month] = bill

    out: list[MonthInput] = []
    for month in sorted(by_month):
        bill = by_month[month]
        splits = db.scalars(
            select(models.OrderIntervalSplit).where(
                models.OrderIntervalSplit.job_id == bill.job_id,
                models.OrderIntervalSplit.amount.is_not(None),
            )
        ).all()
        daily: dict[date, Decimal] = {}
        for sp in splits:
            day = sp.start_ts_local.date()
            daily[day] = daily.get(day, Decimal("0")) + (sp.amount or Decimal("0"))
        revision_ids = sorted(
            r for r in db.scalars(
                select(models.JobInput.revision_id).where(
                    models.JobInput.job_id == bill.job_id,
                    models.JobInput.revision_id.is_not(None),
                )
            ).all() if r is not None
        )
        out.append(MonthInput(month, bill.id, bill.job_id, dict(sorted(daily.items())),
                              revision_ids))
    return out


def build_snapshot(months: list[MonthInput]) -> str:
    return canonical_rules({
        "bills": [
            {
                "month": m.month, "bill_id": m.bill_id, "job_id": m.job_id,
                "revision_ids": m.revision_ids,
                "daily_revenue": {d.isoformat(): str(v) for d, v in m.daily_revenue.items()},
            }
            for m in months
        ]
    })


# --------------------------- 三段式运算 ---------------------------

def _tiered_share(revenue: Decimal, tiers: list[dict]) -> Decimal:
    """按分段（阶梯）比例计算分成：每一段只对落在该段上限内的基数计各自比例。"""
    share = Decimal("0")
    lower = Decimal("0")
    for t in tiers:
        upper = None if t["upper_revenue"] is None else Decimal(t["upper_revenue"])
        rate = Decimal(t["rate"])
        portion_hi = revenue if upper is None else min(revenue, upper)
        if portion_hi > lower:
            share += (portion_hi - lower) * rate
        if upper is None or revenue <= upper:
            break
        lower = upper
    return share


@dataclass
class MonthResult:
    base_revenue: Decimal
    revenue: Decimal                 # 新条款/当前规则下的分段收益（月级舍入后）
    fixed_fee: Decimal
    deduction: Decimal
    minimum: Decimal
    minimum_applied: bool
    in_promo: bool
    payable: Decimal
    rounding_diff: Decimal
    clarify: bool
    clarify_reason: str | None
    detail: dict


def evaluate_month(rules: dict, contract: models.RevenueContract,
                   month_input: MonthInput) -> MonthResult:
    m_start, m_end = month_bounds(month_input.month)
    days_in_month = (m_end - m_start).days + 1

    # 合同生效范围对该月的覆盖（生效时间是实际生效日，不是签署时间）
    eff_lo = max(m_start, contract.effective_date or m_start)
    eff_hi = min(m_end, contract.end_date or m_end)
    covered_days = max(0, (eff_hi - eff_lo).days + 1)
    covered_fraction = Decimal(covered_days) / Decimal(days_in_month)

    # 优惠期落在覆盖范围内的天数
    promo_days = 0
    for p_start, p_end, _ in _windows(rules["promo_periods"]):
        promo_days += _intersect_days(eff_lo, eff_hi, p_start, p_end)
    in_promo = promo_days > 0

    # 第一步：分段（阶梯）收益，只统计生效范围内的日级金额
    in_range_daily = {d: v for d, v in month_input.daily_revenue.items() if eff_lo <= d <= eff_hi}
    base_revenue = quantize(sum(in_range_daily.values(), Decimal("0")), 4)
    tiers = rules["revenue_tiers"]
    daily_share_raw = {d: _tiered_share(v, tiers) for d, v in in_range_daily.items()}
    share_raw_monthly = sum(daily_share_raw.values(), Decimal("0"))
    share_monthly = money(share_raw_monthly)
    # 尾差：月度统一舍入 vs 逐日先舍入再合计（单独列出，不摊回订单）
    sum_daily_rounded = sum((money(v) for v in daily_share_raw.values()), Decimal("0"))
    rounding_diff = quantize(share_monthly - sum_daily_rounded, 4)

    fixed_fee = money(Decimal(rules["fixed_fee"]) * covered_fraction)

    # 第二步：允许抵扣（仅统计生效范围内、当月重叠的抵扣项，按天折算）
    deduction = Decimal("0")
    deduction_detail = []
    for d_start, d_end, item in _windows(rules["deductions"]):
        days = _intersect_days(eff_lo, eff_hi, d_start, d_end)
        if days:
            part = money(Decimal(item["monthly_amount"]) * Decimal(days) / Decimal(days_in_month))
            deduction += part
            deduction_detail.append({"name": item["name"], "days": days, "amount": str(part)})
    deduction = money(deduction)

    net_before_minimum = money(share_monthly + fixed_fee - deduction)

    # 第三步：保底比较
    minimum_fee = Decimal(rules["minimum_fee"])
    non_promo_days = max(0, covered_days - promo_days)
    # 待澄清：保底与优惠在同月互相覆盖，且合同没有写明谁优先
    clarify = (
        minimum_fee > 0 and promo_days > 0 and non_promo_days > 0
        and rules.get("priority") is None
    )
    clarify_reason = None
    if clarify:
        clarify_reason = (
            f"该月优惠期覆盖 {promo_days} 天、其余 {non_promo_days} 天仍在保底范围，"
            "合同未声明保底与优惠的优先级，停止自动判定，请先澄清"
        )
        minimum_effective = money(minimum_fee * covered_fraction)
        minimum_applied = False
        payable = money(net_before_minimum)  # 保留试算过程值，但该月不可批准
    else:
        if rules.get("priority") == PRIORITY_MINIMUM:
            minimum_effective = money(minimum_fee * covered_fraction)
        else:
            minimum_effective = money(
                minimum_fee * Decimal(non_promo_days) / Decimal(days_in_month)
            )
        minimum_applied = minimum_effective > net_before_minimum
        payable = money(max(minimum_effective, net_before_minimum))

    detail = {
        "covered_days": covered_days,
        "days_in_month": days_in_month,
        "promo_days": promo_days,
        "daily_revenue": {d.isoformat(): str(v) for d, v in in_range_daily.items()},
        "daily_share_raw": {d.isoformat(): str(money(v)) for d, v in daily_share_raw.items()},
        "share_raw_monthly": str(share_raw_monthly),
        "share_monthly_rounded": str(share_monthly),
        "sum_daily_rounded": str(sum_daily_rounded),
        "deductions": deduction_detail,
        "net_before_minimum": str(net_before_minimum),
        "minimum_effective": str(minimum_effective),
        "computation_order": "1.分段收益 -> 2.允许抵扣 -> 3.保底比较",
    }
    return MonthResult(
        base_revenue=base_revenue,
        revenue=share_monthly,
        fixed_fee=fixed_fee,
        deduction=deduction,
        minimum=minimum_effective,
        minimum_applied=minimum_applied,
        in_promo=in_promo,
        payable=payable,
        rounding_diff=rounding_diff,
        clarify=clarify,
        clarify_reason=clarify_reason,
        detail=detail,
    )


# --------------------------- 试算创建与状态刷新 ---------------------------

def _periods_overlap(a0: date, a1: date, b0: date, b1: date) -> bool:
    return max(a0, b0) <= min(a1, b1)


def _overlapping_approved_trials(db: Session, exclude_trial_id: int,
                                 site_id: int, p_start: date, p_end: date,
                                 ) -> list[models.ContractTrial]:
    rows = db.scalars(
        select(models.ContractTrial).where(
            models.ContractTrial.site_id == site_id,
            models.ContractTrial.status == models.TRIAL_APPROVED,
            models.ContractTrial.id != exclude_trial_id,
        )
    ).all()
    return [t for t in rows if _periods_overlap(p_start, p_end, t.period_start, t.period_end)]


def classify_trial(db: Session, trial: models.ContractTrial) -> str:
    """依据固定输入的当前状态重新判定可批准性；已落库的试算数字保持不变、仍可查看。"""
    if trial.status in (models.TRIAL_APPROVED, models.TRIAL_REJECTED,
                        models.TRIAL_SUPERSEDED):
        return trial.status

    candidate = db.get(models.RevenueContract, trial.candidate_contract_id)
    # 草稿再编辑：规则哈希不再匹配 → 失去批准资格
    if candidate.rules_hash != trial.candidate_rules_hash:
        trial.status = models.TRIAL_STALE
        trial.status_reason = "合同草稿在试算后已再次编辑，试算输入与当前草稿不一致"
        return trial.status

    # 审批期间电量更正 / 账单版本变化 → 相关月份失效
    changed: list[str] = []
    snapshot = json.loads(trial.bill_snapshot_json)
    all_bills = db.scalars(
        select(models.Bill).where(models.Bill.site_id == trial.site_id)
    ).all()
    for item in snapshot["bills"]:
        bill = db.get(models.Bill, item["bill_id"])
        month = item["month"]
        if bill is None:
            changed.append(f"{month}(账单已删除)")
            continue
        # 同月出现更新的作业账单（例如电量更正后重新核算）→ 账单版本已变化
        same_month = [b for b in all_bills if _bill_month(db, b) == month]
        latest_bill = max(same_month, key=lambda b: b.job_id)
        if latest_bill.id != bill.id:
            changed.append(f"{month}(账单版本已更新)")
            continue
        job = db.get(models.ReconciliationJob, bill.job_id)
        if check_new_revisions(db, job):
            changed.append(f"{month}(电量等依据出现更正)")
    if changed:
        trial.status = models.TRIAL_INVALIDATED
        trial.status_reason = "审批期间输入发生变化：" + "、".join(changed) + "，请重新发起试算"
        return trial.status

    # 相同账期已有批准方案且未确定采用关系
    overlaps = _overlapping_approved_trials(
        db, trial.id, trial.site_id, trial.period_start, trial.period_end
    )
    if overlaps:
        trial.status = models.TRIAL_BLOCKED_OVERLAP
        trial.status_reason = (
            "与已批准试算 " + "、".join(t.trial_no for t in overlaps)
            + " 覆盖相同账期，批准前须声明采用关系（supersedes）"
        )
        return trial.status

    if any(line.line_status == models.LINE_CLARIFY for line in trial.lines):
        trial.status = models.TRIAL_NEEDS_CLARIFICATION
        trial.status_reason = "存在保底与优惠优先级未澄清的月份"
        return trial.status

    trial.status = models.TRIAL_CALCULATED
    trial.status_reason = None
    return trial.status


def create_trial(db: Session, site: models.Site, baseline: models.RevenueContract,
                 candidate: models.RevenueContract, period_start: date,
                 period_end: date, username: str) -> models.ContractTrial:
    if baseline.site_id != site.id or candidate.site_id != site.id:
        raise ContractTrialError("两份合同必须属于同一站点")
    if candidate.status != models.CONTRACT_DRAFT:
        raise ContractTrialError("只有草稿合同可以作为新条款发起试算")
    if baseline.id == candidate.id:
        raise ContractTrialError("原合同与新条款不能是同一版本")
    if candidate.effective_date is None:
        raise ContractTrialError("新条款缺少实际生效日期，不能试算")
    # 期间对齐到月边界
    p_start = period_start.replace(day=1)
    last_day = calendar.monthrange(period_end.year, period_end.month)[1]
    p_end = period_end.replace(day=last_day)

    base_rules = validate_rules(json.loads(baseline.rules_json))
    cand_rules = validate_rules(json.loads(candidate.rules_json))
    months = gather_month_inputs(db, site.id, p_start, p_end)
    if not months:
        raise ContractTrialError("指定期间内没有可试算的账单，请先生成核算作业")

    trial = models.ContractTrial(
        site_id=site.id,
        trial_no=(
            f"TRL-{site.code}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
            f"-{candidate.version_no}-{uuid4().hex[:8]}"
        ),
        baseline_contract_id=baseline.id,
        candidate_contract_id=candidate.id,
        baseline_rules_json=canonical_rules(base_rules),
        candidate_rules_json=canonical_rules(cand_rules),
        candidate_rules_hash=rules_hash(cand_rules),
        period_start=p_start,
        period_end=p_end,
        bill_snapshot_json=build_snapshot(months),
        created_by=username,
    )
    db.add(trial)
    db.flush()

    for mi in months:
        base_r = evaluate_month(base_rules, baseline, mi)
        cand_r = evaluate_month(cand_rules, candidate, mi)
        line = models.ContractTrialLine(
            trial_id=trial.id,
            bill_id=mi.bill_id,
            period_month=mi.month,
            line_status=(models.LINE_CLARIFY if cand_r.clarify else models.TRIAL_CALCULATED),
            clarify_reason=cand_r.clarify_reason,
            base_revenue=base_r.base_revenue,
            cand_revenue=cand_r.base_revenue,
            base_deduction=base_r.deduction,
            cand_deduction=cand_r.deduction,
            base_minimum=base_r.minimum,
            cand_minimum=cand_r.minimum,
            base_minimum_applied=base_r.minimum_applied,
            cand_minimum_applied=cand_r.minimum_applied,
            base_fixed_fee=base_r.fixed_fee,
            cand_fixed_fee=cand_r.fixed_fee,
            base_in_promo=base_r.in_promo,
            cand_in_promo=cand_r.in_promo,
            base_payable=base_r.payable,
            cand_payable=cand_r.payable,
            delta=quantize(cand_r.payable - base_r.payable, 4),
            base_rounding_diff=base_r.rounding_diff,
            cand_rounding_diff=cand_r.rounding_diff,
            detail_json=canonical_rules({
                "baseline": base_r.detail,
                "candidate": cand_r.detail,
            }),
        )
        db.add(line)
    db.flush()
    classify_trial(db, trial)
    return trial


# --------------------------- 审批与采用关系 ---------------------------

def approve_trial(db: Session, trial: models.ContractTrial, period_start: date,
                  period_end: date, note: str | None,
                  supersedes_trial_id: int | None, username: str
                  ) -> models.ContractApproval:
    classify_trial(db, trial)

    # 相同账期被已批准方案占用：必须在本次批准里声明采用关系才能解除阻塞
    if trial.status == models.TRIAL_BLOCKED_OVERLAP:
        overlaps = _overlapping_approved_trials(
            db, trial.id, trial.site_id, trial.period_start, trial.period_end
        )
        if supersedes_trial_id is None or supersedes_trial_id not in {t.id for t in overlaps}:
            raise ContractTrialError(
                "覆盖相同账期的已批准方案：" + "、".join(t.trial_no for t in overlaps)
                + "；必须先确定采用关系，在 supersedes_trial_id 中声明被替代的试算"
            )
        if len(overlaps) > 1:
            raise ContractTrialError(
                "存在多份重叠的已批准方案，请逐份确定采用关系后再批准："
                + "、".join(t.trial_no for t in overlaps)
            )
        old = overlaps[0]
        old.status = models.TRIAL_SUPERSEDED
        old.status_reason = f"被试算 {trial.trial_no} 批准的新方案替代"
        db.flush()  # SessionLocal 关闭了 autoflush，需显式落库后重叠重查才能看到新状态
        classify_trial(db, trial)  # 解除重叠后重新分类

    if trial.status != models.TRIAL_CALCULATED:
        raise ContractTrialError(f"当前试算状态为 {trial.status}，不具备批准资格：{trial.status_reason or ''}")
    if not (trial.period_start <= period_start <= period_end <= trial.period_end):
        raise ContractTrialError("批准期间必须落在试算期间内")

    # 安全兜底：再次核对批准子期间内的重叠（采用关系已在上文处理）
    overlaps = _overlapping_approved_trials(
        db, trial.id, trial.site_id, period_start, period_end
    )
    if overlaps:
        raise ContractTrialError(
            "批准期间仍与已批准方案重叠：" + "、".join(t.trial_no for t in overlaps)
        )

    approved_delta = quantize(sum(
        (line.delta for line in trial.lines
         if period_start.strftime("%Y-%m") <= line.period_month <= period_end.strftime("%Y-%m")),
        Decimal("0")), 4)
    approval = models.ContractApproval(
        trial_id=trial.id,
        site_id=trial.site_id,
        decision=models.APPROVAL_APPROVED,
        period_start=period_start,
        period_end=period_end,
        approved_delta=approved_delta,
        note=note,
        supersedes_trial_id=supersedes_trial_id,
        approved_by=username,
    )
    db.add(approval)
    trial.status = models.TRIAL_APPROVED
    trial.status_reason = None
    db.flush()
    return approval


def reject_trial(db: Session, trial: models.ContractTrial, note: str,
                 username: str) -> models.ContractTrial:
    if trial.status in (models.TRIAL_APPROVED, models.TRIAL_REJECTED):
        raise ContractTrialError("已终结的试算不能再驳回")
    trial.status = models.TRIAL_REJECTED
    trial.status_reason = f"审批驳回：{note}"
    db.flush()
    return trial


# --------------------------- 账单合同视图与应付统计 ---------------------------

def bill_contract_view(db: Session, bill: models.Bill) -> dict:
    """原合同结果 / 新条款结果 / 实际批准调整额三数分列；未批准试算不进应付统计。"""
    lines = db.scalars(
        select(models.ContractTrialLine)
        .join(models.ContractTrial, models.ContractTrial.id == models.ContractTrialLine.trial_id)
        .where(models.ContractTrialLine.bill_id == bill.id)
        .order_by(models.ContractTrial.id.desc())
    ).all()
    latest = lines[0] if lines else None
    approved_delta = Decimal("0")
    approved_records: list[dict] = []
    for line in lines:
        trial = db.get(models.ContractTrial, line.trial_id)
        if trial.status != models.TRIAL_APPROVED:
            continue  # 未批准（含 stale/invalidated/待澄清）的试算不进入应付统计
        for ap in db.scalars(
            select(models.ContractApproval).where(
                models.ContractApproval.trial_id == trial.id,
                models.ContractApproval.decision == models.APPROVAL_APPROVED,
            )
        ).all():
            if ap.period_start.strftime("%Y-%m") <= line.period_month <= ap.period_end.strftime("%Y-%m"):
                approved_delta += line.delta
                approved_records.append({
                    "trial_no": trial.trial_no,
                    "approval_id": ap.id,
                    "delta": str(line.delta),
                    "period": line.period_month,
                })

    original_payable = latest.base_payable if latest else None
    candidate_payable = latest.cand_payable if latest else None
    approved_delta = quantize(approved_delta, 4)
    effective_payable = (
        quantize(original_payable + approved_delta, 4) if original_payable is not None else None
    )
    return {
        "bill_id": bill.id,
        "bill_no": bill.bill_no,
        "original_contract_payable": str(original_payable) if original_payable is not None else None,
        "new_terms_payable": str(candidate_payable) if candidate_payable is not None else None,
        "approved_adjustment": str(approved_delta),
        "effective_payable": str(effective_payable) if effective_payable is not None else None,
        "latest_trial_id": latest.trial_id if latest else None,
        "latest_trial_status": (db.get(models.ContractTrial, latest.trial_id).status
                                if latest else None),
        "approved_records": approved_records,
    }


def site_payable_stats(db: Session, site_id: int) -> dict:
    bills = db.scalars(
        select(models.Bill).where(models.Bill.site_id == site_id)
        .order_by(models.Bill.id)
    ).all()
    total_original = Decimal("0")
    total_approved_adjustment = Decimal("0")
    total_effective = Decimal("0")
    billed = 0
    for bill in bills:
        view = bill_contract_view(db, bill)
        if view["original_contract_payable"] is None:
            continue
        billed += 1
        total_original += Decimal(view["original_contract_payable"])
        total_approved_adjustment += Decimal(view["approved_adjustment"])
        total_effective += Decimal(view["effective_payable"])
    return {
        "site_id": site_id,
        "bill_count_with_contract": billed,
        "original_contract_payable": str(quantize(total_original, 4)),
        "approved_adjustment": str(quantize(total_approved_adjustment, 4)),
        # 关键口径：只含实际批准调整，未批准试算不计入
        "effective_payable": str(quantize(total_effective, 4)),
    }
