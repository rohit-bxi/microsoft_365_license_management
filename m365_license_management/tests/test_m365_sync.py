# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from .common import (ADMIN_GROUP, GRAPH_PATH, GUID_A, GUID_B, USER_GROUP, USER_ID, USER_ID_2,
                     M365Common, graph_stub)

GUID_C = 'cccccccc-0000-0000-0000-000000000001'


@tagged('post_install', '-at_install')
class TestM365Sync(M365Common):

    def _alerts(self, subject):
        return self.tenant.message_ids.filtered(lambda m: m.subject == subject)

    # ------------------------------------------------------------------
    # Inventory and users
    # ------------------------------------------------------------------
    def test_sync_inventory(self):
        removed = self.env['m365.sku'].create({
            'tenant_id': self.tenant.id, 'sku_id': GUID_B, 'sku_part_number': 'VISIOCLIENT',
            'enabled_units': 5, 'consumed_units': 1})
        items = [
            {'skuId': GUID_A, 'skuPartNumber': 'SPE_E3', 'capabilityStatus': 'Warning',
             'consumedUnits': 8, 'appliesTo': 'User',
             'prepaidUnits': {'enabled': 10, 'suspended': 2, 'warning': 3}},
            {'skuId': GUID_C, 'skuPartNumber': 'CONTOSO_CUSTOM_PLAN',
             'capabilityStatus': 'Unknown', 'consumedUnits': 0, 'prepaidUnits': {'enabled': 1}},
        ]
        with patch(GRAPH_PATH, return_value=items) as graph:
            self.assertEqual(self.tenant._sync_inventory(), 2)
        self.assertEqual(graph.call_args.args, ('GET', '/subscribedSkus'))
        sku = self.sku
        self.assertEqual((sku.status, sku.enabled_units, sku.consumed_units, sku.suspended_units,
                          sku.warning_units, sku.available_units, sku.applies_to),
                         ('Warning', 10, 8, 2, 3, 2, 'User'))
        # Unknown part numbers are added to the catalogue; unknown statuses are active
        new = self.tenant.sku_ids.filtered(lambda s: s.sku_id == GUID_C)
        self.assertEqual((new.name, new.status), ('Contoso Custom Plan', 'Enabled'))
        self.assertTrue(new.product_id.auto_created)
        # Subscriptions no longer returned are kept as deleted
        self.assertEqual((removed.status, removed.enabled_units, removed.consumed_units),
                         ('Deleted', 0, 0))
        self.assertEqual(len(sku.snapshot_ids | new.snapshot_ids | removed.snapshot_ids), 3)
        self.assertEqual(sku.snapshot_ids.consumed_units, 8)
        self.assertEqual((self.tenant.state, self.tenant.last_error), ('connected', False))
        self.assertTrue(self.tenant.last_sync_date)
        # A deleted subscription is not snapshotted again
        with patch(GRAPH_PATH, return_value=items):
            self.tenant._sync_inventory()
        self.assertEqual(len(removed.snapshot_ids), 1)

    def test_sync_users_full_read(self):
        items = [
            {'id': USER_ID, 'displayName': 'Adele Vance', 'mail': 'adele@contoso.com',
             'userPrincipalName': 'adele.vance@contoso.com', 'accountEnabled': False,
             'department': 'Finance', 'jobTitle': 'Analyst', 'usageLocation': 'GB',
             'assignedLicenses': [{'skuId': GUID_A}, {'skuId': GUID_C}]},
            {'id': 'graph-lee', 'userPrincipalName': 'lee.gu@contoso.com',
             'assignedLicenses': []},
        ]
        with patch(GRAPH_PATH, return_value=items) as graph:
            self.assertEqual(self.tenant._sync_users_from_graph(), 2)
        self.assertEqual(graph.call_args.args, ('GET', '/users'))
        adele = self.adele
        self.assertEqual((adele.mail, adele.account_enabled, adele.department, adele.job_title,
                          adele.usage_location, adele.sku_ids),
                         ('adele@contoso.com', False, 'Finance', 'Analyst', 'GB', self.sku))
        # Named after the user principal name when there is no display name
        lee = self.tenant.user_ids.filtered(lambda u: u.graph_id == 'graph-lee')
        self.assertEqual(lee.name, 'lee.gu@contoso.com')
        # Users that no longer exist are archived
        self.assertFalse(self.megan.active)
        self.assertTrue(self.tenant.last_user_sync_date)
        # and restored when they come back
        with patch(GRAPH_PATH, return_value=[{'id': USER_ID_2, 'displayName': 'Megan'}]):
            self.tenant._sync_users_from_graph()
        self.assertTrue(self.megan.active)
        self.assertFalse(self.megan.sku_ids)

    def test_sync_all(self):
        with patch(GRAPH_PATH, side_effect=graph_stub):
            self.assertEqual(self.tenant._sync_all_safe(), (True, [
                'Contoso: 1 subscription(s) synchronised.', 'Contoso: 0 user(s) synchronised.']))
        self.assertEqual(self._logs('sync_inventory').status, 'success')
        self.assertEqual(self._logs('sync_users').status, 'success')
        # Users are not synchronised when disabled
        self.tenant.sync_users = False
        with patch(GRAPH_PATH, side_effect=graph_stub) as graph:
            ok, messages = self.tenant._sync_all_safe()
        self.assertTrue(ok)
        self.assertEqual([call.args[1] for call in graph.call_args_list], ['/subscribedSkus'])

    def test_user_sync_failure(self):
        self.tenant.failure_count = 2

        def graph(method, path, **kw):
            if path == '/users':
                raise M365GraphError('HTTP 403 - Authorization_RequestDenied')
            return graph_stub(method, path, **kw)

        with patch(GRAPH_PATH, side_effect=graph):
            ok, messages = self.tenant._sync_all_safe()
        self.assertFalse(ok)
        self.assertEqual(messages, ['Contoso: 1 subscription(s) synchronised.',
                                    'Contoso: HTTP 403 - Authorization_RequestDenied'])
        # The inventory is up to date: still connected, the failure counter is kept
        self.assertEqual((self.tenant.state, self.tenant.failure_count, self.tenant.last_error),
                         ('connected', 2, 'HTTP 403 - Authorization_RequestDenied'))
        self.assertEqual(self._logs('sync_users').status, 'failed')

    def test_repeated_failures_alert_admins(self):
        subject = 'Microsoft 365 synchronisation failing - Contoso'
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 500 - InternalServerError')):
            for _i in range(4):
                self.assertEqual(self.tenant._sync_all_safe(),
                                 (False, ['Contoso: HTTP 500 - InternalServerError']))
        # Sent once, at the third failure in a row
        alert = self._alerts(subject)
        self.assertEqual(len(alert), 1)
        self.assertIn('failed 3 times in a row', alert.body)
        self.assertIn(self.env.ref('base.user_admin').partner_id, alert.partner_ids)
        self.assertEqual((self.tenant.state, self.tenant.failure_count), ('error', 4))
        self.assertEqual(self._logs('sync_inventory').mapped('status'), ['failed'] * 4)
        # A successful synchronisation resets the counter
        with patch(GRAPH_PATH, side_effect=graph_stub):
            self.assertTrue(self.tenant._sync_all_safe()[0])
        self.assertEqual((self.tenant.state, self.tenant.failure_count), ('connected', 0))

    def test_cron_sync_all(self):
        Tenant = self.env['m365.tenant']
        (Tenant.search([]) - self.tenant).sync_enabled = False
        failing, draft, disabled = Tenant.create([{
            'name': name, 'domain': '%s.com' % name, 'tenant_id': 'tid-%s' % name,
            'client_id': 'cid', 'state': state, 'sync_enabled': sync,
        } for name, state, sync in (('failing', 'error', True), ('draft', 'draft', True),
                                    ('disabled', 'connected', False))])
        with patch(GRAPH_PATH, autospec=True, side_effect=lambda tenant, *args, **kw:
                   graph_stub(*args, **kw)) as graph:
            Tenant._cron_sync_all()
        # Connected tenants and the ones in error, not the untested or disabled ones
        synced = Tenant.union(*(call.args[0] for call in graph.call_args_list))
        self.assertEqual(synced, self.tenant | failing)
        self.assertFalse(draft.last_sync_date or disabled.last_sync_date)

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def test_low_availability_alert(self):
        subject = 'Low license availability - Contoso'
        self.sku.consumed_units = 9  # 1 of 10 left: the 10 % threshold
        self.tenant._check_low_availability()
        self.assertTrue(self.sku.low_alert_sent)
        alert = self._alerts(subject)
        self.assertEqual(len(alert), 1)
        self.assertIn('1 available of 10 (90% utilised)', alert.body)
        self.assertEqual(self._logs('notification').message,
                         'Low availability alert sent for: Microsoft 365 E3')
        # Sent once only
        self.tenant._check_low_availability()
        self.assertEqual(len(self._alerts(subject)), 1)
        # Re-armed when the licenses are available again
        self.sku.consumed_units = 5
        self.tenant._check_low_availability()
        self.assertFalse(self.sku.low_alert_sent)
        # Threshold in units
        self.tenant.low_availability_units = 5
        self.tenant._check_low_availability()
        self.assertTrue(self.sku.low_alert_sent)
        self.assertEqual(len(self._alerts(subject)), 2)

    def test_no_alert_for_inactive_subscriptions(self):
        self.sku.write({'status': 'Suspended', 'consumed_units': 10})
        self.env['m365.sku'].create({'tenant_id': self.tenant.id, 'sku_id': GUID_B,
                                     'sku_part_number': 'FREE', 'enabled_units': 0})
        self.tenant._check_low_availability()
        self.assertFalse(self.tenant.sku_ids.filtered('low_alert_sent'))
        self.assertFalse(self._logs('notification'))

    def test_threshold_constraint(self):
        with self.assertRaisesRegex(UserError, 'between 0 and 100'):
            self.tenant.low_availability_percent = 150

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def test_test_connection_missing_permission(self):
        self.tenant.write({'state': 'error', 'failure_count': 2})

        def graph(method, path, params=None, **kw):
            if path == '/organization':
                return [{'displayName': 'Contoso Ltd'}]
            if path == '/users':
                raise M365GraphError('HTTP 403 - Authorization_RequestDenied')
            return []

        with patch(GRAPH_PATH, side_effect=graph):
            action = self.tenant.action_test_connection()
        self.assertEqual((self.tenant.state, self.tenant.failure_count), ('connected', 0))
        params = action['params']
        self.assertEqual((params['type'], params['sticky']), ('danger', True))
        self.assertEqual(params['message'],
                         'Contoso: Connected to Microsoft 365 organisation "Contoso Ltd". No '
                         'access to the user directory (User.Read.All): HTTP 403 - '
                         'Authorization_RequestDenied')
        self.assertEqual(self._logs('test_connection').status, 'failed')
        # Without user synchronisation the user directory is not needed
        self.tenant.sync_users = False
        with patch(GRAPH_PATH, side_effect=graph):
            action = self.tenant.action_test_connection()
        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual(action['params']['next']['tag'], 'soft_reload')

    def test_sync_button_and_dashboard_refresh(self):
        with patch(GRAPH_PATH, side_effect=graph_stub):
            action = self.tenant.action_sync_inventory()
        self.assertEqual(action['params']['type'], 'success')
        self.assertEqual(action['params']['message'], 'Contoso: 1 subscription(s) synchronised.\n'
                                                      'Contoso: 0 user(s) synchronised.')
        user = new_test_user(self.env, 'm365_sync_user', groups=USER_GROUP)
        with self.assertRaisesRegex(UserError, 'Only Microsoft 365 administrators'):
            self.env['m365.tenant'].with_user(user).sync_from_dashboard()
        admin = new_test_user(self.env, 'm365_sync_admin', groups=ADMIN_GROUP)
        with patch(GRAPH_PATH, side_effect=M365GraphError('HTTP 401 - Unauthorized')):
            result = self.env['m365.tenant'].with_user(admin).sync_from_dashboard(
                [self.tenant.id])
        self.assertEqual(result, {'ok': False, 'messages': ['Contoso: HTTP 401 - Unauthorized']})

    def test_statistics_and_actions(self):
        self.env['m365.sku'].create({'tenant_id': self.tenant.id, 'sku_id': GUID_B,
                                     'sku_part_number': 'VISIOCLIENT', 'enabled_units': 5,
                                     'consumed_units': 5})
        tenant = self.tenant
        self.assertEqual((tenant.sku_count, tenant.user_count, tenant.request_count,
                          tenant.total_licenses, tenant.assigned_licenses,
                          tenant.available_licenses), (2, 2, 0, 15, 9, 6))
        for method in ('action_view_skus', 'action_view_users', 'action_view_requests'):
            action = getattr(tenant, method)()
            self.assertEqual(action['domain'], [('tenant_id', '=', tenant.id)])
            self.assertEqual(action['context'], {'default_tenant_id': tenant.id})
        action = self.sku.action_view_users()
        self.assertEqual(self.env['m365.license.user'].search(action['domain']), self.megan)
