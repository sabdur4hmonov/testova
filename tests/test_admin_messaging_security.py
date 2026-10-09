"""Confirmed sends must not repeat when a browser retries a POST."""
import re
from tests.test_admin_panel_http import (client, auth_client, seeded, panel_bot, _csrf_from)  # noqa: F401

async def _payload(client, text='audit message'):
    html = (await client.get('/messaging')).text
    intent = re.search(r'name="send_intent"\s+value="([^"]+)"', html)
    return {'segment':'paid_active', 'text':text, 'csrf_token':_csrf_from(html),
            'send_intent':intent.group(1) if intent else 'missing-before-fix', 'confirmed':'1'}

async def test_send_requires_explicit_confirmation(auth_client, seeded, panel_bot):
    data = await _payload(auth_client)
    data.pop('confirmed')
    response = await auth_client.post('/messaging/broadcast', data=data)
    assert response.status_code == 403
    assert panel_bot.sent == []

async def test_retrying_the_same_send_cannot_deliver_twice(auth_client, seeded, panel_bot):
    data = await _payload(auth_client)
    first = await auth_client.post('/messaging/broadcast', data=data)
    assert first.status_code == 303
    count = len(panel_bot.sent)
    assert count > 0
    second = await auth_client.post('/messaging/broadcast', data=data)
    assert second.status_code in (409, 429)
    assert len(panel_bot.sent) == count

async def test_distinct_posts_share_a_send_rate_limit(auth_client, seeded, panel_bot):
    first = await auth_client.post('/messaging/broadcast', data=await _payload(auth_client, 'first'))
    assert first.status_code == 303
    count = len(panel_bot.sent)
    second = await auth_client.post('/messaging/broadcast', data=await _payload(auth_client, 'second'))
    assert second.status_code == 429
    assert len(panel_bot.sent) == count
