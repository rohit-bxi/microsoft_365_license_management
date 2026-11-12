# -*- coding: utf-8 -*-
import csv
import io

import openpyxl
from openpyxl.styles import Font, PatternFill

from odoo import fields, http
from odoo.http import content_disposition, request

from ..models.m365_tenant_provisioning import STEPS

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

    @staticmethod
    def _sheet(book, title, header, rows, first=False):
        sheet = book.active if first else book.create_sheet(title)
        sheet.title = title
        sheet.append(header)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append(['' if value is None or value is False else value for value in row])
        for index, name in enumerate(header, start=1):
            sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = \
                max(12, min(len(name) + 4, 40))
        sheet.freeze_panes = 'C2'
        return sheet

    @http.route('/m365/export/provisioning', type='http', auth='user', methods=['GET'])
    def export_provisioning(self, tenant_ids=None, **kw):
        """Provisioning report (Excel): every step of each tenant with its references, and
        the domains, users, licenses and subscriptions of the tenants."""
        if not request.env.user.has_group('m365_license_management.group_m365_user'):
            return request.not_found()
        is_admin = request.env.user.has_group('m365_license_management.group_m365_admin')
        Tenant = request.env['m365.tenant']
        domain = [('active', '=', True)]
        ids = [int(i) for i in (tenant_ids or '').split(',') if i.strip().isdigit()]
        if ids:
            domain.append(('id', 'in', ids))
        tenants = Tenant.search(domain, order='name')
        Request = request.env['m365.license.request']
        book = openpyxl.Workbook()
        labels = dict(Tenant._fields['prov_tenant'].selection)

        def label(record, fname):
            return dict(record._fields[fname].selection).get(record[fname], '') \
                if record[fname] else ''

        # 1. Summary: one row per tenant
        header = ['Customer', 'Tenant', 'Domain', 'Directory ID', 'Customer ID',
                  'Provisioning'] + [Tenant._fields[s].string for s in STEPS] + [
            'Custom Domains', 'Initial Admin', 'Customer Agreement', 'Shared App',
            'GDAP Status', 'GDAP Group Access', 'GDAP Ends On', 'Subscription Orders',
            'Subscription IDs', 'Users', 'Users Waiting', 'Licenses Assigned',
            'Licenses Failed', 'Verified', 'Mismatch', 'Automatic', 'Next', 'Waiting For',
            'Last Error']
        rows = []
        for tenant in tenants:
            sudo = tenant.sudo()
            orders = sudo.pc_order_ids
            requests = Request.sudo().search([('tenant_id', '=', tenant.id),
                                              ('request_type', '=', 'assign')])
            done = requests.filtered(lambda r: r.state == 'done')
            rows.append([
                tenant.tenant_customer_id.name or '', tenant.name, tenant.domain or '',
                tenant.tenant_id or '', sudo.customer_id or '', label(tenant, 'prov_state'),
            ] + [labels.get(tenant[s], '') for s in STEPS] + [
                tenant.custom_domains or '',
                sudo.admin_username if is_admin else '',
                ('%s %s' % (sudo.mca_email or '', sudo.mca_date or '')).strip()
                if sudo.mca_accepted else '',
                label(tenant, 'app_install_state') if tenant.app_install_state != 'none'
                else '',
                label(tenant, 'gdap_status'), label(tenant, 'gdap_assignment_status'),
                fields.Datetime.to_string(tenant.gdap_end_date) or '',
                ', '.join('%s (%s)' % (o.name, o.state) for o in orders),
                ', '.join(filter(None, orders.line_ids.mapped('subscription_id'))),
                len(tenant.user_ids),
                len(sudo.pending_user_ids.filtered(lambda u: u.state == 'waiting')),
                len(done), len(requests.filtered(lambda r: r.state == 'failed')),
                len(done.filtered(lambda r: r.verification_state == 'verified')),
                len(done.filtered(lambda r: r.verification_state in ('mismatch', 'error'))),
                'Yes' if tenant.auto_provision else '', tenant.prov_summary or '',
                tenant.prov_waiting or '',
                (tenant.prov_error or tenant.last_error or sudo.provision_error
                 or tenant.gdap_error or ''),
            ])
        sheet = self._sheet(book, 'Provisioning', header, rows, first=True)
        for row_index, tenant in enumerate(tenants, start=2):
            for col, step in enumerate(STEPS, start=7):
                fill = STATE_FILLS.get(tenant[step])
                if fill:
                    sheet.cell(row=row_index, column=col).fill = PatternFill(
                        'solid', fgColor=fill)
        # 2. Domains
        domains = request.env['m365.tenant.domain'].search(
            [('tenant_id', 'in', tenants.ids)], order='tenant_id, is_initial desc, name')
        self._sheet(book, 'Domains', [
            'Tenant', 'Directory ID', 'Domain', 'Type', 'Status', 'Default', 'TXT Record',
            'Added On', 'Next Check', 'Error'], [[
                d.tenant_id.name, d.tenant_id.tenant_id, d.name,
                'Initial' if d.is_initial else 'Custom', label(d, 'state'),
                'Yes' if d.is_default else '', d.txt_record,
                fields.Datetime.to_string(d.added_date),
                fields.Datetime.to_string(d.next_check), d.error,
            ] for d in domains])
        # 3. Users: of Microsoft 365, then the ones still to create
        users = request.env['m365.license.user'].search([('tenant_id', 'in', tenants.ids)],
                                                        order='tenant_id, name')
        pending = request.env['m365.tenant.pending.user'].search(
            [('tenant_id', 'in', tenants.ids), ('state', 'in', ('waiting', 'failed'))])
        self._sheet(book, 'Users', [
            'Tenant', 'Directory ID', 'User Principal Name', 'Display Name', 'Enabled',
            'Usage Location', 'Licenses', 'Status', 'Details'], [[
                u.tenant_id.name, u.tenant_id.tenant_id, u.user_principal_name, u.name,
                'Yes' if u.account_enabled else 'No', u.usage_location,
                ', '.join(u.sku_ids.mapped('name')), 'In Microsoft 365', '',
            ] for u in users] + [[
                p.tenant_id.name, p.tenant_id.tenant_id, p.upn, p.name, '',
                p.usage_location, p.licenses, label(p, 'state'),
                p.error or p.wait_reason,
            ] for p in pending])
        # 4. Licenses: assignment requests and their verification
        requests = Request.search([('tenant_id', 'in', tenants.ids),
                                   ('request_type', 'in', ('assign', 'remove'))],
                                  order='tenant_id, id')
        self._sheet(book, 'Licenses', [
            'Tenant', 'User Principal Name', 'Operation', 'License', 'SKU', 'Request',
            'Status', 'Verification', 'Details', 'Executed On'], [[
                r.tenant_id.name, r.user_id.user_principal_name, label(r, 'request_type'),
                r.sku_id.name, r.sku_id.sku_part_number, r.name, label(r, 'state'),
                label(r, 'verification_state'),
                r.verification_message or r.error_message,
                fields.Datetime.to_string(r.executed_date),
            ] for r in requests])
        # 5. Subscriptions bought through Partner Center
        lines = request.env['m365.pc.order.line'].search(
            [('tenant_id', 'in', tenants.ids)], order='tenant_id, order_id')
        self._sheet(book, 'Subscriptions', [
            'Tenant', 'Order', 'Offer', 'Quantity', 'Subscription ID', 'Order Status',
            'Partner Center Status', 'Ordered On', 'Done On'], [[
                line.tenant_id.name, line.order_id.name, line.offer_id.display_name,
                line.quantity, line.subscription_id, label(line.order_id, 'state'),
                line.pc_status, fields.Datetime.to_string(line.order_id.sent_date),
                fields.Datetime.to_string(line.order_id.done_date),
            ] for line in lines])
        out = io.BytesIO()
        book.save(out)
        return request.make_response(out.getvalue(), headers=[
            ('Content-Type',
             'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', content_disposition('m365_provisioning_report.xlsx')),
        ])

    @http.route('/m365/export/credentials', type='http', auth='user', methods=['GET'])
    def export_credentials(self, ids=None, **kw):
        """Temporary passwords of the users created from the customer file (Excel),
        downloaded once: they are removed from Odoo by the download."""
        if not request.env.user.has_group('m365_license_management.group_m365_admin'):
            return request.not_found()
        record_ids = [int(i) for i in (ids or '').split(',') if i.strip().isdigit()]
        users = request.env['m365.tenant.pending.user'].search(
            [('id', 'in', record_ids)], order='tenant_id, sequence, id').filtered(
            lambda u: u.sudo().password)
        if not users:
            return request.not_found()
        book = openpyxl.Workbook()
        self._sheet(book, 'Credentials', [
            'Tenant', 'User Principal Name', 'Display Name', 'Temporary Password',
            'Created On'], [[
                u.tenant_id.name, u.upn, u.name, u.sudo().password,
                fields.Datetime.to_string(u.created_date),
            ] for u in users], first=True)
        out = io.BytesIO()
        book.save(out)
        users.sudo().write({'password': False})
        for tenant in users.tenant_id:
            tenant._log('create_user', 'info', 'Temporary passwords of %s user(s) downloaded '
                        'by %s and removed from Odoo.' % (
                            len(users.filtered(lambda u: u.tenant_id == tenant)),
                            request.env.user.name))
        return request.make_response(out.getvalue(), headers=[
            ('Content-Type',
             'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', content_disposition('m365_credentials.xlsx')),
            ('Cache-Control', 'no-store'),
        ])
