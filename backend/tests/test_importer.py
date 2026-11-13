"""导入规则：原件留存、来源记录号去重、修订、已采用不覆盖。"""
from sqlalchemy import select

from app.database import SessionLocal
from app import models
from app.services.importer import import_file

SAMPLE = __import__("pathlib").Path(__file__).resolve().parent.parent / "sample_data"


def _read(code, name):
    return (SAMPLE / code / name).read_bytes()


def test_original_file_and_rows_retained():
    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        before = len(db.scalars(select(models.ImportFile)).all())
        import_file(db, site, "master_reading", "dup_master.csv",
                    _read("S01", "master_readings.csv"), "tester", "data/test_originals")
        db.commit()
        files = db.scalars(select(models.ImportFile)).all()
        assert len(files) == before + 1
        imp = files[-1]
        assert imp.sha256 and imp.row_count == 5
        rows = db.scalars(
            select(models.ImportRow).where(models.ImportRow.import_file_id == imp.id)
        ).all()
        # 物理行号从 2 开始，原始行文本完整保留
        assert [r.line_no for r in rows] == [2, 3, 4, 5, 6]
        assert all(r.raw_line.startswith("MR-SH-") for r in rows)
    finally:
        db.close()


def test_same_record_no_same_content_is_duplicate():
    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        # OR-SH-009 当前生效版本即补报文件内容，再次导入应识别为重复
        result = import_file(db, site, "order", "backfill_again.csv",
                             _read("S01", "orders_v2_backfill.csv"), "tester",
                             "data/test_originals")
        db.commit()
        assert result["summary"].get("duplicate", 0) == 1
        sr = db.scalar(select(models.SourceRecord).where(
            models.SourceRecord.site_id == site.id,
            models.SourceRecord.source_type == "order",
            models.SourceRecord.source_record_no == "OR-SH-009",
        ))
        revs = db.scalars(select(models.RecordRevision).where(
            models.RecordRevision.source_record_id == sr.id)).all()
        assert len(revs) == 2  # 原始 v1 + 补报 v2，重复导入不产生新版本
    finally:
        db.close()


def test_revision_before_adoption_supersedes_old():
    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        # OR-SH-009 已被 v2 补报修订，当前生效版本读数差值应为 40
        sr = db.scalar(select(models.SourceRecord).where(
            models.SourceRecord.site_id == site.id,
            models.SourceRecord.source_type == "order",
            models.SourceRecord.source_record_no == "OR-SH-009",
        ))
        revs = db.scalars(select(models.RecordRevision).where(
            models.RecordRevision.source_record_id == sr.id)
            .order_by(models.RecordRevision.revision_no)).all()
        assert [r.status for r in revs] == ["applied", "applied"]
        active = db.scalars(select(models.ChargingOrder).where(
            models.ChargingOrder.source_record_id == sr.id,
            models.ChargingOrder.status == models.ENTITY_ACTIVE)).all()
        assert len(active) == 1
        assert active[0].end_reading_kwh - active[0].start_reading_kwh == 40
    finally:
        db.close()


def test_revision_after_adoption_is_blocked_not_overwritten():
    from datetime import datetime
    from app.services.reconciliation import run_reconciliation

    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        # 先跑一轮作业，采用 OR-SH-003 当前版本（end_reading=85）
        job = run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        order_before = db.scalar(select(models.ChargingOrder).where(
            models.ChargingOrder.site_id == site.id,
            models.ChargingOrder.order_no == "O-1003",
            models.ChargingOrder.status == models.ENTITY_ACTIVE,
        ))
        assert order_before.end_reading_kwh == 85
        assert order_before.adopted_by_job_id == job.id

        # 作业之后导入同号更正（end_reading=86）
        result = import_file(db, site, "order", "orders_v3.csv",
                             _read("S01", "orders_v3_after_adopt.csv"), "tester",
                             "data/test_originals")
        db.commit()
        assert result["summary"].get("blocked_adopted", 0) == 1

        # 原数据保持不变，新修订被标记 blocked_adopted
        db.refresh(order_before)
        assert order_before.end_reading_kwh == 85
        sr = db.scalar(select(models.SourceRecord).where(
            models.SourceRecord.site_id == site.id,
            models.SourceRecord.source_record_no == "OR-SH-003",
        ))
        blocked = db.scalars(select(models.RecordRevision).where(
            models.RecordRevision.source_record_id == sr.id,
            models.RecordRevision.status == models.REV_BLOCKED_ADOPTED)).all()
        assert len(blocked) == 1
    finally:
        db.close()
