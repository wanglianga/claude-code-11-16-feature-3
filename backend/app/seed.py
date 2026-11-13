"""初始化表结构、账号与站点，并导入样例计量文件。"""
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from .database import Base, SessionLocal, engine
from . import models
from .security import hash_password
from .services.importer import ImportError_, import_file
from .services.contract_svc import canonical_rules, rules_hash, validate_rules

SAMPLE_DIR = Path(__file__).parent.parent / "sample_data"
STORAGE_DIR = "data/originals"

USERS = [
    ("finance", "Finance@123", "平台财务", models.ROLE_FINANCE, []),
    ("operator", "Operator@123", "运营专员", models.ROLE_OPERATOR, []),
    ("property_sh", "Property@123", "上海物业", models.ROLE_PROPERTY, ["S01"]),
    ("property_xj", "Property@123", "乌鲁木齐物业", models.ROLE_PROPERTY, ["S02"]),
]

IMPORT_PLAN = {
    "S01": [
        ("tariff", "tariffs.csv"),
        ("master_reading", "master_readings.csv"),
        ("device_reading", "device_readings.csv"),
        ("public_load", "public_loads.csv"),
        ("order", "orders_v1.csv"),
        ("order", "orders_v2_backfill.csv"),
    ],
    "S02": [
        ("tariff", "tariffs.csv"),
        ("master_reading", "master_readings.csv"),
        ("device_reading", "device_readings.csv"),
        ("replacement", "replacements.csv"),
        ("public_load", "public_loads.csv"),
        ("order", "orders.csv"),
    ],
}


def _contract(db, site, contract_no, version_no, name, status, raw_rules,
              effective_date, end_date, signed_at):
    rules = validate_rules(raw_rules)
    return models.RevenueContract(
        site_id=site.id,
        contract_no=contract_no,
        version_no=version_no,
        name=name,
        status=status,
        rules_json=canonical_rules(rules),
        rules_hash=rules_hash(rules),
        effective_date=effective_date,
        end_date=end_date,
        signed_at=signed_at,
        created_by="seed",
    )


def seed_contracts(db, sites):
    """幂等播种分成合同：每站点一份已生效 v1 + 一份变更草稿 v2。

    S01 草稿含“保底 + 优惠期重叠且未写优先级”，试算 10 月会停在待澄清；
    S02 草稿明确 minimum_first，试算可直接进入批准流程。
    签署时间（signed_at）均早于实际生效日期（effective_date），二者分别留痕。
    """
    if db.scalar(select(models.RevenueContract).limit(1)):
        return

    from datetime import date, datetime

    sh = sites["S01"]
    sh_v1 = {
        "fixed_fee": "1000", "minimum_fee": "2000",
        "revenue_tiers": [
            {"upper_revenue": "5000", "rate": "0.20"},
            {"upper_revenue": "20000", "rate": "0.15"},
            {"upper_revenue": None, "rate": "0.10"},
        ],
        "deductions": [
            {"name": "设备运维抵扣", "monthly_amount": "300",
             "start_date": "2026-09-01", "end_date": None},
        ],
        "promo_periods": [], "priority": None,
    }
    sh_v2 = {
        # 物业提出：提高保底到 2500、下调阶梯比例，并申请 10 月上半月优惠期（未写优先级）
        "fixed_fee": "1200", "minimum_fee": "2500",
        "revenue_tiers": [
            {"upper_revenue": "5000", "rate": "0.18"},
            {"upper_revenue": None, "rate": "0.12"},
        ],
        "deductions": [
            {"name": "设备运维抵扣", "monthly_amount": "300",
             "start_date": "2026-09-01", "end_date": None},
        ],
        "promo_periods": [
            {"start_date": "2026-10-01", "end_date": "2026-10-15"},
        ],
        "priority": None,
    }
    db.add(_contract(db, sh, "RC-SH-01", 1, "上海站点分成合同（现行）",
                     models.CONTRACT_ACTIVE, sh_v1,
                     date(2026, 9, 1), None, datetime(2026, 8, 20, 10, 0)))
    db.add(_contract(db, sh, "RC-SH-01", 2, "上海站点分成合同变更草稿（保底+优惠期）",
                     models.CONTRACT_DRAFT, sh_v2,
                     date(2026, 10, 1), None, datetime(2026, 9, 25, 14, 30)))

    xj = sites["S02"]
    xj_v1 = {
        "fixed_fee": "800", "minimum_fee": "1500",
        "revenue_tiers": [
            {"upper_revenue": "3000", "rate": "0.25"},
            {"upper_revenue": None, "rate": "0.18"},
        ],
        "deductions": [], "promo_periods": [], "priority": None,
    }
    xj_v2 = {
        # 合同明确写了保底优先，因此优惠期与保底重叠不会进入待澄清
        "fixed_fee": "900", "minimum_fee": "1800",
        "revenue_tiers": [
            {"upper_revenue": "3000", "rate": "0.22"},
            {"upper_revenue": None, "rate": "0.16"},
        ],
        "deductions": [],
        "promo_periods": [
            {"start_date": "2026-10-10", "end_date": "2026-10-20"},
        ],
        "priority": "minimum_first",
    }
    db.add(_contract(db, xj, "RC-XJ-01", 1, "乌鲁木齐站点分成合同（现行）",
                     models.CONTRACT_ACTIVE, xj_v1,
                     date(2026, 9, 1), None, datetime(2026, 8, 22, 9, 0)))
    db.add(_contract(db, xj, "RC-XJ-01", 2, "乌鲁木齐站点分成合同变更草稿（优惠期+保底优先）",
                     models.CONTRACT_DRAFT, xj_v2,
                     date(2026, 10, 1), None, datetime(2026, 9, 26, 16, 0)))
    db.flush()
    print("分成合同种子数据初始化完成（2 站点 × 2 版本）")


def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.scalar(select(models.Site).limit(1)):
            print("种子数据已存在，跳过")
            return
        sites = {
            "S01": models.Site(code="S01", name="上海虹桥充电站", timezone="Asia/Shanghai",
                               diff_threshold_pct=Decimal("0.02")),
            "S02": models.Site(code="S02", name="乌鲁木齐经开充电站", timezone="Asia/Urumqi",
                               diff_threshold_pct=Decimal("0.02")),
        }
        db.add_all(sites.values())
        db.flush()
        for username, pwd, display, role, site_codes in USERS:
            user = models.User(
                username=username, password_hash=hash_password(pwd),
                display_name=display, role=role,
            )
            db.add(user)
            db.flush()
            for code in site_codes:
                db.add(models.UserSite(user_id=user.id, site_id=sites[code].id))
        db.flush()

        for code, plan in IMPORT_PLAN.items():
            site = sites[code]
            for source_type, filename in plan:
                path = SAMPLE_DIR / code / filename
                content = path.read_bytes()
                result = import_file(
                    db, site, source_type, filename, content, "seed", STORAGE_DIR
                )
                print(f"[{code}] {filename}: {result['summary']}")
        seed_contracts(db, sites)
        db.commit()
        print("种子数据初始化完成")
    except (Exception, ImportError_):
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
