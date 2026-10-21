# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests import new_test_user, tagged

from ..models.m365_dashboard import _delta, _pct
from .common import GUID_B, USER_GROUP, M365Common

GUID_C = 'cccccccc-0000-0000-0000-000000000001'


@tagged('post_install', '-at_install')
class TestM365Dashboard(M365Common):

    def _data(self, env=None):
        return (env or self.env)['m365.dashboard'].get_dashboard_data(
            tenant_ids=[self.tenant.id], date_range=10)

    def test_helpers(self):
        self.assertEqual((_pct(1, 3), _pct(1, 0)), (33.3, 0.0))
        self.assertEqual(_delta(15, 10), 50.0)
        # No history to compare with
        self.assertIsNone(_delta(5, None))
        self.assertIsNone(_delta(5, 0))

    def test_kpis_trend_and_forecast(self):
        now = fields.Datetime.now()
        self.sku.consumed_units = 6
        self.tenant.last_sync_date = now
        self.env['m365.inventory.snapshot'].create([
            {'sku_id': self.sku.id, 'snapshot_date': now - timedelta(days=days),
             'enabled_units': 10, 'consumed_units': consumed}
            for days, consumed in ((15, 1), (5, 6))])
        data = self._data()
        # Compared with the last snapshot before the 10 days window
        self.assertEqual(data['kpis'], {
            'total': 10, 'assigned': 6, 'available': 4, 'suspended': 0, 'products': 1,
            'assigned_pct': 60.0, 'available_pct': 40.0,
            'deltas': {'total': 0.0, 'assigned': 500.0, 'available': -55.6, 'products': 0},
        })
        # One point per day of the window, carrying the last snapshot forward
        trend = data['trend']
        self.assertEqual(trend[0], {'date': (now - timedelta(days=10)).date().isoformat(),
                                    'total': 10, 'assigned': 1, 'available': 9})
        self.assertEqual([point['assigned'] for point in trend], [1] * 5 + [6] * 6)
        # 5 licenses assigned in 10 days: the 4 left last 8 days
        self.assertEqual(data['products'], [{
            'name': 'Microsoft 365 E3', 'sku': 'SPE_E3', 'total': 10, 'assigned': 6,
            'available': 4, 'suspended': 0, 'tenants': 1, 'utilization': 60.0,
            'status': 'Active', 'days_left': 8,
        }])
        row = next(row for row in data['tenants'] if row['id'] == self.tenant.id)
        self.assertEqual(row, {
            'id': self.tenant.id, 'name': 'Contoso', 'domain': 'contoso.com',
            'state': 'connected', 'selected': True, 'total': 10, 'assigned': 6,
            'available': 4, 'utilization': 60.0, 'last_sync': fields.Datetime.to_string(now),
        })
        self.assertEqual(data['last_updated'], fields.Datetime.to_string(now))
        self.assertTrue(data['is_admin'])

    def test_no_history(self):
        data = self._data()
        self.assertEqual(data['kpis']['deltas'], {'total': None, 'assigned': None,
                                                  'available': None, 'products': None})
        self.assertEqual(data['trend'], [])
        self.assertIsNone(data['products'][0]['days_left'])
        self.assertFalse(data['last_updated'])

    def test_alerts(self):
        self.tenant.write({'state': 'error', 'last_error': 'AADSTS7000215'})
        self.sku.write({'consumed_units': 10, 'warning_units': 2})
        self.env['m365.sku'].create([
            {'tenant_id': self.tenant.id, 'sku_id': GUID_B, 'sku_part_number': 'VISIOCLIENT',
             'status': 'Suspended', 'enabled_units': 5},
            {'tenant_id': self.tenant.id, 'sku_id': GUID_C, 'sku_part_number': 'POWER_BI_PRO',
             'enabled_units': 10, 'consumed_units': 9, 'low_alert_sent': True},
        ])
        self.env['m365.license.request'].create([{
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id,
            'state': state} for state in ('failed', 'submitted')])
        alerts = self._data()['alerts']
        # Most severe first
        self.assertEqual([(a['level'], a['kind'], a['title'], a['text']) for a in alerts], [
            ('danger', 'tenant_error', 'Contoso', 'AADSTS7000215'),
            ('danger', 'sku_empty', 'Microsoft 365 E3', 'No licenses left (Contoso).'),
            ('danger', 'sku_status', 'VISIOCLIENT', 'Suspended subscription (Contoso).'),
            ('danger', 'requests_failed', 'Failed requests', '1 request(s) need attention.'),
            ('warning', 'sku_expiring', 'Microsoft 365 E3', '2 license(s) expiring (Contoso).'),
            ('warning', 'sku_low', 'POWER_BI_PRO', 'Only 1 of 10 left (Contoso).'),
            ('info', 'requests_pending', 'Awaiting approval', '1 request(s) pending.'),
        ])

    def test_waste(self):
        self.megan.account_enabled = False
        self.assertEqual(self._data()['waste'], {
            'disabled_licensed_users': 1, 'disabled_licenses': 1, 'users_total': 2,
            'users_licensed': 1, 'users_unlicensed': 1,
        })

    def test_activity(self):
        self.tenant._log('sync_inventory', 'success', '1 subscription(s) synchronised.')
        self.tenant._log('notification', 'info', 'Low availability alert sent.')
        # Administrators: the audit log, without notifications
        self.assertEqual([(a['action'], a['status'], a['text'], a['tenant'])
                          for a in self._data()['activity']],
                         [('Inventory Synchronisation', 'success',
                           '1 subscription(s) synchronised.', 'Contoso')])
        # Users: their own requests
        user = new_test_user(self.env, 'm365_dashboard_user', groups=USER_GROUP)
        Request = self.env['m365.license.request']
        own = Request.with_user(user).create({
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id})
        Request.create({'tenant_id': self.tenant.id, 'user_id': self.megan.id,
                        'sku_id': self.sku.id, 'request_type': 'remove'})
        data = self._data(self.env(user=user))
        self.assertFalse(data['is_admin'])
        self.assertEqual([(a['id'], a['action'], a['status'], a['text'])
                          for a in data['activity']],
                         [(own.id, 'Assign License', 'info',
                           '%s - Adele Vance / Microsoft 365 E3 (Draft)' % own.name)])
