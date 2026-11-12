# -*- coding: utf-8 -*-
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class M365IngramWebhookController(http.Controller):
    """Order events of Ingram Micro, authenticated by the x-hub-signature header."""

    @http.route('/m365/ingram/webhook/<int:account_id>', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def ingram_webhook(self, account_id, **kw):
        try:
            status = request.env['m365.ingram.event'].sudo()._receive(
                account_id, request.httprequest.get_data(),
                request.httprequest.headers.get('x-hub-signature'))
        except Exception:
            _logger.exception("Invalid Ingram Micro webhook call")
            status = 400
        return request.make_response('', status=status)
