# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models


def _pct(part, whole):
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _delta(current, previous):
    """Percentage change; None when there is no history to compare with."""
    if previous is None:
        return None
    if not previous:
        return None
    return round((current - previous) * 100.0 / previous, 1)


class M365Dashboard(models.AbstractModel):
    _name = 'm365.dashboard'
    _description = 'Microsoft 365 License Dashboard Backend'

    @api.model
    def get_dashboard_data(self, tenant_ids=None, date_range=30):
        """Aggregate figures for the OWL dashboard.

        :param tenant_ids: restrict to these tenants (None = all accessible)
        :param date_range: comparison window in days for the trend indicators
        """
        Tenant = self.env['m365.tenant']
        all_tenants = Tenant.search([('active', '=', True)])
        selected = all_tenants.filtered(lambda t: t.id in tenant_ids) if tenant_ids else all_tenants
        skus = self.env['m365.sku'].sudo().search([
            ('tenant_id', 'in', selected.ids), ('status', '!=', 'Deleted')])
        date_range = int(date_range or 30)

        total = sum(skus.mapped('enabled_units'))
        assigned = sum(skus.mapped('consumed_units'))
        available = sum(skus.mapped('available_units'))
        suspended = sum(skus.mapped('suspended_units'))
        products_now = len(skus.filtered(lambda s: s.enabled_units > 0))

        # --- trend vs. previous period ---------------------------------
        cmp_date = fields.Datetime.now() - timedelta(days=date_range)
        prev = self.env['m365.inventory.snapshot'].sudo().search_read(
            [('sku_id', 'in', skus.ids), ('snapshot_date', '<=', cmp_date)],
            ['sku_id', 'enabled_units', 'consumed_units', 'available_units'],
            order='snapshot_date desc')
        latest_prev = {}
        for row in prev:
            latest_prev.setdefault(row['sku_id'][0], row)
        if latest_prev:
            p_total = sum(r['enabled_units'] for r in latest_prev.values())
            p_assigned = sum(r['consumed_units'] for r in latest_prev.values())
            p_available = sum(r['available_units'] for r in latest_prev.values())
            p_products = len([r for r in latest_prev.values() if r['enabled_units'] > 0])
            deltas = {
                'total': _delta(total, p_total),
                'assigned': _delta(assigned, p_assigned),
                'available': _delta(available, p_available),
                'products': products_now - p_products,
            }
        else:
            deltas = {'total': None, 'assigned': None, 'available': None, 'products': None}

        # --- per product (aggregated over the selected tenants) ---------
        by_product = {}
        for sku in skus:
            key = sku.sku_part_number
            row = by_product.setdefault(key, {
                'name': sku.name, 'sku': key, 'total': 0, 'assigned': 0, 'available': 0,
                'suspended': 0, 'tenants': set(), 'statuses': set(),
            })
            row['total'] += sku.enabled_units
            row['assigned'] += sku.consumed_units
            row['available'] += sku.available_units
            row['suspended'] += sku.suspended_units
            row['tenants'].add(sku.tenant_id.id)
            row['statuses'].add(sku.status)
        products = []
        for row in sorted(by_product.values(), key=lambda r: (-r['total'], r['name'])):
            statuses = row.pop('statuses')
            row['tenants'] = len(row['tenants'])
            row['utilization'] = _pct(row['assigned'], row['total'])
            row['status'] = 'Active' if 'Enabled' in statuses else sorted(statuses)[0]
            products.append(row)

        # --- per tenant -------------------------------------------------
        tenant_rows = []
        for tenant in all_tenants:
            t_skus = skus.filtered(lambda s: s.tenant_id == tenant)
            t_total = sum(t_skus.mapped('enabled_units'))
            t_assigned = sum(t_skus.mapped('consumed_units'))
            tenant_rows.append({
                'id': tenant.id,
                'name': tenant.name,
                'domain': tenant.domain,
                'state': tenant.state,
                'selected': tenant in selected,
                'total': t_total,
                'assigned': t_assigned,
                'available': sum(t_skus.mapped('available_units')),
                'utilization': _pct(t_assigned, t_total),
                'last_sync': fields.Datetime.to_string(tenant.last_sync_date)
                if tenant.last_sync_date else False,
            })

        syncs = [t.last_sync_date for t in selected if t.last_sync_date]
        return {
            'tenants': tenant_rows,
            'kpis': {
                'total': total,
                'assigned': assigned,
                'available': available,
                'suspended': suspended,
                'products': products_now,
                'assigned_pct': _pct(assigned, total),
                'available_pct': _pct(available, total),
                'deltas': deltas,
            },
            'products': products,
            'last_updated': fields.Datetime.to_string(max(syncs)) if syncs else False,
            'is_admin': self.env.user.has_group('m365_license_management.group_m365_admin'),
        }
