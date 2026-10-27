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
    def get_dashboard_data(self, tenant_ids=None, date_range=30, customer_id=None):
        """Aggregate figures for the OWL dashboard.

        :param tenant_ids: restrict to these tenants (None = all accessible)
        :param date_range: comparison window in days for the trend indicators
        :param customer_id: restrict to the tenants of this customer (customer.customer)
        """
        Tenant = self.env['m365.tenant']
        accessible = Tenant.search(Tenant._dashboard_domain())
        customer_id = int(customer_id or 0)
        all_tenants = (accessible.filtered(lambda t: t.tenant_customer_id.id == customer_id)
                       if customer_id else accessible)
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
        trend, forecast = self._get_trend(skus, date_range)
        for row in products:
            row['days_left'] = forecast.get(row['sku'])
        return {
            'trend': trend,
            'alerts': self._get_alerts(selected, skus),
            'activity': self._get_activity(selected),
            'waste': self._get_waste(selected),
            'tenants': tenant_rows,
            'customers': self._get_customers(accessible),
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

    @api.model
    def _get_customers(self, tenants):
        """Customers of the dashboard tenants, for the customer filter.

        Their name only, as on the tenant form: also for the customers the user does not
        see (tenants mapped to the user directly).
        """
        customers = tenants.tenant_customer_id.sudo().sorted('name')
        return [{'id': customer.id, 'name': customer.name} for customer in customers]

    # ------------------------------------------------------------------
    # Advanced analytics
    # ------------------------------------------------------------------
    @api.model
    def _get_trend(self, skus, date_range):
        """Daily totals over the window (carry-forward of the last snapshot).

        :return: (points, forecast) where ``points`` is a list of
                 ``{date, total, assigned, available}`` and ``forecast`` maps
                 sku_part_number -> estimated days until the product runs out
                 (None when consumption is flat or falling).
        """
        end = fields.Datetime.now()
        start = end - timedelta(days=date_range)
        Snap = self.env['m365.inventory.snapshot'].sudo()
        # baseline = last snapshot before the window, then every snapshot inside it
        state = {}
        before = Snap.search_read(
            [('sku_id', 'in', skus.ids), ('snapshot_date', '<', start)],
            ['sku_id', 'enabled_units', 'consumed_units'], order='snapshot_date desc')
        for row in before:
            state.setdefault(row['sku_id'][0], (row['enabled_units'], row['consumed_units']))
        inside = Snap.search_read(
            [('sku_id', 'in', skus.ids), ('snapshot_date', '>=', start)],
            ['sku_id', 'snapshot_date', 'enabled_units', 'consumed_units'],
            order='snapshot_date asc')
        by_day = {}
        for row in inside:
            by_day.setdefault(row['snapshot_date'].date(), []).append(row)
        points, first_assigned = [], {}
        for offset in range(date_range + 1):
            day = (start + timedelta(days=offset)).date()
            for row in by_day.get(day, []):
                state[row['sku_id'][0]] = (row['enabled_units'], row['consumed_units'])
            if not state:
                continue
            total = sum(v[0] for v in state.values())
            assigned = sum(v[1] for v in state.values())
            points.append({'date': day.isoformat(), 'total': total, 'assigned': assigned,
                           'available': max(total - assigned, 0)})
        # per-product burn rate for the forecast
        sku_part = {s.id: s.sku_part_number for s in skus}
        first, last = {}, {}
        for row in before[::-1] + inside:
            sid = row['sku_id'][0]
            first.setdefault(sid, row['consumed_units'])
            last[sid] = row['consumed_units']
        rates = {}
        for sid, part in sku_part.items():
            if sid in first:
                rates[part] = rates.get(part, 0.0) + (last[sid] - first[sid]) / float(date_range)
        available = {}
        for s in skus:
            available[s.sku_part_number] = available.get(s.sku_part_number, 0) + s.available_units
        forecast = {part: (int(available[part] / rate) if rate > 0 else None)
                    for part, rate in rates.items()}
        return points, forecast

    @api.model
    def _get_alerts(self, tenants, skus):
        """Actionable insights shown in the dashboard attention panel."""
        alerts = []
        for tenant in tenants:
            if tenant.state == 'error':
                alerts.append({'level': 'danger', 'icon': 'fa-plug',
                               'title': tenant.name, 'text': tenant.last_error or '',
                               'kind': 'tenant_error'})
            elif tenant.state == 'draft':
                alerts.append({'level': 'warning', 'icon': 'fa-plug', 'title': tenant.name,
                               'text': 'Connection not tested yet.', 'kind': 'tenant_draft'})
        for sku in skus.filtered(lambda s: s.enabled_units):
            if sku.status in ('Suspended', 'LockedOut'):
                alerts.append({'level': 'danger', 'icon': 'fa-ban', 'title': sku.name,
                               'text': '%s subscription (%s).' % (sku.status, sku.tenant_id.name),
                               'kind': 'sku_status'})
            elif sku.available_units == 0:
                alerts.append({'level': 'danger', 'icon': 'fa-battery-empty', 'title': sku.name,
                               'text': 'No licenses left (%s).' % sku.tenant_id.name,
                               'kind': 'sku_empty'})
            elif sku.low_alert_sent:
                alerts.append({'level': 'warning', 'icon': 'fa-battery-quarter',
                               'title': sku.name,
                               'text': 'Only %s of %s left (%s).' % (
                                   sku.available_units, sku.enabled_units, sku.tenant_id.name),
                               'kind': 'sku_low'})
            if sku.warning_units:
                alerts.append({'level': 'warning', 'icon': 'fa-clock-o', 'title': sku.name,
                               'text': '%s license(s) expiring (%s).' % (
                                   sku.warning_units, sku.tenant_id.name),
                               'kind': 'sku_expiring'})
        Request = self.env['m365.license.request']
        failed = Request.search_count([('tenant_id', 'in', tenants.ids), ('state', '=', 'failed')])
        pending = Request.search_count([('tenant_id', 'in', tenants.ids),
                                        ('state', '=', 'submitted')])
        if failed:
            alerts.append({'level': 'danger', 'icon': 'fa-exclamation-triangle',
                           'title': 'Failed requests', 'text': '%s request(s) need attention.'
                           % failed, 'kind': 'requests_failed'})
        if pending:
            alerts.append({'level': 'info', 'icon': 'fa-hourglass-half',
                           'title': 'Awaiting approval', 'text': '%s request(s) pending.'
                           % pending, 'kind': 'requests_pending'})
        order = {'danger': 0, 'warning': 1, 'info': 2}
        alerts.sort(key=lambda a: order[a['level']])
        return alerts[:30]

    @api.model
    def _get_waste(self, tenants):
        """Licenses that can probably be reclaimed (cost optimisation)."""
        User = self.env['m365.license.user'].sudo()
        base = [('tenant_id', 'in', tenants.ids)]
        disabled = User.search(base + [('account_enabled', '=', False),
                                       ('license_count', '>', 0)])
        return {
            'disabled_licensed_users': len(disabled),
            'disabled_licenses': sum(disabled.mapped('license_count')),
            'users_total': User.search_count(base),
            'users_licensed': User.search_count(base + [('license_count', '>', 0)]),
            'users_unlicensed': User.search_count(base + [('license_count', '=', 0)]),
        }

    @api.model
    def _get_activity(self, tenants, limit=8):
        """Latest operations: audit log for administrators, own requests otherwise."""
        if self.env.user.has_group('m365_license_management.group_m365_admin'):
            logs = self.env['m365.audit.log'].search(
                [('tenant_id', 'in', tenants.ids), ('action', '!=', 'notification')],
                limit=limit)
            return [{'id': l.id, 'date': fields.Datetime.to_string(l.date),
                     'status': l.status, 'action': dict(l._fields['action'].selection)[l.action],
                     'text': (l.message or '')[:140], 'tenant': l.tenant_id.name}
                    for l in logs]
        reqs = self.env['m365.license.request'].search(
            [('tenant_id', 'in', tenants.ids)], limit=limit)
        status = {'done': 'success', 'failed': 'failed'}
        return [{'id': r.id, 'date': fields.Datetime.to_string(r.create_date),
                 'status': status.get(r.state, 'info'),
                 'action': dict(r._fields['request_type'].selection)[r.request_type],
                 'text': '%s - %s / %s (%s)' % (r.name, r.user_id.name, r.sku_id.name,
                                                dict(r._fields['state'].selection)[r.state]),
                 'tenant': r.tenant_id.name} for r in reqs]
