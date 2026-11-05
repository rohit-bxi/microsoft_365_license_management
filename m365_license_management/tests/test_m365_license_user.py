# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from ..models.m365_tenant_graph import USER_SELECT
from .common import GRAPH_PATH, GUID_A, GUID_B, USER_GROUP, USER_ID, M365Common


@tagged('post_install', '-at_install')
class TestM365LicenseUser(M365Common):

    def test_refresh_from_graph(self):
        item = {'id': USER_ID, 'displayName': 'Adele V.', 'mail': 'adele@contoso.com',
                'userPrincipalName': 'adele.vance@contoso.com', 'accountEnabled': False,
                'usageLocation': 'GB', 'assignedLicenses': [{'skuId': GUID_A}]}
        with patch(GRAPH_PATH, return_value=item) as graph:
            self.adele.action_refresh_from_graph()
        self.assertEqual(graph.call_args.args, ('GET', '/users/%s' % USER_ID))
        self.assertEqual(graph.call_args.kwargs['params'], {'$select': USER_SELECT})
        adele = self.adele
        self.assertEqual((adele.name, adele.mail, adele.account_enabled, adele.usage_location,
                          adele.sku_ids, adele.license_count),
                         ('Adele V.', 'adele@contoso.com', False, 'GB', self.sku, 1))
        self.assertTrue(adele.last_sync)
        self.assertEqual(self._logs('refresh_user').message,
                         'Adele V. refreshed from Microsoft 365.')

    def test_refresh_deleted_user(self):
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 404 - Request_ResourceNotFound')):
            self.adele.action_refresh_from_graph()
        # Deleted in Microsoft Entra ID: archived
        self.assertFalse(self.adele.active)
        self.assertTrue(self.adele.last_sync)
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 403 - Forbidden')), \
                self.assertRaisesRegex(M365GraphError, 'HTTP 403'):
            self.megan.action_refresh_from_graph()
        self.assertTrue(self.megan.active)

    def test_admin_only_actions(self):
        user = new_test_user(self.env, 'm365_directory_user', groups=USER_GROUP)
        adele = self.adele.with_user(user)
        with self.assertRaises(AccessError):
            adele.action_refresh_from_graph()
        with self.assertRaises(AccessError):
            adele.set_usage_location('IN')

    def test_set_usage_location(self):
        wizard = self.env['m365.usage.location.wizard'].create({
            'user_id': self.adele.id, 'country_id': self.env.ref('base.de').id})
        with patch(GRAPH_PATH, return_value={}) as graph:
            self.assertEqual(wizard.action_apply(), {'type': 'ir.actions.act_window_close'})
        self.assertEqual(graph.call_args.args, ('PATCH', '/users/%s' % USER_ID))
        self.assertEqual(graph.call_args.kwargs['json_body'], {'usageLocation': 'DE'})
        self.assertEqual(self.adele.usage_location, 'DE')
        self.assertEqual(self._logs('update_user').message,
                         'Usage location of Adele Vance set to DE.')
        # Not changed in Odoo when Microsoft 365 refuses it
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 403 - Forbidden')), \
                self.assertRaises(M365GraphError):
            self.adele.set_usage_location('FR')
        self.assertEqual(self.adele.usage_location, 'DE')

    def test_live_license_details(self):
        items = [{'skuId': GUID_A, 'skuPartNumber': 'SPE_E3', 'servicePlans': [{
            'servicePlanName': 'EXCHANGE_S_ENTERPRISE', 'servicePlanId': GUID_B,
            'provisioningStatus': 'Success', 'appliesTo': 'User'}]}]
        with patch(GRAPH_PATH, return_value=items) as graph:
            details = self.megan.get_live_license_details()
        self.assertEqual(graph.call_args.args, ('GET', '/users/%s/licenseDetails'
                                                % self.megan.graph_id))
        self.assertEqual(details, [{
            'sku_id': GUID_A, 'sku_part_number': 'SPE_E3',
            'service_plans': [{'name': 'EXCHANGE_S_ENTERPRISE', 'id': GUID_B,
                               'provisioning_status': 'Success', 'applies_to': 'User'}],
        }])

    def test_actions(self):
        self.assertEqual((self.adele.license_count, self.megan.license_count), (0, 1))
        self.assertEqual(self.sku.user_count, 1)
        for method, request_type in (('action_request_assign', 'assign'),
                                     ('action_request_remove', 'remove')):
            action = getattr(self.adele, method)()
            self.assertEqual(action['res_model'], 'm365.license.request')
            self.assertEqual(action['context'], {
                'default_tenant_id': self.tenant.id, 'default_user_id': self.adele.id,
                'default_request_type': request_type})
        action = self.adele.action_set_usage_location()
        self.assertEqual((action['res_model'], action['target'], action['context']),
                         ('m365.usage.location.wizard', 'new',
                          {'default_user_id': self.adele.id}))
