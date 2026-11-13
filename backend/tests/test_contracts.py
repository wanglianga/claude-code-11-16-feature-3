"""分成合同变更追溯试算：规则结构化校验、三段式运算顺序、待澄清、舍入尾差、
草稿再编辑失效、电量更正失效、账单版本更新失效、重叠采用关系、应付统计口径、角色权限。"""
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app import models
from app.services.reconciliation import run_reconciliation
from app.services import contract_svc as svc
from app.services.importer import import_file

client = TestClient(app)
CYCLE = (datetime(2026, 10, 1), datetime(2026, 11, 1))


def _login(username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fin_headers():
    return _auth(_login("finance", "Finance@123"))


def _site(db, code):
    return db.scalar(select(models.Site).where(models.Site.code == code))


def _contracts(db, site_id):
    rows = db.scalars(select(models.RevenueContract).where(
        models.RevenueContract.site_id == site_id).order_by(
        models.RevenueContract.version_no)).all()
    return {c.version_no: c for c in rows}


def _make_job(db, code):
    site = _site(db, code)
    job = run_reconciliation(db, site, *CYCLE, "pytest")
    db.commit()
    return job


def _rules(**over):
    base = {
        "fixed_fee": "0", "minimum_fee": "0",
        "revenue_tiers": [{"upper_revenue": None, "rate": "0.2"}],
        "deductions": [], "promo_periods": [], "priority": None,
    }
    base.update(over)
    return base


# --------------------------- 规则只能是结构化数据 ---------------------------

def test_rules_reject_code_and_bad_structure():
    # 未知字段 / 越界比例 / 非递增上限 / 非法优先级 一律拒绝
    with pytest.raises(svc.ContractRuleError):
        svc.validate_rules(_rules(script="print(1)"))
    with pytest.raises(svc.ContractRuleError):
        svc.validate_rules(_rules(revenue_tiers=[{"upper_revenue": None, "rate": "1.5"}]))
    with pytest.raises(svc.ContractRuleError):
        svc.validate_rules(_rules(revenue_tiers=[
            {"upper_revenue": "100", "rate": "0.2"},
            {"upper_revenue": "50", "rate": "0.1"},
            {"upper_revenue": None, "rate": "0.1"}]))
    with pytest.raises(svc.ContractRuleError):
        svc.validate_rules(_rules(priority="who_benefits_more"))
    # 只允许基础类型：函数等可执行内容无法通过结构校验
    with pytest.raises(svc.ContractRuleError):
        svc.validate_rules(_rules(revenue_tiers=[{"upper_revenue": None, "rate": lambda: 1}]))
    # 合法规则被规范化为固定结构
    clean = svc.validate_rules(_rules())
    assert set(clean) == {"fixed_fee", "minimum_fee", "revenue_tiers",
                         "deductions", "promo_periods", "priority"}


def test_trial_candidate_must_be_draft(fin_headers):
    db = SessionLocal()
    try:
        site = _site(db, "S01")
        cs = _contracts(db, site.id)
        # 用已生效的 v1 同时作为基线与候选 → 拒绝
        r = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id,
            "candidate_contract_id": cs[1].id,
            "period_start": "2026-10-01", "period_end": "2026-10-31"})
        assert r.status_code == 400
    finally:
        db.close()


# --------------------------- 三段式运算顺序与阶梯 ---------------------------

def test_computation_order_revenue_deduction_then_minimum():
    db = SessionLocal()
    try:
        site = _site(db, "S01")
        job = _make_job(db, "S01")
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        months = svc.gather_month_inputs(db, site.id, date(2026, 10, 1), date(2026, 10, 31))
        assert months and months[0].bill_id == bill.id

        # 自定义规则：单一比例 0.2，固定费 100，抵扣 50，保底 2000
        rules = svc.validate_rules(_rules(
            fixed_fee="100", minimum_fee="2000",
            revenue_tiers=[{"upper_revenue": None, "rate": "0.2"}],
            deductions=[{"name": "抵扣", "monthly_amount": "50",
                         "start_date": "2026-10-01", "end_date": None}]))
        contract = models.RevenueContract(site_id=site.id, contract_no="TMP", version_no=1,
                                          effective_date=date(2026, 10, 1), end_date=None)
        result = svc.evaluate_month(rules, contract, months[0])
        revenue = bill.charging_amount
        expect_share = svc.money(revenue * Decimal("0.2"))
        expect_net = svc.money(expect_share + Decimal("100") - Decimal("50"))
        # 顺序：先分段收益，再扣抵扣，最后与保底比较；保底 2000 更高 → 应付=2000
        assert result.revenue == expect_share
        assert result.deduction == Decimal("50.0000")
        assert result.minimum_applied is True
        assert result.payable == Decimal("2000.0000")
        assert result.detail["computation_order"].startswith("1.分段收益")
    finally:
        db.close()


def test_tiered_share_uses_bracket_rates():
    # 6000 元基数：0~5000 按 0.2，5000~6000 按 0.1
    tiers = [{"upper_revenue": "5000", "rate": "0.2"},
             {"upper_revenue": None, "rate": "0.1"}]
    assert svc._tiered_share(Decimal("6000"), tiers) == Decimal("1100")
    assert svc._tiered_share(Decimal("4000"), tiers) == Decimal("800")


def test_rounding_diff_is_listed_separately_not_allocated_to_orders():
    # 构造日级金额，使“月度一次舍入”与“逐日先舍入再合计”产生尾差
    daily = {date(2026, 10, 1): Decimal("10.0001"),
             date(2026, 10, 2): Decimal("20.0001"),
             date(2026, 10, 3): Decimal("30.0001")}
    mi = svc.MonthInput("2026-10", bill_id=1, job_id=1, daily_revenue=daily, revision_ids=[])
    rules = svc.validate_rules(_rules(
        revenue_tiers=[{"upper_revenue": None, "rate": "0.3333"}]))
    contract = models.RevenueContract(site_id=1, contract_no="TMP", version_no=1,
                                      effective_date=date(2026, 10, 1), end_date=None)
    result = svc.evaluate_month(rules, contract, mi)
    # 尾差恒等式：月度舍入值 = 逐日舍入合计 + 尾差；尾差单列，不回摊到任何订单
    assert result.revenue == Decimal(result.detail["share_monthly_rounded"])
    assert Decimal(result.detail["share_monthly_rounded"]) == (
        Decimal(result.detail["sum_daily_rounded"]) + result.rounding_diff)
    assert result.rounding_diff != 0  # 该组数据确实产生尾差


# --------------------------- 保底与优惠无优先级 → 待澄清 ---------------------------

def test_missing_priority_halts_at_clarification_and_blocks_approval(fin_headers):
    db = SessionLocal()
    try:
        _make_job(db, "S01")
        site = _site(db, "S01")
        cs = _contracts(db, site.id)
        r = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": cs[2].id,
            "period_start": "2026-10-01", "period_end": "2026-10-31"})
        assert r.status_code == 200
        trial = r.json()
        assert trial["status"] == models.TRIAL_NEEDS_CLARIFICATION
        line = trial["lines"][0]
        assert line["line_status"] == models.LINE_CLARIFY and line["clarify_reason"]
        # 待澄清试算不具备批准资格
        ar = client.post(f"/api/contracts/trials/{trial['id']}/approve",
                         headers=fin_headers,
                         json={"period_start": "2026-10-01", "period_end": "2026-10-31"})
        assert ar.status_code == 409 and "待澄清" in ar.json()["detail"] or ar.status_code == 409
    finally:
        db.close()


def test_explicit_priority_allows_approval_and_three_value_view(fin_headers):
    db = SessionLocal()
    try:
        _make_job(db, "S02")
        site = _site(db, "S02")
        cs = _contracts(db, site.id)
        r = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": cs[2].id,
            "period_start": "2026-10-01", "period_end": "2026-10-31"})
        trial = r.json()
        assert trial["status"] == models.TRIAL_CALCULATED
        line = trial["lines"][0]
        bill_id = line["bill_id"]
        ar = client.post(f"/api/contracts/trials/{trial['id']}/approve", headers=fin_headers,
                         json={"period_start": "2026-10-01", "period_end": "2026-10-31",
                               "note": "批准10月差额"})
        assert ar.status_code == 200
        assert Decimal(ar.json()["approved_delta"]) == Decimal(line["delta"])
        # 账单三数视图：原合同 / 新条款 / 实际批准调整
        view = client.get(f"/api/contracts/bills/{bill_id}/view", headers=fin_headers).json()
        assert view["original_contract_payable"] == line["base_payable"]
        assert view["new_terms_payable"] == line["cand_payable"]
        assert Decimal(view["approved_adjustment"]) == Decimal(line["delta"])
        assert Decimal(view["effective_payable"]) == (
            Decimal(line["base_payable"]) + Decimal(line["delta"]))
    finally:
        db.close()


# --------------------------- 草稿再编辑 → 旧试算 stale ---------------------------

def test_draft_reedit_makes_trial_stale_but_viewable(fin_headers):
    db = SessionLocal()
    try:
        _make_job(db, "S02")
        site = _site(db, "S02")
        cs = _contracts(db, site.id)
        trial = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": cs[2].id,
            "period_start": "2026-10-01", "period_end": "2026-10-31"}).json()
        assert trial["status"] == models.TRIAL_CALCULATED
        # 草稿再次编辑（试算固定输入被改变）
        pr = client.patch(f"/api/contracts/{cs[2].id}", headers=fin_headers, json={
            "rules": _rules(minimum_fee="9999",
                            revenue_tiers=[{"upper_revenue": None, "rate": "0.16"}],
                            promo_periods=[{"start_date": "2026-10-10",
                                            "end_date": "2026-10-20"}],
                            priority="minimum_first")})
        assert pr.status_code == 200
        refreshed = client.get(f"/api/contracts/trials/{trial['id']}",
                               headers=fin_headers).json()
        assert refreshed["status"] == models.TRIAL_STALE  # 失去批准资格
        assert refreshed["lines"][0]["cand_payable"] == trial["lines"][0]["cand_payable"]  # 数字不变、仍可查看
        ar = client.post(f"/api/contracts/trials/{trial['id']}/approve", headers=fin_headers,
                         json={"period_start": "2026-10-01", "period_end": "2026-10-31"})
        assert ar.status_code == 409
    finally:
        db.close()


# --------------------------- 审批期间电量更正 → invalidated ---------------------------

def test_energy_correction_invalidates_trial(fin_headers):
    db = SessionLocal()
    try:
        job = _make_job(db, "S01")
        site = _site(db, "S01")
        cs = _contracts(db, site.id)
        # 用一份明确优先级的临时草稿，保证试算本身可批准，仅因更正而失效
        tmp = client.post("/api/contracts", headers=fin_headers, json={
            "site_id": site.id, "contract_no": "RC-SH-TMP", "name": "临时可批草稿",
            "effective_date": "2026-10-01",
            "rules": _rules(minimum_fee="100",
                            revenue_tiers=[{"upper_revenue": None, "rate": "0.2"}],
                            promo_periods=[{"start_date": "2026-10-10",
                                            "end_date": "2026-10-20"}],
                            priority="minimum_first")}).json()
        trial = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": tmp["id"],
            "period_start": "2026-10-01", "period_end": "2026-10-31"}).json()
        assert trial["status"] == models.TRIAL_CALCULATED
        # 审批期间到达电量更正（作业采用后的同号修订 → blocked_adopted）
        content = (Path(__file__).resolve().parent.parent
                   / "sample_data" / "S01" / "orders_v3_after_adopt.csv").read_bytes()
        import_file(db, site, "order", "orders_v3_after_adopt.csv", content,
                    "pytest", "data/test_originals")
        db.commit()
        refreshed = client.post(f"/api/contracts/trials/{trial['id']}/refresh",
                                headers=fin_headers).json()
        assert refreshed["status"] == models.TRIAL_INVALIDATED
        assert "更正" in refreshed["status_reason"]
        # 原试算仍可查看，但不能批准
        assert client.get(f"/api/contracts/trials/{trial['id']}",
                          headers=fin_headers).status_code == 200
        ar = client.post(f"/api/contracts/trials/{trial['id']}/approve",
                         headers=fin_headers,
                         json={"period_start": "2026-10-01", "period_end": "2026-10-31"})
        assert ar.status_code == 409
    finally:
        db.close()


def test_new_bill_version_same_month_invalidates_trial(fin_headers):
    db = SessionLocal()
    try:
        job1 = _make_job(db, "S02")
        site = _site(db, "S02")
        cs = _contracts(db, site.id)
        trial = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": cs[2].id,
            "period_start": "2026-10-01", "period_end": "2026-10-31"}).json()
        # 同账期重新核算，产生新版本账单
        run_reconciliation(db, site, *CYCLE, "pytest", rerun_of_job_id=job1.id)
        db.commit()
        refreshed = client.post(f"/api/contracts/trials/{trial['id']}/refresh",
                                headers=fin_headers).json()
        assert refreshed["status"] == models.TRIAL_INVALIDATED
        assert "账单版本已更新" in refreshed["status_reason"]
    finally:
        db.close()


# --------------------------- 相同账期重叠 → 必须确定采用关系 ---------------------------

def test_overlapping_trials_require_supersede_relation(fin_headers):
    db = SessionLocal()
    try:
        _make_job(db, "S02")
        site = _site(db, "S02")
        cs = _contracts(db, site.id)
        t1 = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": cs[2].id,
            "period_start": "2026-10-01", "period_end": "2026-10-31"}).json()
        client.post(f"/api/contracts/trials/{t1['id']}/approve", headers=fin_headers,
                    json={"period_start": "2026-10-01", "period_end": "2026-10-31"})
        # 第二份方案：另建草稿
        t2_contract = client.post("/api/contracts", headers=fin_headers, json={
            "site_id": site.id, "contract_no": "RC-XJ-02", "name": "第二套追溯方案",
            "effective_date": "2026-10-01",
            "rules": _rules(minimum_fee="1600",
                            revenue_tiers=[{"upper_revenue": None, "rate": "0.2"}])}).json()
        t2 = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": t2_contract["id"],
            "period_start": "2026-10-01", "period_end": "2026-10-31"}).json()
        assert t2["status"] == models.TRIAL_BLOCKED_OVERLAP
        # 不声明采用关系 → 拒绝
        bad = client.post(f"/api/contracts/trials/{t2['id']}/approve", headers=fin_headers,
                          json={"period_start": "2026-10-01", "period_end": "2026-10-31"})
        assert bad.status_code == 409 and "采用关系" in bad.json()["detail"]
        # 声明替代 t1 → 批准成功，t1 被标记 superseded
        ok = client.post(f"/api/contracts/trials/{t2['id']}/approve", headers=fin_headers,
                         json={"period_start": "2026-10-01", "period_end": "2026-10-31",
                               "supersedes_trial_id": t1["id"]})
        assert ok.status_code == 200
        old = client.get(f"/api/contracts/trials/{t1['id']}", headers=fin_headers).json()
        assert old["status"] == models.TRIAL_SUPERSEDED
    finally:
        db.close()


# --------------------------- 未批准试算不进应付统计 ---------------------------

def test_unapproved_trial_excluded_from_payable_stats(fin_headers):
    db = SessionLocal()
    try:
        _make_job(db, "S02")
        site = _site(db, "S02")
        cs = _contracts(db, site.id)
        trial = client.post("/api/contracts/trials", headers=fin_headers, json={
            "baseline_contract_id": cs[1].id, "candidate_contract_id": cs[2].id,
            "period_start": "2026-10-01", "period_end": "2026-10-31"}).json()
        # 试算存在但未批准：调整额必须为 0，应付=原合同结果，新条款差额不进统计
        pending = client.get(f"/api/contracts/sites/{site.id}/payable-stats",
                             headers=fin_headers).json()
        assert Decimal(pending["approved_adjustment"]) == Decimal("0")
        assert Decimal(pending["effective_payable"]) == Decimal(
            pending["original_contract_payable"])
        # 批准后：调整额=试算差额，应付=原合同结果+实际批准调整
        client.post(f"/api/contracts/trials/{trial['id']}/approve", headers=fin_headers,
                    json={"period_start": "2026-10-01", "period_end": "2026-10-31"})
        approved = client.get(f"/api/contracts/sites/{site.id}/payable-stats",
                              headers=fin_headers).json()
        delta = Decimal(trial["lines"][0]["delta"])
        assert Decimal(approved["approved_adjustment"]) == delta
        assert Decimal(approved["effective_payable"]) == (
            Decimal(approved["original_contract_payable"]) + delta)
    finally:
        db.close()


# --------------------------- 生效接替、签署/生效时间分列 ---------------------------

def test_activate_supersedes_and_keeps_signed_vs_effective(fin_headers):
    db = SessionLocal()
    try:
        site = _site(db, "S01")
        cs = _contracts(db, site.id)
        assert cs[1].status == models.CONTRACT_ACTIVE
        assert cs[2].signed_at.date() < cs[2].effective_date  # 签署时间不能代替生效时间
        r = client.post(f"/api/contracts/{cs[2].id}/activate", headers=fin_headers)
        assert r.status_code == 200 and r.json()["status"] == models.CONTRACT_ACTIVE
        db.expire_all()
        again = _contracts(db, site.id)
        assert again[1].status == models.CONTRACT_SUPERSEDED
        assert again[1].superseded_by_id == again[2].id
        # 生效版本不可再编辑
        pr = client.patch(f"/api/contracts/{cs[2].id}", headers=fin_headers,
                          json={"name": "改不了"})
        assert pr.status_code == 409
    finally:
        db.close()


# --------------------------- 角色与站点隔离 ---------------------------

def test_property_readonly_and_site_scoped():
    db = SessionLocal()
    try:
        _make_job(db, "S01")
        _make_job(db, "S02")
    finally:
        db.close()
    prop_sh = _auth(_login("property_sh", "Property@123"))
    # 物业只读：不能建合同、不能发起试算、不能审批
    r = client.post("/api/contracts", headers=prop_sh, json={
        "site_id": 1, "contract_no": "X", "name": "x", "effective_date": "2026-10-01",
        "rules": _rules()})
    assert r.status_code == 403
    # 物业只能看到授权站点（S01）的合同
    rows = client.get("/api/contracts", headers=prop_sh).json()
    assert rows and all(c["site_id"] == 1 for c in rows)
    # 跨站访问 S02 的合同被拒
    db = SessionLocal()
    try:
        xj = _contracts(db, _site(db, "S02").id)[1]
        assert client.get(f"/api/contracts/{xj.id}", headers=prop_sh).status_code == 403
    finally:
        db.close()
    # 运营同样只读
    op = _auth(_login("operator", "Operator@123"))
    assert client.post("/api/contracts/trials", headers=op, json={
        "baseline_contract_id": 1, "candidate_contract_id": 2,
        "period_start": "2026-10-01", "period_end": "2026-10-31"}).status_code == 403


def test_static_trial_route_not_captured_by_contract_id(fin_headers):
    # 回归：GET /contracts/trials 必须命中试算列表，不能被 /contracts/{contract_id} 当作 id 解析成 422
    r = client.get("/api/contracts/trials", headers=fin_headers)
    assert r.status_code == 200 and isinstance(r.json(), list)
    # 参数化合同 id 仍正常
    db = SessionLocal()
    try:
        c = db.scalar(select(models.RevenueContract))
        assert client.get(f"/api/contracts/{c.id}", headers=fin_headers).status_code == 200
    finally:
        db.close()
