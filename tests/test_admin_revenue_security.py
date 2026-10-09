"""Payment inputs must fail before a database write; private pages cannot be cached."""
import pytest
from sqlalchemy import func, select
from app.models.payment import Payment
from tests.test_admin_panel_http import (client, auth_client, seeded, _post, TG_PAID)  # noqa: F401

@pytest.mark.parametrize('fields', [
    {'amount_som': str(2**31)},
    {'reference': 'R' * 129},
])
async def test_invalid_payment_input_is_a_user_error_without_writing(auth_client, seeded, session, fields):
    payload = {'plan': 'standart', 'amount_som': '75000', **fields}
    response = await _post(auth_client, f'/users/{TG_PAID}/payment', f'/users/{TG_PAID}', **payload)
    assert response.status_code == 303
    assert (await session.execute(select(func.count()).select_from(Payment))).scalar_one() == 0
    assert 'To‘lov yozilmadi' in (await auth_client.get(f'/users/{TG_PAID}')).text

async def test_private_revenue_response_is_not_cacheable(auth_client, seeded):
    response = await auth_client.get('/revenue')
    assert 'no-store' in response.headers.get('cache-control', '')

async def test_teacher_name_is_escaped_on_revenue_and_user_pages(auth_client, seeded, session):
    seeded['paid'].full_name = '<script>alert(1)</script>'
    await session.flush()
    for path in ('/revenue', f'/users/{TG_PAID}'):
        response = await auth_client.get(path)
        assert '<script>alert(1)</script>' not in response.text
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
