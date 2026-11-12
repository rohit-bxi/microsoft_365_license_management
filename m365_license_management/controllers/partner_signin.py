# -*- coding: utf-8 -*-
import logging

from odoo import _, http
from odoo.http import request

from ..models.m365_partner_signin import SIGNIN_CALLBACK_PATH

_logger = logging.getLogger(__name__)


class M365PartnerSigninController(http.Controller):
    """Redirect of the Microsoft sign-in page of the partner user (App+User)."""

    @http.route(SIGNIN_CALLBACK_PATH, type='http', auth='user', methods=['GET'],
                sitemap=False)
    def partner_signin_callback(self, **params):
        try:
            result = request.env['m365.partner.account']._partner_signin_callback(params)
        except Exception:
            request.env.cr.rollback()
            _logger.exception("Partner user sign-in callback failed")
            result = {'status': 'failed', 'account': None, 'message': _(
                "An unexpected error occurred. Ask your Odoo administrator to check the "
                "logs.")}
        account = result.get('account')
        return request.render('m365_license_management.m365_consent_result', {
            'title': _("Partner user sign-in"),
            'status': result['status'],
            'message': result['message'],
            'tenant_name': False,
            'tenant_url': ('/odoo/action-m365_license_management.action_m365_partner_account/%s'
                           % account.id) if account else False,
            'link_label': _("Open the Partner Center account in Odoo"),
        })
