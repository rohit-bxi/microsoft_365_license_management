# -*- coding: utf-8 -*-
"""Invoices and renewals of Ingram Micro, read for the license orders of Odoo.

=========================================  ===============================================
Data                                       Ingram Micro endpoint
=========================================  ===============================================
Invoices (search, 100 per page)            GET  /resellers/v6/invoices
Invoice lines and totals                   GET  /resellers/v6.1/invoices/{invoiceNumber}
Renewals                                   POST /resellers/v6/renewals/search
=========================================  ===============================================

Invoices are matched to the orders by PO number (``customerOrderNumber``) or Ingram
order number; their lines are read for the matched ones only. Renewals are matched by PO
number; administrators get a to-do 30 days before the expiration.
"""
import logging
import urllib.parse
from datetime import date, timedelta

from odoo import _, api, fields, models
from odoo.tools import config

from .m365_ingram_account import M365IngramError

_logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_PAGES = 50
FIRST_SYNC_DAYS = 90       # invoices read on the first synchronisation
SYNC_OVERLAP_DAYS = 7      # invoices are read again for a week (status changes)
RENEWAL_ALERT_DAYS = 30


def _date(value):
    """``2022-10-29`` or ``2023-07-04T00:00:00`` -> date (or False)."""
    try:
        return date.fromisoformat(str(value)[:10]) if value else False
    except ValueError:
        return False


class M365IngramInvoice(models.Model):
    _name = 'm365.ingram.invoice'
    _description = 'Ingram Micro Invoice'
    _order = 'invoice_date desc, id desc'
    _rec_name = 'invoice_number'

    account_id = fields.Many2one('m365.ingram.account', required=True, ondelete='cascade',
                                 index=True)
    invoice_number = fields.Char(string="Invoice Number", required=True, index=True)
    status = fields.Char()
    invoice_type = fields.Char(string="Type")
    invoice_date = fields.Date(string="Invoice Date")
    due_date = fields.Date(string="Due Date")
    customer_order_number = fields.Char(string="PO Number", index=True)
    end_customer_order_number = fields.Char(string="End Customer Reference")
    erp_order_number = fields.Char(string="Ingram Order Number")
    amount_due = fields.Float(string="Amount Due", digits=(16, 2))
    amount_incl_tax = fields.Float(string="Amount incl. Tax", digits=(16, 2))
    net_amount = fields.Float(string="Net Amount", digits=(16, 2))
    tax_amount = fields.Float(string="Tax", digits=(16, 2))
    currency_id = fields.Many2one('res.currency', string="Currency")
    order_id = fields.Many2one('m365.ingram.order', string="License Order", index=True,
                               ondelete='set null')
    tenant_id = fields.Many2one(related='order_id.tenant_id', store=True, string="Tenant")
    line_ids = fields.One2many('m365.ingram.invoice.line', 'invoice_id', string="Lines")
    details_loaded = fields.Boolean(readonly=True)
    last_refresh = fields.Datetime(readonly=True)

    _account_number_uniq = models.Constraint(
        'unique(account_id, invoice_number)', 'This invoice already exists.')

    def _load_details(self):
        """Lines and totals (GET /resellers/v6.1/invoices/{n})."""
        Currency = self.env['res.currency'].with_context(active_test=False)
        for invoice in self:
            _status, data = invoice.account_id._im_request(
                'GET', '/resellers/v6.1/invoices/%s'
                % urllib.parse.quote(invoice.invoice_number, safe=''))
            totals = (data.get('summary') or {}).get('totals') or {}
            lines = []
            for line in data.get('lines') or []:
                code = line.get('currencyCode')
                lines.append((0, 0, {
                    'ingram_line_number': line.get('ingramLineNumber') or False,
                    'ingram_part_number': line.get('ingramPartNumber') or False,
                    'vendor_part_number': line.get('vendorPartNumber') or False,
                    'description': line.get('productDescription') or False,
                    'quantity': line.get('quantity') or 0,
                    'unit_price': line.get('unitPrice') or 0.0,
                    'extended_price': line.get('extendedPrice') or 0.0,
                    'tax_amount': line.get('taxAmount') or 0.0,
                    'currency_id': Currency.search([('name', '=', code)], limit=1).id
                    if code else False,
                }))
            vals = {'line_ids': [(5, 0, 0)] + lines, 'details_loaded': True,
                    'net_amount': totals.get('netInvoiceAmount') or 0.0,
                    'tax_amount': totals.get('totalTaxAmount') or 0.0,
                    'invoice_type': data.get('invoiceType') or invoice.invoice_type}
            if totals.get('invoicedAmountDue') is not None:
                vals['amount_due'] = totals['invoicedAmountDue']
            if not invoice.currency_id and lines:
                vals['currency_id'] = lines[0][2]['currency_id']
            invoice.sudo().write(vals)

    def action_load_details(self):
        self.env['m365.tenant']._check_admin()
        self._load_details()
        return True


class M365IngramInvoiceLine(models.Model):
    _name = 'm365.ingram.invoice.line'
    _description = 'Ingram Micro Invoice Line'
    _order = 'invoice_id, id'

    invoice_id = fields.Many2one('m365.ingram.invoice', required=True, ondelete='cascade',
                                 index=True)
    tenant_id = fields.Many2one(related='invoice_id.tenant_id', store=True)
    ingram_line_number = fields.Char(string="Line")
    ingram_part_number = fields.Char(string="Ingram Part Number")
    vendor_part_number = fields.Char(string="Vendor Part Number")
    description = fields.Char()
    quantity = fields.Integer()
    unit_price = fields.Float(digits=(16, 2))
    extended_price = fields.Float(string="Amount", digits=(16, 2))
    tax_amount = fields.Float(string="Tax", digits=(16, 2))
    currency_id = fields.Many2one('res.currency')


class M365IngramRenewal(models.Model):
    _name = 'm365.ingram.renewal'
    _description = 'Ingram Micro Renewal'
    _order = 'expiration_date, id'
    _rec_name = 'renewal_id'

    account_id = fields.Many2one('m365.ingram.account', required=True, ondelete='cascade',
                                 index=True)
    renewal_id = fields.Char(string="Renewal ID", required=True, index=True)
    customer_order_number = fields.Char(string="PO Number", index=True)
    reference_number = fields.Char(string="Reference")
    end_user = fields.Char(string="End User")
    vendor = fields.Char()
    expiration_date = fields.Date(string="Expires On")
    renewal_value = fields.Float(string="Value", digits=(16, 2))
    status = fields.Char()
    order_id = fields.Many2one('m365.ingram.order', string="License Order", index=True,
                               ondelete='set null')
    tenant_id = fields.Many2one(related='order_id.tenant_id', store=True, string="Tenant")
    alert_sent = fields.Boolean(readonly=True)
    last_refresh = fields.Datetime(readonly=True)

    _account_renewal_uniq = models.Constraint(
        'unique(account_id, renewal_id)', 'This renewal already exists.')


class M365IngramAccountBilling(models.Model):
    _inherit = 'm365.ingram.account'

    invoice_count = fields.Integer(compute='_compute_billing_counts')
    renewal_count = fields.Integer(compute='_compute_billing_counts')

    def _compute_billing_counts(self):
        Invoice, Renewal = self.env['m365.ingram.invoice'], self.env['m365.ingram.renewal']
        for rec in self:
            rec.invoice_count = Invoice.search_count([('account_id', '=', rec.id)])
            rec.renewal_count = Renewal.search_count([('account_id', '=', rec.id)])

    def _orders_by_reference(self):
        """Orders of the account by PO number and by Ingram order number."""
        self.ensure_one()
        orders = self.env['m365.ingram.order'].sudo().search(
            [('account_id', '=', self.id), ('state', 'not in', ('draft', 'rejected'))])
        index = {}
        for order in orders:
            index[order.name] = order
            for number in (order.ingram_order_numbers or '').split(', '):
                if number:
                    index[number] = order
        return index

    def _sync_invoices(self, date_from=None):
        """Read the invoices since ``date_from`` (default: last sync less a week).

        :return: number of invoices read
        """
        self.ensure_one()
        today = fields.Date.context_today(self)
        if not date_from:
            date_from = (self.last_invoice_sync.date() - timedelta(days=SYNC_OVERLAP_DAYS)
                         if self.last_invoice_sync else today - timedelta(days=FIRST_SYNC_DAYS))
        orders = self._orders_by_reference()
        Invoice = self.env['m365.ingram.invoice'].sudo()
        Currency = self.env['res.currency'].with_context(active_test=False)
        existing = {i.invoice_number: i for i in Invoice.search([('account_id', '=', self.id)])}
        count, to_detail = 0, Invoice.browse()
        for page in range(1, MAX_PAGES + 1):
            _status, data = self._im_request('GET', '/resellers/v6/invoices', params={
                'invoiceFromDate': date_from.isoformat(), 'invoiceToDate': today.isoformat(),
                'pageSize': PAGE_SIZE, 'pageNumber': page})
            items = [i for i in data.get('invoices') or [] if isinstance(i, dict)]
            for item in items:
                number = str(item.get('invoiceNumber') or '')
                if not number:
                    continue
                count += 1
                erp = item.get('erpOrderNumber') or ''
                order = (orders.get(item.get('customerOrderNumber') or '')
                         or orders.get(erp) or orders.get(erp.rsplit('-', 1)[0]))
                vals = {
                    'status': item.get('invoiceStatus') or False,
                    'invoice_date': _date(item.get('invoiceDate')),
                    'due_date': _date(item.get('invoiceDueDate')
                                      or item.get('paymentTermsDueDate')),
                    'customer_order_number': item.get('customerOrderNumber') or False,
                    'end_customer_order_number': item.get('endCustomerOrderNumber') or False,
                    'erp_order_number': erp or False,
                    'amount_due': item.get('invoicedAmountDue') or 0.0,
                    'amount_incl_tax': item.get('invoiceAmountInclTax') or 0.0,
                    'order_id': order.id if order else False,
                    'last_refresh': fields.Datetime.now(),
                }
                if item.get('currencyCode'):
                    vals['currency_id'] = Currency.search(
                        [('name', '=', item['currencyCode'])], limit=1).id or False
                invoice = existing.get(number)
                if invoice:
                    invoice.write(vals)
                else:
                    invoice = existing[number] = Invoice.create(
                        dict(vals, account_id=self.id, invoice_number=number))
                if invoice.order_id and not invoice.details_loaded:
                    to_detail |= invoice
            total = data.get('recordsFound') or 0
            if len(items) < PAGE_SIZE or page * PAGE_SIZE >= total:
                break
        for invoice in to_detail:
            try:
                invoice._load_details()
            except M365IngramError as exc:
                _logger.info("Lines of Ingram Micro invoice %s not read: %s",
                             invoice.invoice_number, exc)
        self.write({'last_invoice_sync': fields.Datetime.now()})
        return count

    def _sync_renewals(self):
        """Read the renewals of the vendor. :return: number of renewals read"""
        self.ensure_one()
        orders = self._orders_by_reference()
        Renewal = self.env['m365.ingram.renewal'].sudo()
        existing = {r.renewal_id: r for r in Renewal.search([('account_id', '=', self.id)])}
        count = 0
        for page in range(1, MAX_PAGES + 1):
            _status, data = self._im_request(
                'POST', '/resellers/v6/renewals/search',
                params={'page': page, 'size': PAGE_SIZE}, json_body={'vendor': self.vendor_name})
            items = data.get('renewals') or []
            items = [items] if isinstance(items, dict) else [i for i in items
                                                             if isinstance(i, dict)]
            for item in items:
                renewal_id = str(item.get('renewalId') or '')
                if not renewal_id:
                    continue
                count += 1
                order = orders.get(item.get('customerOrderNumber') or '')
                vals = {
                    'customer_order_number': item.get('customerOrderNumber') or False,
                    'reference_number': item.get('referenceNumber') or False,
                    'end_user': item.get('endUser') or False,
                    'vendor': item.get('vendor') or False,
                    'expiration_date': _date(item.get('expirationDate')),
                    'renewal_value': item.get('renewalValue') or 0.0,
                    'status': item.get('status') or False,
                    'order_id': order.id if order else False,
                    'last_refresh': fields.Datetime.now(),
                }
                if renewal_id in existing:
                    existing[renewal_id].write(vals)
                else:
                    existing[renewal_id] = Renewal.create(
                        dict(vals, account_id=self.id, renewal_id=renewal_id))
            total = data.get('recordsFound') or 0
            if len(items) < PAGE_SIZE or page * PAGE_SIZE >= total:
                break
        self._alert_renewals()
        self.write({'last_renewal_sync': fields.Datetime.now()})
        return count

    def _alert_renewals(self):
        """To-do on the order of the renewals expiring within 30 days, once."""
        limit = fields.Date.context_today(self) + timedelta(days=RENEWAL_ALERT_DAYS)
        renewals = self.env['m365.ingram.renewal'].sudo().search([
            ('account_id', 'in', self.ids), ('alert_sent', '=', False),
            ('order_id', '!=', False), ('expiration_date', '!=', False),
            ('expiration_date', '<=', limit)])
        for renewal in renewals:
            renewal.order_id._schedule_admin_activity(
                _("Renewal of %(order)s expires on %(date)s", order=renewal.order_id.name,
                  date=renewal.expiration_date),
                _("Ingram Micro renewal %(id)s (%(status)s), value %(value)s.",
                  id=renewal.renewal_id, status=renewal.status or '-',
                  value=renewal.renewal_value))
            renewal.alert_sent = True

    def action_sync_billing(self):
        self._check_admin()
        messages = []
        for rec in self:
            invoices = rec._sync_invoices()
            renewals = rec._sync_renewals()
            messages.append(_("%(account)s: %(invoices)s invoice(s), %(renewals)s "
                              "renewal(s) read.", account=rec.name, invoices=invoices,
                              renewals=renewals))
        return self.env['m365.tenant']._notify("\n".join(messages))

    def action_view_invoices(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_ingram_invoice')
        action['domain'] = [('account_id', '=', self.id)]
        return action

    def action_view_renewals(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_ingram_renewal')
        action['domain'] = [('account_id', '=', self.id)]
        return action

    @api.model
    def _cron_sync_billing(self):
        """Daily: invoices and renewals of every connected account."""
        for account in self.search([('state', '=', 'connected')]):
            for method in (account._sync_invoices, account._sync_renewals):
                try:
                    method()
                except M365IngramError as exc:
                    _logger.warning("Ingram Micro account %s: %s", account.id, exc)
            if not config['test_enable']:
                self.env.cr.commit()
