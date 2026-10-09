"""Migration 034 must be additive, match the model, and survive up/down/up."""
import uuid
from pathlib import Path
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from app.config import settings
from app.models.payment import Payment
from app.models.user import User
from tests.conftest import db_url_for, new_test_db_name
from tests.test_migration_033_exam_editions import _admin, _db
from tests.synthetic_ids import TEST_TARGET_ID

def test_034_payments_schema_and_additive_up_down_up(monkeypatch):
    name = new_test_db_name()
    url = db_url_for(name)
    monkeypatch.setattr(settings, 'DATABASE_URL', url)
    cfg = Config()
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'migrations'))
    _admin(f'CREATE DATABASE "{name}"')
    user_id = uuid.uuid4()
    async def seed(conn):
        await conn.execute(User.__table__.insert().values(id=user_id, telegram_id=TEST_TARGET_ID, full_name='Synthetic Teacher'))
    async def schema(conn):
        def read(sync):
            inspector = inspect(sync)
            return {table: [(c['name'], str(c['type']), c['nullable']) for c in inspector.get_columns(table)]
                    for table in inspector.get_table_names()}
        return await conn.run_sync(read)
    async def check_payment(conn):
        def read(sync):
            inspector = inspect(sync)
            return inspector.get_columns('payments'), inspector.get_indexes('payments'), inspector.get_foreign_keys('payments')
        columns, indexes, foreign_keys = await conn.run_sync(read)
        assert {c['name']: c['nullable'] for c in columns} == {c.name: c.nullable for c in Payment.__table__.columns}
        assert {i['name']: i['column_names'] for i in indexes} == {
            'ix_payments_paid_at':['paid_at'], 'ix_payments_user_id_paid_at':['user_id','paid_at']}
        assert foreign_keys[0]['referred_table'] == 'users'
        assert foreign_keys[0]['options']['ondelete'] == 'CASCADE'
        await conn.execute(text("INSERT INTO payments (id,user_id,plan_key,amount_som) VALUES (:id,:user,'standart',75000)"),
                           {'id':uuid.uuid4(),'user':user_id})
        row = (await conn.execute(text('SELECT amount_som,period_days,method,paid_at,created_at FROM payments'))).one()
        assert tuple(row[:3]) == (75000,30,'manual')
        assert row[3] is not None and row[4] is not None
    async def count_user(conn):
        return (await conn.execute(text('SELECT count(*) FROM users WHERE id=:id'), {'id':user_id})).scalar_one()
    try:
        command.upgrade(cfg,'033')
        _db(url,seed)
        before = _db(url,schema)
        assert 'payments' not in before
        command.upgrade(cfg,'034')
        after = _db(url,schema)
        assert {k:v for k,v in after.items() if k != 'payments'} == before
        _db(url,check_payment)
        command.downgrade(cfg,'033')
        assert _db(url,schema) == before
        assert _db(url,count_user) == 1
        command.upgrade(cfg,'034')
        assert _db(url,schema) == after
        assert _db(url,count_user) == 1
    finally:
        _admin(f'DROP DATABASE "{name}" WITH (FORCE)')
