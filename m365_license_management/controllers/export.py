# -*- coding: utf-8 -*-
import csv
import io

import openpyxl
from openpyxl.styles import Font, PatternFill

from odoo import fields, http
from odoo.http import content_disposition, request

STATE_FILLS = {'done': 'C6EFCE', 'failed': 'FFC7CE', 'action': 'FFEB9C', 'pending': 'FFEB9C'}


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

    @http.route('/m365/export/provisioning', type='http', auth='user', methods=['GET'])
    def export_provisioning(self, tenant_ids=None, **kw):
        """Provisioning report (Excel): every step of each tenant, with its references."""
        if not request.env.user.has_group('m365_license_management.group_m365_user'):
            return request.not_found()
        Tenant = request.env['m365.tenant']
        domain = [('active', '=', True)]
        ids = [int(i) for i in (tenant_ids or '').split(',') if i.strip().isdigit()]
        if ids:
            domain.append(('id', 'in', ids))
        tenants = Tenant.search(domain, order='name')
        Request = request.env['m365.license.request']
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = 'Provisioning'
        steps = ['prov_tenant', 'prov_connection', 'prov_gdap', 'prov_subscriptions',
                 'prov_users', 'prov_licenses', 'prov_verification']
        header = ['Customer', 'Tenant', 'Domain', 'Directory ID', 'Customer ID',
                  'Provisioning'] + [Tenant._fields[s].string for s in steps] + [
            'GDAP Status', 'GDAP Ends On', 'Subscription Orders', 'Subscription IDs',
            'Users', 'Licenses Assigned', 'Licenses Failed', 'Verified', 'Mismatch',
            'Next', 'Last Error']
        sheet.append(header)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        labels = dict(Tenant._fields['prov_tenant'].selection)
        for tenant in tenants:
            sudo = tenant.sudo()
            orders = sudo.pc_order_ids
            requests = Request.sudo().search([('tenant_id', '=', tenant.id),
                                              ('request_type', '=', 'assign')])
            done = requests.filtered(lambda r: r.state == 'done')
            row = [
                tenant.tenant_customer_id.name or '', tenant.name, tenant.domain or '',
                tenant.tenant_id or '', sudo.customer_id or '',
                dict(Tenant._fields['prov_state'].selection).get(tenant.prov_state, ''),
            ] + [labels.get(tenant[s], '') for s in steps] + [
                dict(Tenant._fields['gdap_status'].selection).get(tenant.gdap_status, ''),
                fields.Datetime.to_string(tenant.gdap_end_date) or '',
                ', '.join('%s (%s)' % (o.name, o.state) for o in orders),
                ', '.join(filter(None, orders.line_ids.mapped('subscription_id'))),
                len(tenant.user_ids), len(done),
                len(requests.filtered(lambda r: r.state == 'failed')),
                len(done.filtered(lambda r: r.verification_state == 'verified')),
                len(done.filtered(lambda r: r.verification_state in ('mismatch', 'error'))),
                tenant.prov_summary or '',
                tenant.last_error or sudo.provision_error or tenant.gdap_error or '',
            ]
            sheet.append(row)
            for col, step in enumerate(steps, start=7):
                fill = STATE_FILLS.get(tenant[step])
                if fill:
                    sheet.cell(row=sheet.max_row, column=col).fill = PatternFill(
                        'solid', fgColor=fill)
        for index, title in enumerate(header, start=1):
            sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = \
                max(12, min(len(title) + 4, 40))
        sheet.freeze_panes = 'C2'
        out = io.BytesIO()
        book.save(out)
        return request.make_response(out.getvalue(), headers=[
            ('Content-Type',
             'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', content_disposition('m365_provisioning_report.xlsx')),
        ])
