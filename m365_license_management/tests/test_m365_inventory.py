# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from .common import M365Common


@tagged('post_install', '-at_install')
class TestM365Inventory(M365Common):

    def test_available_units(self):
        sku = self.sku
        self.assertEqual((sku.available_units, sku.utilization), (6, 40.0))
        # Over-assigned subscriptions have no license available
        sku.consumed_units = 12
        self.assertEqual((sku.available_units, sku.utilization), (0, 120.0))
        sku.write({'enabled_units': 0, 'consumed_units': 0})
        self.assertEqual((sku.available_units, sku.utilization), (0, 0.0))

    def test_name_from_catalogue(self):
        self.assertEqual(self.sku.name, 'Microsoft 365 E3')
        self.sku.product_id.name = 'M365 E3'
        self.assertEqual(self.sku.name, 'M365 E3')
        self.sku.product_id = False
        self.assertEqual(self.sku.name, 'SPE_E3')

    def test_record_snapshot(self):
        now = fields.Datetime.now()
        sku = self.sku
        sku._record_snapshot(now)
        # Unchanged on the same day: not recorded
        sku._record_snapshot(now + timedelta(hours=1))
        self.assertEqual(len(sku.snapshot_ids), 1)
        sku.consumed_units = 5
        sku._record_snapshot(now + timedelta(hours=2))
        # Unchanged but the last one is more than a day old
        sku._record_snapshot(now + timedelta(days=1, hours=3))
        snapshots = self.env['m365.inventory.snapshot'].search([('sku_id', '=', sku.id)])
        self.assertEqual(snapshots.mapped('consumed_units'), [5, 5, 4])
        self.assertEqual(snapshots.mapped('available_units'), [5, 5, 6])
        latest = snapshots[0]
        self.assertEqual((latest.tenant_id, latest.sku_part_number, latest.snapshot_date),
                         (self.tenant, 'SPE_E3', now + timedelta(days=1, hours=3)))

    def test_cleanup_snapshots(self):
        Snapshot = self.env['m365.inventory.snapshot']
        now = fields.Datetime.now()
        old, recent = Snapshot.create([
            {'sku_id': self.sku.id, 'snapshot_date': now - timedelta(days=days)}
            for days in (401, 399)])
        Snapshot._cron_cleanup()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_cleanup_audit_logs(self):
        Log = self.env['m365.audit.log']
        ICP = self.env['ir.config_parameter'].sudo()
        # Kept for a year by default
        ICP.set_param('m365.audit_retention_days', False)
        now = fields.Datetime.now()
        old, recent, older = Log.create([
            {'tenant_id': self.tenant.id, 'action': 'webhook', 'status': 'info',
             'date': now - timedelta(days=days)} for days in (366, 364, 1000)])
        Log._cron_cleanup()
        self.assertFalse(old.exists() or older.exists())
        self.assertTrue(recent.exists())
        ICP.set_param('m365.audit_retention_days', '30')
        Log._cron_cleanup()
        self.assertFalse(recent.exists())
        # 0 keeps the logs forever
        ICP.set_param('m365.audit_retention_days', '0')
        kept = Log.create({'tenant_id': self.tenant.id, 'action': 'webhook', 'status': 'info',
                           'date': now - timedelta(days=5000)})
        Log._cron_cleanup()
        self.assertTrue(kept.exists())
