# -*- coding: utf-8 -*-
"""Microsoft Graph operations of a tenant.

Thin, documented wrappers around the Graph endpoints used by the module:

=========================================  ======================================
Method                                     Graph endpoint
=========================================  ======================================
``graph_get_organization``                 GET   /organization
``graph_get_subscribed_skus``              GET   /subscribedSkus
``graph_get_user``                         GET   /users/{id}
``graph_get_license_details``              GET   /users/{id}/licenseDetails
``graph_update_user``                      PATCH /users/{id}
``graph_assign_license``                   POST  /users/{id}/assignLicense
=========================================  ======================================

Reference: https://learn.microsoft.com/en-us/graph/use-the-api
"""
import re
import urllib.parse

from odoo import _, fields, models

from .m365_tenant import M365GraphError

USER_SELECT = ('id,displayName,userPrincipalName,mail,accountEnabled,department,'
               'jobTitle,usageLocation,assignedLicenses')
GUID_RE = re.compile(r'^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$')


class M365TenantGraph(models.Model):
    _inherit = 'm365.tenant'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _graph_key(value):
        """Safely encode an object id / UPN for use as a URL path segment."""
        value = (value or '').strip()
        if not value:
            raise M365GraphError(_("A Microsoft Graph user identifier is required."))
        return urllib.parse.quote(value, safe='')

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------
    def graph_get_organization(self):
        self.ensure_one()
        data = self._graph_request('GET', '/organization', paginate=True,
                                   params={'$select': 'id,displayName,verifiedDomains,'
                                                      'assignedPlans'})
        return data[0] if data else {}

    def graph_get_subscribed_skus(self):
        """Raw subscribed SKUs, including their service plans."""
        self.ensure_one()
        return self._graph_request('GET', '/subscribedSkus', paginate=True)

    def graph_get_user(self, graph_id):
        self.ensure_one()
        return self._graph_request('GET', '/users/%s' % self._graph_key(graph_id),
                                   params={'$select': USER_SELECT})

    def graph_get_license_details(self, graph_id):
        """Licenses of a user with the state of each service plan."""
        self.ensure_one()
        items = self._graph_request(
            'GET', '/users/%s/licenseDetails' % self._graph_key(graph_id), paginate=True)
        return [{
            'sku_id': item.get('skuId'),
            'sku_part_number': item.get('skuPartNumber'),
            'service_plans': [{
                'name': plan.get('servicePlanName'),
                'id': plan.get('servicePlanId'),
                'provisioning_status': plan.get('provisioningStatus'),
                'applies_to': plan.get('appliesTo'),
            } for plan in item.get('servicePlans', [])],
        } for item in items]

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    def _check_operations_allowed(self):
        self.ensure_one()
        if not self.allow_license_operations:
            raise M365GraphError(
                _("License operations are disabled for tenant %s.", self.name))

    def graph_update_user(self, graph_id, values):
        """PATCH /users/{id} (e.g. ``{'usageLocation': 'IN'}``)."""
        self.ensure_one()
        self._check_operations_allowed()
        return self._graph_request('PATCH', '/users/%s' % self._graph_key(graph_id),
                                   json_body=values)

    def graph_assign_license(self, graph_id, add_sku_ids=None, remove_sku_ids=None,
                             disabled_plans=None):
        """POST /users/{id}/assignLicense.

        :param add_sku_ids: list of SKU GUIDs to add
        :param remove_sku_ids: list of SKU GUIDs to remove
        :param disabled_plans: ``{sku_guid: [service plan GUIDs to disable]}``
        """
        self.ensure_one()
        self._check_operations_allowed()
        disabled_plans = disabled_plans or {}
        for guid in list(add_sku_ids or []) + list(remove_sku_ids or []):
            if not GUID_RE.match(guid or ''):
                raise M365GraphError(_("Invalid SKU identifier: %s", guid))
        body = {
            'addLicenses': [{'skuId': guid, 'disabledPlans': disabled_plans.get(guid, [])}
                            for guid in (add_sku_ids or [])],
            'removeLicenses': list(remove_sku_ids or []),
        }
        return self._graph_request(
            'POST', '/users/%s/assignLicense' % self._graph_key(graph_id), json_body=body)

    # ------------------------------------------------------------------
    # Local mirror refresh of a single user
    # ------------------------------------------------------------------
    def _refresh_user(self, user):
        """Re-read one user from Graph and update the local record.

        A user deleted in Entra ID (HTTP 404) is archived locally.
        """
        self.ensure_one()
        try:
            item = self.graph_get_user(user.graph_id)
        except M365GraphError as exc:
            if 'HTTP 404' in str(exc):
                user.sudo().write({'active': False, 'last_sync': fields.Datetime.now()})
                return user
            raise
        sku_map = {s.sku_id: s.id for s in self.env['m365.sku'].sudo().search(
            [('tenant_id', '=', self.id)])}
        lic_ids = [sku_map[lic['skuId']] for lic in item.get('assignedLicenses', [])
                   if lic.get('skuId') in sku_map]
        user.sudo().write({
            'name': item.get('displayName') or item.get('userPrincipalName'),
            'user_principal_name': item.get('userPrincipalName'),
            'mail': item.get('mail'),
            'account_enabled': item.get('accountEnabled', True),
            'department': item.get('department'),
            'job_title': item.get('jobTitle'),
            'usage_location': item.get('usageLocation'),
            'active': True,
            'last_sync': fields.Datetime.now(),
            'sku_ids': [(6, 0, lic_ids)],
        })
        return user
