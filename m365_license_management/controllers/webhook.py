# -*- coding: utf-8 -*-
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class M365WebhookController(http.Controller):
    """Endpoint called by Microsoft Graph change notifications."""

    @http.route('/m365/webhook/<int:tenant_id>', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def webhook(self, tenant_id, validationToken=None, **kw):
        # 1. Subscription validation handshake: echo the token as plain text (< 10 s).
        if validationToken:
            return request.make_response(
                validationToken, headers=[('Content-Type', 'text/plain; charset=utf-8')])
        # 2. Notifications: authenticate through clientState, answer 202 quickly.
        try:
            payload = request.get_json_data()
            notifications = payload.get('value', []) if isinstance(payload, dict) else []
            request.env['m365.tenant']._webhook_receive(tenant_id, notifications)
        except Exception:
            _logger.exception("Invalid Microsoft 365 webhook payload")
            return request.make_response('', status=400)
        return request.make_response('', status=202)
