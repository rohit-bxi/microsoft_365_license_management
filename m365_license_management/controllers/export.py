# -*- coding: utf-8 -*-
import csv
import io

from odoo import http
from odoo.http import content_disposition, request


class M365ExportController(http.Controller):

    @http.route('/m365/export/licenses', type='http', auth='user', methods=['GET'])
    def export_licenses(self, tenant_id=None, customer_id=None, **kw):
        """CSV export of the license inventory (session authenticated)."""
        if not request.env.user.has_group('m365_license_management.group_m365_user'):
            return request.not_found()
        Tenant = request.env['m365.tenant']
        tenant_domain = Tenant._dashboard_domain()
        if (customer_id or '').isdigit():
            tenant_domain.append(('tenant_customer_id', '=', int(customer_id)))
        domain = [('tenant_id', 'in', Tenant.search(tenant_domain).ids)]
        if (tenant_id or '').isdigit():
            domain.append(('tenant_id', '=', int(tenant_id)))
        skus = request.env['m365.sku'].search(domain)
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['Tenant', 'Product', 'SKU', 'Status', 'Total', 'Assigned',
                         'Available', 'Suspended', 'Utilisation %'])
        for sku in skus:
            writer.writerow([sku.tenant_id.name, sku.name, sku.sku_part_number, sku.status,
                             sku.enabled_units, sku.consumed_units, sku.available_units,
                             sku.suspended_units, round(sku.utilization, 1)])
        return request.make_response(out.getvalue(), headers=[
            ('Content-Type', 'text/csv; charset=utf-8'),
            ('Content-Disposition', content_disposition('m365_licenses.csv')),
        ])
