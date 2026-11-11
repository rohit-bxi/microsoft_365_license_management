# -*- coding: utf-8 -*-
import logging

from odoo import _, http
from odoo.http import request

from ..models.m365_tenant_consent import CONSENT_CALLBACK_PATH

_logger = logging.getLogger(__name__)

ADMIN_GROUP = 'm365_license_management.group_m365_admin'


class M365ConsentController(http.Controller):
    """Redirect of the Microsoft admin consent page (shared app of the partner)."""

    @http.route(CONSENT_CALLBACK_PATH, type='http', auth='public', methods=['GET'],
                sitemap=False)
    def consent_callback(self, **params):
        try:
            result = request.env['m365.tenant'].sudo()._consent_callback(params)
        except Exception:
            request.env.cr.rollback()
            _logger.exception("Microsoft 365 admin consent callback failed")
            result = {'status': 'failed', 'tenant': None, 'message': _(
                "An unexpected error occurred. Ask your Odoo administrator to check the "
                "logs.")}
        tenant = result.get('tenant')
        user = request.env.user
        tenant_url = False
        if tenant and not user._is_public() and user.has_group(ADMIN_GROUP):
            tenant_url = '/odoo/action-m365_license_management.action_m365_tenant/%s' % (
                tenant.id)
        elif tenant and not user._is_public() and tenant.sudo().portal_user_id == user:
            tenant_url = tenant.sudo()._portal_url()  # the portal user's own tenant
        return request.render('m365_license_management.m365_consent_result', {
            'status': result['status'],
            'message': result['message'],
            'tenant_name': tenant.name if tenant else False,
            'tenant_url': tenant_url,
        })
