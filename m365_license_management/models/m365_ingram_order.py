# -*- coding: utf-8 -*-
"""License orders: Microsoft subscriptions bought from Ingram Micro for a tenant.

::

    Draft --Submit--> Waiting Approval --Approve--> Sending --accepted--> Sent
                            |                          |                    |
                            +--Reject--> Rejected      +--error--> Failed <-+-- webhook error
                                                                     |
                                                       Retry (same PO number) --> Sending
    Sent --webhook: Ingram order created--> Confirmed --licenses seen in Graph--> Delivered

* Approval: always by a Microsoft 365 administrator, never automatic.
* No duplicate order: the PO number (``customerOrderNumber``) is saved before the call.
  When Ingram Micro gave no answer, *Retry* first looks for the PO number at Ingram Micro
  and sends the order again only if it is not there.
* Ingram Micro creates orders in the background and calls the webhook
  ``/m365/ingram/webhook/<account>`` (signed ``x-hub-signature``) with the result.
* Delivered: the inventory of the tenant shows the ordered quantity more than when the
  order was sent (Ingram Micro does not return the subscriptions yet).
"""
import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import config

from .m365_ingram_account import M365IngramError, M365IngramTimeout
from .m365_tenant import M365GraphError

_logger = logging.getLogger(__name__)

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
DELIVERY_ALERT_AFTER = timedelta(hours=24)
FOLLOW_UP_AFTER = timedelta(minutes=30)      # sent, no webhook yet: look it up
SENDING_STALE = timedelta(minutes=15)        # no answer recorded: the call was cut
CONFIRMED_CHECK_AFTER = timedelta(hours=2)   # not delivered: cancelled at Ingram?
FOLLOW_UP_ALERT_AFTER = timedelta(hours=24)
ERROR_WORDS = ('error', 'reject', 'fail', 'invalid')
CANCEL_WORDS = ('cancel', 'void')
# Limits of the v7 order (Ingram Micro specification)
PO_MAX = 18                   # customerOrderNumber
END_CUSTOMER_REF_MAX = 18     # endCustomerOrderNumber
LINES_MAX = 999               # customerLineNumber: 3 characters
END_USER_LIMITS = {'companyName': 40, 'addressLine1': 70, 'addressLine2': 70, 'city': 25,
                   'state': 3, 'postalCode': 10, 'countryCode': 10}


class M365IngramOrder(models.Model):
    _name = 'm365.ingram.order'
    _description = 'Microsoft 365 License Order (Ingram Micro)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string="PO Number", default=lambda self: _('New'), readonly=True,
                       copy=False, index=True,
                       help="Sent to Ingram Micro as customerOrderNumber.")
    account_id = fields.Many2one(
        'm365.ingram.account', string="Ingram Micro Account", required=True, tracking=True,
        default=lambda self: self.env['m365.ingram.account']._get_default())
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True, tracking=True,
                                index=True,
                                domain="['|', ('tenant_type', '=', 'existing'), "
                                       "('provision_state', '=', 'done')]")
    customer_id = fields.Many2one(related='tenant_id.tenant_customer_id', store=True,
                                  string="Customer")
    line_ids = fields.One2many('m365.ingram.order.line', 'order_id', string="Lines",
                               copy=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Waiting Approval'),
        ('sending', 'Sending'),
        ('sent', 'Sent'),
        ('confirmed', 'Confirmed'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ], string="Status", default='draft', required=True, tracking=True, copy=False,
        index=True)
    reason = fields.Text(string="Business Justification")
    reject_reason = fields.Text(string="Rejection Reason", copy=False)
    requested_by = fields.Many2one('res.users', string="Requested By", readonly=True,
                                   default=lambda self: self.env.user, copy=False)
    approver_id = fields.Many2one('res.users', string="Approved / Rejected By", readonly=True,
                                  copy=False)
    send_attempted = fields.Boolean(
        readonly=True, copy=False,
        help="The order was sent to Ingram Micro without a clear answer: it may exist there.")
    confirmation_number = fields.Char(string="Confirmation Number", readonly=True, copy=False)
    ingram_order_numbers = fields.Char(string="Ingram Order Number(s)", readonly=True,
                                       copy=False)
    sent_date = fields.Datetime(string="Sent On", readonly=True, copy=False)
    confirmed_date = fields.Datetime(string="Confirmed On", readonly=True, copy=False)
    delivered_date = fields.Datetime(string="Delivered On", readonly=True, copy=False)
    attempt_date = fields.Datetime(string="Last Sending Attempt", readonly=True, copy=False)
    delivery_alert_sent = fields.Boolean(readonly=True, copy=False)
    followup_alert_sent = fields.Boolean(readonly=True, copy=False)
    invoice_ids = fields.One2many('m365.ingram.invoice', 'order_id', string="Invoices")
    invoice_count = fields.Integer(compute='_compute_invoice_count')
    error_message = fields.Text(string="Error", readonly=True, copy=False)
    currency_id = fields.Many2one('res.currency', compute='_compute_amount', store=True)
    amount_total = fields.Float(string="Total", compute='_compute_amount', store=True,
                                digits=(16, 2))
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)
    is_admin = fields.Boolean(compute='_compute_is_admin')

    @api.depends('line_ids.subtotal', 'line_ids.currency_id')
    def _compute_amount(self):
        for rec in self:
            rec.amount_total = sum(rec.line_ids.mapped('subtotal'))
            rec.currency_id = rec.line_ids.currency_id[:1]

    @api.depends_context('uid')
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group(ADMIN_GROUP)
        for rec in self:
            rec.is_admin = is_admin

    @api.depends('invoice_ids')
    def _compute_invoice_count(self):
        for rec in self:
            rec.invoice_count = len(rec.invoice_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = (self.env['ir.sequence'].next_by_code('m365.ingram.order')
                                or _('New'))
        return super().create(vals_list)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_sent(self):
        if self.filtered(lambda o: o.state not in ('draft', 'rejected', 'cancelled')
                         or o.send_attempted):
            raise UserError(_("An order sent to Ingram Micro cannot be deleted."))

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    def _commit(self):
        """Keep the result of Ingram Micro calls, which cannot be undone."""
        if not config['test_enable']:
            self.env.cr.commit()

    def _log(self, status, message):
        self.ensure_one()
        return self.tenant_id._log('license_order', status, "%s: %s" % (self.name, message))

    def _admin_users(self):
        users = self.env['res.users'].sudo().search([('share', '=', False)])
        return users.filtered(lambda u: u.has_group(ADMIN_GROUP))

    def _schedule_admin_activity(self, summary, note):
        """To-do of the administrators, once while it is open."""
        if self.sudo().activity_ids.filtered(lambda a: a.summary == summary):
            return
        for admin in self._admin_users():
            self.sudo().activity_schedule('mail.mail_activity_data_todo', user_id=admin.id,
                                          summary=summary, note=note)

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _validate(self):
        self.ensure_one()
        tenant, account = self.tenant_id, self.account_id
        if not self.line_ids:
            raise UserError(_("Add the plans to buy."))
        if self.create_tenant:
            self._validate_tenant_creation()
        elif not tenant.filtered_domain(tenant._live_domain()):
            raise UserError(_("Create the tenant in Microsoft 365 first."))
        if len(self.name or '') > PO_MAX:
            raise UserError(_("The PO number %s is longer than the 18 characters Ingram Micro "
                              "accepts: shorten the prefix of its sequence.", self.name))
        if len(self.line_ids) > LINES_MAX:
            raise UserError(_("An order has at most %s lines.", LINES_MAX))
        if len(self.line_ids.currency_id) > 1:
            raise UserError(_("All the lines of an order must be in the same currency."))
        # A tenant created by Ingram Micro gets its relationship with the tenant
        if (account.require_relationship and not tenant.ingram_relationship
                and not self.create_tenant):
            raise UserError(_(
                "Confirm first that tenant %s has a reseller relationship with Ingram Micro "
                "(Purchasing tab of the tenant): Ingram Micro can only order for the "
                "customers it is a partner of.", tenant.name))
        if (not self.create_tenant and not tenant._initial_domain_prefix()
                and 'microsoft_domain' not in account.vmf_attribute_ids.mapped('name')):
            raise UserError(_(
                "The .onmicrosoft.com domain of tenant %s is not known: Ingram Micro needs it "
                "to find the tenant. Connect the tenant and refresh its domains.", tenant.name))
        if account.require_mca and not tenant._mca_recorded():
            raise UserError(_(
                "Record first who accepted the Microsoft Customer Agreement for tenant %s, "
                "and when (Purchasing tab of the tenant).", tenant.name))
        for line in self.line_ids:
            plan = line.plan_id
            if plan.account_id != self.account_id:
                raise ValidationError(_("%s is not a plan of this Ingram Micro account.",
                                        plan.name))
            if line.term_id and line.term_id.plan_id != plan:
                raise UserError(_("%(period)s is not a period of the plan %(plan)s.",
                                  period=line.term_id.name, plan=plan.name))
            if len(plan.term_ids) > 1 and not line.term_id:
                raise UserError(_("Choose the subscription period of %(plan)s (%(periods)s).",
                                  plan=plan.name, periods=", ".join(plan.term_ids.mapped('name'))))
            if plan.min_units and line.quantity < plan.min_units:
                raise UserError(_("%(plan)s: at least %(count)s units.", plan=plan.name,
                                  count=plan.min_units))
            if plan.max_units and line.quantity > plan.max_units:
                raise UserError(_("%(plan)s: at most %(count)s units.", plan=plan.name,
                                  count=plan.max_units))

    def action_submit(self):
        for rec in self.filtered(lambda o: o.state == 'draft'):
            rec._validate()
            rec.write({'state': 'submitted', 'requested_by': self.env.uid})
            rec._log('info', _("submitted by %s.", self.env.user.name))
            rec._schedule_admin_activity(
                _("Approve license order %s", rec.name),
                _("%(tenant)s: %(lines)s", tenant=rec.tenant_id.name, lines=", ".join(
                    "%s x %s" % (line.quantity, line.plan_id.name) for line in rec.line_ids)))
        return True

    def action_approve(self):
        self._check_admin()
        for rec in self.filtered(lambda o: o.state == 'submitted'):
            rec._validate()
            rec.write({'approver_id': self.env.uid})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
            rec._log('info', _("approved by %s.", self.env.user.name))
            rec._send()
        return True

    def action_reject(self):
        self._check_admin()
        for rec in self.filtered(lambda o: o.state == 'submitted'):
            rec.write({'state': 'rejected', 'approver_id': self.env.uid})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
            rec._log('info', _("rejected by %(user)s. %(reason)s", user=self.env.user.name,
                               reason=rec.reject_reason or ''))
        return True

    def action_retry(self):
        """Send again a failed order, or one whose sending was cut (no answer recorded).
        An order that may exist at Ingram Micro is looked for first."""
        self._check_admin()
        now = fields.Datetime.now()
        for rec in self.filtered(lambda o: o.state in ('failed', 'sending')):
            if rec.state == 'sending' and rec.attempt_date and \
                    rec.attempt_date > now - SENDING_STALE:
                raise UserError(_("%s is being sent: wait a few minutes.", rec.name))
            rec._validate()
            rec._send()
        return True

    def action_cancel(self):
        for rec in self:
            if rec.state not in ('draft', 'submitted', 'failed'):
                raise UserError(_("Only orders not sent to Ingram Micro can be cancelled."))
            if rec.send_attempted:
                raise UserError(_("Ingram Micro may have received %s: use Retry, which looks "
                                  "for it at Ingram Micro first.", rec.name))
            if rec.state != 'draft':
                rec._check_admin()
            rec.write({'state': 'cancelled'})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
        return True

    def action_reset_draft(self):
        for rec in self.filtered(lambda o: o.state in ('rejected', 'cancelled')):
            vals = {'state': 'draft', 'approver_id': False, 'error_message': False}
            if rec.sent_date or rec.ingram_order_numbers or rec.attempt_date:
                # The PO number is used at Ingram Micro, which refuses it a second time
                vals.update({
                    'name': self.env['ir.sequence'].next_by_code('m365.ingram.order')
                    or _('New'),
                    'confirmation_number': False, 'ingram_order_numbers': False,
                    'sent_date': False, 'confirmed_date': False, 'attempt_date': False,
                    'send_attempted': False, 'delivery_alert_sent': False,
                    'followup_alert_sent': False,
                })
                rec.line_ids.write({'baseline_units': 0, 'delivered': False})
                rec.message_post(body=_("New PO number %s: the previous one was used at "
                                        "Ingram Micro.", vals['name']))
            rec.write(vals)
        return True

    def action_cancel_at_ingram(self):
        """Cancel the order at Ingram Micro: possible only before it is released."""
        self._check_admin()
        self.ensure_one()
        if self.state not in ('sent', 'confirmed'):
            raise UserError(_("Only orders sent to Ingram Micro and not delivered can be "
                              "cancelled there."))
        numbers = [n for n in (self.ingram_order_numbers or '').split(', ') if n]
        if not numbers:
            raise UserError(_("Ingram Micro has not given the order number of %s yet: wait "
                              "for its confirmation.", self.name))
        errors = []
        for number in numbers:
            try:
                self.account_id._im_request(
                    'DELETE', '/resellers/v6/orders/%s' % urllib.parse.quote(number, safe=''))
            except M365IngramError as exc:
                errors.append("%s: %s" % (number, exc))
        if errors:
            message = _("Not cancelled at Ingram Micro (an order released to the warehouse "
                        "cannot be cancelled): %s", "; ".join(errors))
            self._log('failed', message)
            return self.env['m365.tenant']._notify(message, 'danger')
        self._mark_cancelled(_("Cancelled at Ingram Micro by %s.", self.env.user.name))
        return self.env['m365.tenant']._notify(_("%s is cancelled at Ingram Micro.",
                                                 self.name))

    def action_view_invoices(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_ingram_invoice')
        action['domain'] = [('order_id', '=', self.id)]
        return action

    def action_get_prices(self):
        self._check_admin()
        for rec in self:
            plans = rec.line_ids.plan_id
            plans._refresh_details()
            plans._refresh_prices()
            for line in rec.line_ids:
                line.write({'unit_price': line.plan_id.unit_price,
                            'currency_id': line.plan_id.currency_id.id})
        return True

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def _vmf_value(self, source, fixed=None, line=None):
        """Value of a vendor field for this order (see m365.ingram.vmf.attribute)."""
        tenant = self.tenant_id
        customer = tenant.tenant_customer_id
        contact = self._contact()
        values = {
            'tenant_id': tenant.tenant_id,
            'domain': tenant.domain,
            'customer_name': customer.name or tenant.name,
            'contact_first_name': contact['first_name'],
            'contact_last_name': contact['last_name'],
            'contact_email': contact['email'],
            'contact_phone': contact['phone'],
            'order_date': fields.Date.context_today(self).isoformat(),
            'mca_first_name': tenant.mca_first_name,
            'mca_last_name': tenant.mca_last_name,
            'mca_email': tenant.mca_email,
            'mca_phone': tenant.mca_phone,
            'mca_date': tenant.mca_date.isoformat() if tenant.mca_date else '',
            'plan_id': line.plan_id.plan_id if line else '',
            'quantity': line.quantity if line else '',
            'term': line.term_id.code if line else '',
            'term_months': line.term_id.months if line and line.term_id else '',
            'term_key': line.term_id.key if line else '',
            'fixed': fixed or '',
        }
        return values.get(source) or ''

    def _contact(self):
        """Contact of the customer: tenant creation data, else the tenant customer."""
        tenant = self.tenant_id
        customer = tenant.tenant_customer_id
        person = customer.child_ids[:1] or customer
        first, last = tenant.contact_first_name, tenant.contact_last_name
        if not (first or last) and person.name:
            first, _sep, last = person.name.partition(' ')
        return {
            'first_name': first or '', 'last_name': last or '',
            'email': tenant.contact_email or person.email or customer.email or '',
            'phone': tenant.contact_phone or person.phone or customer.phone or '',
        }

    def _state_code(self):
        """State of the end user as a code of 3 characters at most (Ingram's limit)."""
        tenant = self.tenant_id
        customer = tenant.tenant_customer_id
        if customer.state_id.code:
            return customer.state_id.code
        text = (tenant.address_state or '').strip()
        if len(text) <= 3:
            return text.upper()
        country = customer.country_id or tenant.country_id
        state = self.env['res.country.state'].search([
            ('country_id', '=', country.id), '|', ('name', '=ilike', text),
            ('code', '=ilike', text)], limit=1) if country else False
        return state.code if state and len(state.code) <= 3 else ''

    def _end_user_info(self):
        tenant = self.tenant_id
        customer = tenant.tenant_customer_id
        contact = self._contact()
        info = {
            'companyName': customer.name or tenant.name,
            'contact': " ".join(filter(None, (contact['first_name'], contact['last_name']))),
            'addressLine1': customer.street or tenant.address_line1,
            'addressLine2': customer.street2 or tenant.address_line2,
            'city': customer.city or tenant.city,
            'state': self._state_code(),
            'postalCode': customer.zip or tenant.postal_code,
            'countryCode': customer.country_id.code or tenant.country_id.code,
            'phoneNumber': contact['phone'],
            'email': contact['email'],
        }
        return {key: value[:END_USER_LIMITS[key]] if key in END_USER_LIMITS else value
                for key, value in info.items() if value}

    def _end_customer_reference(self):
        """endCustomerOrderNumber: the customer reference, else the first label of the
        domain, at most 18 characters."""
        customer = self.tenant_id.tenant_customer_id
        ref = (customer.ref or '').strip() or (self.tenant_id.domain or '').split('.')[0]
        return ref[:END_CUSTOMER_REF_MAX]

    def _order_body(self):
        """Body of POST /resellers/v7/orders."""
        self.ensure_one()
        account = self.account_id
        body = {
            'customerOrderNumber': self.name,
            'endCustomerOrderNumber': self._end_customer_reference(),
            'endUserInfo': self._end_user_info(),
            'lines': [],
        }
        if account.bill_to_address_id:
            body['billToAddressId'] = account.bill_to_address_id
        header = account.vmf_attribute_ids.filtered(lambda a: a.level == 'header')
        if header:
            body['vmfAdditionalAttributes'] = [attr._payload(self) for attr in header]
        line_attrs = account.vmf_attribute_ids.filtered(lambda a: a.level == 'line')
        # Vendor required info of the plans: the tenant to create, or the existing tenant
        required = self._creation_required_info()
        for number, line in enumerate(self.line_ids, start=1):
            plan = line.plan_id
            vals = {'customerLineNumber': str(number), 'quantity': line.quantity}
            if plan.ingram_part_number:
                vals['ingramPartNumber'] = plan.ingram_part_number
            if plan.vendor_part_number:
                vals['vendorPartNumber'] = plan.vendor_part_number
            if line_attrs:
                vals['vmfAdditionalAttributesLines'] = [attr._payload(self, line)
                                                        for attr in line_attrs]
            vals.update(self._vendor_info_line(line, required.get(plan.plan_id)))
            body['lines'].append(vals)
        return body

    def _find_at_ingram(self):
        """Orders of Ingram Micro with this PO number."""
        self.ensure_one()
        _status, data = self.account_id._im_request(
            'GET', '/resellers/v6/orders/search',
            params={'customerOrderNumber': self.name, 'pageSize': 25, 'pageNumber': 1})
        return [o for o in data.get('orders') or []
                if isinstance(o, dict) and o.get('customerOrderNumber', self.name) == self.name]

    def _send(self):
        """Send the order to Ingram Micro. Failures are stored, never raised."""
        self.ensure_one()
        account = self.account_id
        try:
            if self.send_attempted:
                # The last call got no answer: the order may exist at Ingram Micro
                found = self._find_at_ingram()
                if found:
                    self._apply_found(found, _("found at Ingram Micro"))
                    return True
            for line in self.line_ids.filtered(lambda l: not (
                    l.plan_id.ingram_part_number or l.plan_id.vendor_part_number)):
                line.plan_id._refresh_details()
            missing = self.line_ids.filtered(lambda l: not (
                l.plan_id.ingram_part_number or l.plan_id.vendor_part_number))
            if missing:
                raise M365IngramError(_("Ingram Micro returned no part number for %s.",
                                        ", ".join(missing.plan_id.mapped('name'))))
            body = self._order_body()
        except M365IngramError as exc:
            self.write({'state': 'failed', 'error_message': str(exc)})
            self._log('failed', str(exc))
            return False
        for line in self.line_ids:
            line._set_baseline()
        # Saved before the call: a retry after no answer looks for the order first
        self.write({'state': 'sending', 'send_attempted': True, 'error_message': False,
                    'attempt_date': fields.Datetime.now()})
        self._commit()
        try:
            _status, data = account._im_request('POST', '/resellers/v7/orders',
                                                json_body=body)
        except M365IngramTimeout as exc:
            message = _("%s Retry looks for the order at Ingram Micro first, so that it is "
                        "not ordered twice.", exc)
            self.write({'state': 'failed', 'error_message': message})
            self._log('failed', message)
            self._commit()
            return False
        except M365IngramError as exc:
            # 4xx: refused, nothing created. 5xx: unknown, look for it on retry
            self.write({'state': 'failed', 'error_message': str(exc),
                        'send_attempted': bool(exc.status and exc.status >= 500)})
            self._log('failed', str(exc))
            self._commit()
            return False
        self.write({
            'state': 'sent', 'sent_date': fields.Datetime.now(),
            'confirmation_number': data.get('confirmationNumber') or False,
        })
        self.message_post(body=Markup("<p>%s</p>") % escape(_(
            "Order sent to Ingram Micro. Confirmation: %(number)s. %(message)s",
            number=data.get('confirmationNumber') or '-', message=data.get('message') or '')))
        self._log('success', _("sent to Ingram Micro, confirmation %s.",
                               data.get('confirmationNumber') or '-'))
        self._commit()
        return True

    def _confirm(self, numbers, origin):
        """Ingram Micro created the order(s)."""
        self.ensure_one()
        known = [n for n in (self.ingram_order_numbers or '').split(', ') if n]
        numbers = known + [n for n in numbers if n and n not in known]
        vals = {'ingram_order_numbers': ", ".join(numbers) or False, 'error_message': False,
                'send_attempted': False}
        if self.state in ('sending', 'sent', 'failed'):
            vals.update(state='confirmed', confirmed_date=fields.Datetime.now())
        self.write(vals)
        message = _("Ingram Micro order(s) %(numbers)s (%(origin)s).",
                    numbers=", ".join(numbers) or '-', origin=origin)
        self.message_post(body=Markup("<p>%s</p>") % escape(message))
        self._log('success', message)
        self._trigger_delivery_check()

    def _trigger_delivery_check(self):
        cron = self.env.ref('m365_license_management.ir_cron_m365_ingram_deliveries',
                            raise_if_not_found=False)
        if cron:  # licenses usually arrive within minutes
            cron.sudo()._trigger(at=datetime.now(timezone.utc).replace(tzinfo=None)
                                 + timedelta(minutes=5))

    def _apply_found(self, found, origin):
        """Orders found at Ingram Micro by the PO number, with their status."""
        self.ensure_one()
        numbers = [o.get('ingramOrderNumber') for o in found]
        statuses = {(o.get('orderStatus') or '').strip().upper() for o in found} - {''}
        if statuses and all(any(w in st.lower() for w in CANCEL_WORDS) for st in statuses):
            self._mark_cancelled(_("Ingram Micro order(s) %s cancelled (%s).",
                                   ", ".join(filter(None, numbers)) or '-', origin), numbers)
            return
        self._confirm(numbers, origin)
        if statuses & {'ON HOLD', 'BACKORDERED'}:
            self._attention(_("on hold or backordered at Ingram Micro (%s)",
                              ", ".join(sorted(statuses))))

    def _mark_cancelled(self, message, numbers=()):
        self.ensure_one()
        known = [n for n in (self.ingram_order_numbers or '').split(', ') if n]
        numbers = known + [n for n in numbers if n and n not in known]
        self.write({'state': 'cancelled', 'send_attempted': False,
                    'ingram_order_numbers': ", ".join(numbers) or False})
        self.message_post(body=Markup("<p>%s</p>") % escape(message))
        self._log('info', message)
        self.sudo().activity_unlink(['mail.mail_activity_data_todo'])

    def _attention(self, what):
        """Something administrators must look at, without changing the status."""
        self.ensure_one()
        message = _("%(order)s: %(what)s.", order=self.name, what=what)
        self.message_post(body=Markup("<p>%s</p>") % escape(message))
        self._log('info', what)
        self._schedule_admin_activity(_("License order %s needs attention", self.name),
                                      message)

    # ------------------------------------------------------------------
    # Webhook events
    # ------------------------------------------------------------------
    def _apply_event(self, event_name, numbers, errors, kinds=()):
        """Apply an event of Ingram Micro.

        :param kinds: statuses read from the event type and the line statuses:
            cancelled, partly_cancelled, hold, backorder, shipped
        """
        self.ensure_one()
        event_name = (event_name or '').lower()
        if 'cancelled' in kinds or any(word in event_name for word in CANCEL_WORDS):
            if self.state != 'delivered':
                self._mark_cancelled(_("Order cancelled at Ingram Micro (%s).",
                                       event_name or '-'), numbers)
            return
        if numbers:
            self._confirm(numbers, event_name or _("webhook"))
        if 'partly_cancelled' in kinds:
            self._attention(_("some lines are cancelled at Ingram Micro"))
        if 'hold' in kinds:
            self._attention(_("on hold at Ingram Micro"))
        if 'backorder' in kinds:
            self._attention(_("backordered at Ingram Micro"))
        if 'shipped' in kinds and self.state == 'confirmed':
            self._trigger_delivery_check()
        if errors:
            message = "; ".join(errors)
            if numbers:
                self.message_post(body=Markup("<p>%s</p>") % escape(
                    _("Ingram Micro reported line errors: %s", message)))
                self._log('failed', _("line errors: %s", message))
            elif self.state not in ('delivered', 'cancelled'):
                self.write({'state': 'failed', 'error_message': message,
                            'send_attempted': False})
                self._log('failed', message)
                self._schedule_admin_activity(
                    _("License order %s failed", self.name), message)

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    def _check_delivery(self):
        """Delivered once the tenant has the ordered quantity more than when sent."""
        now = fields.Datetime.now()
        for rec in self.filtered(lambda o: o.state == 'confirmed'):
            for line in rec.line_ids:
                line.delivered = line._is_delivered()
            if all(rec.line_ids.mapped('delivered')):
                rec.write({'state': 'delivered', 'delivered_date': now})
                rec.message_post(body=_("The licenses are in the tenant."))
                rec._log('success', _("delivered: the licenses are in the tenant."))
            elif (not rec.delivery_alert_sent and rec.confirmed_date
                  and rec.confirmed_date < now - DELIVERY_ALERT_AFTER):
                rec.delivery_alert_sent = True
                rec._schedule_admin_activity(
                    _("License order %s not delivered", rec.name),
                    _("Confirmed by Ingram Micro more than 24 hours ago, but the licenses "
                      "are not in the tenant %s yet.", rec.tenant_id.name))

    @api.model
    def _follow_up(self):
        """Orders whose result did not arrive by webhook: looked up by PO number.

        * Sending for more than 15 minutes (the call was cut): found, or failed;
        * Sent for more than 30 minutes: found, or an alert after 24 hours;
        * Confirmed for more than 2 hours and not delivered: cancelled at Ingram?
        """
        now = fields.Datetime.now()
        orders = self.search([
            '|', '|',
            '&', ('state', '=', 'sending'), ('attempt_date', '<', now - SENDING_STALE),
            '&', ('state', '=', 'sent'), ('sent_date', '<', now - FOLLOW_UP_AFTER),
            '&', ('state', '=', 'confirmed'),
            ('confirmed_date', '<', now - CONFIRMED_CHECK_AFTER),
        ])
        for order in orders:
            try:
                found = order._find_at_ingram()
            except M365IngramError as exc:
                _logger.info("License order %s not looked up: %s", order.name, exc)
                continue
            if order.state == 'confirmed':
                statuses = {(o.get('orderStatus') or '').upper() for o in found} - {''}
                if statuses and all('CANCEL' in st for st in statuses):
                    order._apply_found(found, _("hourly check"))
            elif found:
                order._apply_found(found, _("found by the hourly check"))
            elif order.state == 'sending':
                message = _("No answer of Ingram Micro was recorded and the order is not "
                            "found at Ingram Micro. Retry looks for it again before sending.")
                order.write({'state': 'failed', 'error_message': message})
                order._log('failed', message)
            elif (not order.followup_alert_sent
                  and order.sent_date < now - FOLLOW_UP_ALERT_AFTER):
                order.followup_alert_sent = True
                order._schedule_admin_activity(
                    _("License order %s not confirmed", order.name),
                    _("Sent to Ingram Micro more than 24 hours ago, but no webhook arrived "
                      "and Ingram Micro does not find its PO number. Check the webhook of "
                      "the Ingram Micro account."))
            self._commit()

    @api.model
    def _cron_check_deliveries(self):
        self._follow_up()
        orders = self.search([('state', '=', 'confirmed')])
        for tenant in orders.tenant_id:
            try:
                tenant._sync_inventory()
            except M365GraphError as exc:
                _logger.info("Inventory of tenant %s not read: %s", tenant.id, exc)
                continue
            orders.filtered(lambda o: o.tenant_id == tenant)._check_delivery()
            self._commit()


class M365IngramOrderLine(models.Model):
    _name = 'm365.ingram.order.line'
    _description = 'Microsoft 365 License Order Line'
    _order = 'order_id, sequence, id'

    order_id = fields.Many2one('m365.ingram.order', required=True, ondelete='cascade',
                               index=True)
    sequence = fields.Integer(default=10)
    account_id = fields.Many2one(related='order_id.account_id')
    tenant_id = fields.Many2one(related='order_id.tenant_id', store=True)
    plan_id = fields.Many2one('m365.ingram.plan', string="Plan", required=True,
                              domain="[('account_id', '=', account_id)]")
    product_id = fields.Many2one(related='plan_id.product_id', store=True,
                                 string="Catalogue Product")
    term_id = fields.Many2one('m365.ingram.plan.term', string="Subscription Period",
                              domain="[('plan_id', '=', plan_id)]",
                              help="Required when the plan is sold for several periods.")
    quantity = fields.Integer(required=True, default=1)
    unit_price = fields.Float(string="Unit Price", digits=(16, 2))
    currency_id = fields.Many2one('res.currency', string="Currency")
    subtotal = fields.Float(compute='_compute_subtotal', store=True, digits=(16, 2))
    baseline_units = fields.Integer(
        string="Licenses Before", readonly=True, copy=False,
        help="Licenses of the product in the tenant when the order was sent.")
    baseline_skus = fields.Text(
        readonly=True, copy=False,
        help="Licenses of every SKU of the tenant when the order was sent, for a plan "
             "without catalogue product (JSON).")
    delivered = fields.Boolean(readonly=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        Plan = self.env['m365.ingram.plan']
        for vals in vals_list:
            if vals.get('plan_id') and 'term_id' not in vals:
                vals['term_id'] = Plan.browse(vals['plan_id'])._default_term().id or False
        return super().create(vals_list)

    @api.onchange('plan_id')
    def _onchange_plan_term(self):
        if self.plan_id and self.term_id.plan_id != self.plan_id:
            self.term_id = self.plan_id._default_term()

    def _tenant_skus(self):
        return self.env['m365.sku'].sudo().search([('tenant_id', '=', self.tenant_id.id),
                                                   ('status', '!=', 'Deleted')])

    def _set_baseline(self):
        """Licenses in the tenant when the order is sent: of the product of the plan, or of
        every SKU when the plan has no product yet (a license never bought before)."""
        self.ensure_one()
        if self.product_id:
            self.write({'baseline_units': self.tenant_id._product_units(self.product_id),
                        'baseline_skus': False})
        else:
            self.write({'baseline_units': 0, 'baseline_skus': json.dumps(
                {s.sku_id: s.enabled_units for s in self._tenant_skus()})})

    def _is_delivered(self):
        """The ordered licenses are in the tenant. A plan without product is mapped to the
        license that grew by the ordered quantity."""
        self.ensure_one()
        if not self.baseline_skus:
            return (self.tenant_id._product_units(self.product_id)
                    >= self.baseline_units + self.quantity)
        try:
            before = json.loads(self.baseline_skus)
        except ValueError:
            before = {}
        grown = self._tenant_skus().filtered(
            lambda s: s.enabled_units - before.get(s.sku_id, 0) >= self.quantity)
        base = self.env['m365.ingram.account']._base_name(self.plan_id.name)
        named = grown.filtered(lambda s: self.env['m365.ingram.account']._base_name(
            s.product_id.name) == base)
        sku = named[:1] if len(named) == 1 else grown if len(grown) == 1 else None
        if not sku:
            return False
        if not self.plan_id.product_id:
            self.plan_id.sudo().write({'product_id': sku.product_id.id,
                                       'product_suggested': False})
            self.order_id._log('info', _("plan %(plan)s mapped to %(product)s, the license "
                                         "delivered in the tenant.", plan=self.plan_id.name,
                                         product=sku.product_id.name))
        return True

    _quantity_positive = models.Constraint('CHECK(quantity > 0)',
                                           'The quantity must be positive.')

    @api.depends('quantity', 'unit_price')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.unit_price

    @api.onchange('plan_id')
    def _onchange_plan_id(self):
        if self.plan_id:
            self.unit_price = self.plan_id.unit_price
            self.currency_id = self.plan_id.currency_id
            if self.plan_id.min_units and self.quantity < self.plan_id.min_units:
                self.quantity = self.plan_id.min_units


class M365IngramEvent(models.Model):
    """Notification received from Ingram Micro (webhook), processed in the background."""
    _name = 'm365.ingram.event'
    _description = 'Ingram Micro Webhook Event'
    _order = 'id desc'

    account_id = fields.Many2one('m365.ingram.account', required=True, ondelete='cascade',
                                 index=True)
    event_id = fields.Char(string="Event ID", required=True, index=True)
    event = fields.Char(string="Event")
    topic = fields.Char()
    received = fields.Datetime(default=fields.Datetime.now, readonly=True)
    payload = fields.Text(readonly=True)
    state = fields.Selection([('pending', 'To Process'), ('done', 'Processed'),
                              ('error', 'Error')], default='pending', index=True)
    message = fields.Text()
    order_ids = fields.Many2many('m365.ingram.order', string="Orders")

    _event_uniq = models.Constraint('unique(account_id, event_id)',
                                    'This event was already received.')

    @staticmethod
    def _signature_ok(secret, event_id, signature):
        """``x-hub-signature``: base64 of the HMAC-SHA512 of the event ID with the webhook
        secret key (hex accepted as well)."""
        if not (secret and event_id and signature):
            return False
        digest = hmac.new(secret.encode(), event_id.encode(), hashlib.sha512).digest()
        signature = signature.strip()
        if signature.lower().startswith('sha512='):
            signature = signature[len('sha512='):]
        return any(hmac.compare_digest(signature, expected) for expected in (
            base64.b64encode(digest).decode(), digest.hex()))

    @api.model
    def _receive(self, account_id, raw, signature):
        """Store a notification. :return: HTTP status for Ingram Micro"""
        account = self.env['m365.ingram.account'].sudo().browse(account_id).exists()
        if not account:
            return 404
        try:
            payload = json.loads(raw or b'{}')
        except ValueError:
            return 400
        if not isinstance(payload, dict):
            return 400
        event_id = str(payload.get('eventId') or '')
        if not self._signature_ok(account.webhook_secret, event_id, signature):
            _logger.warning("Ingram Micro webhook of account %s: missing or wrong signature",
                            account.id)
            return 401
        Event = self.sudo()
        if Event.search_count([('account_id', '=', account.id), ('event_id', '=', event_id)]):
            return 200  # sent again by Ingram Micro: already received
        Event.create({
            'account_id': account.id, 'event_id': event_id,
            'event': payload.get('event') or payload.get('eventType') or False,
            'topic': payload.get('topic') or False,
            'payload': json.dumps(payload),
        })
        cron = self.env.ref('m365_license_management.ir_cron_m365_ingram_events',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return 200

    @staticmethod
    def _resources(payload):
        resources = payload.get('resource') or []
        return [resources] if isinstance(resources, dict) else [
            r for r in resources if isinstance(r, dict)]

    @classmethod
    def _parse_resource(cls, resource):
        """(PO number, Ingram order numbers, error messages, statuses) of an event
        resource: the order create event (``orders``) or a status event (``lines``)."""
        orders = [o for o in resource.get('orders') or [] if isinstance(o, dict)]
        numbers = [resource.get('orderNumber'), resource.get('ingramOrderNumber')]
        numbers += [o.get('ingramOrderNumber') for o in orders]
        lines = [line for line in resource.get('lines') or [] if isinstance(line, dict)]
        for order in orders:
            lines += [line for line in order.get('lines') or [] if isinstance(line, dict)]
        errors, statuses = [], []
        for line in lines:
            status = " ".join(str(line.get(key) or '') for key in (
                'lineStatus', 'status', 'lineStatusCode')).lower().strip()
            if status:
                statuses.append(status)
            if any(word in status for word in ERROR_WORDS):
                errors.append("%s: %s" % (
                    line.get('ingramPartNumber') or line.get('customerLineNumber') or '-',
                    line.get('statusReason') or line.get('message') or line.get('notes')
                    or status))
        if not errors and not any(numbers):
            failed = sum(int(o.get('numberOfLinesWithError') or 0) for o in orders) or int(
                resource.get('numberOfLinesWithError') or 0)
            if failed:
                errors.append(_("%s line(s) refused by Ingram Micro.", failed))
        event_type = str(resource.get('eventType') or '').lower()
        cancelled = [st for st in statuses if any(w in st for w in CANCEL_WORDS)]
        kinds = set()
        if any(w in event_type for w in CANCEL_WORDS) or (
                statuses and len(cancelled) == len(statuses)):
            kinds.add('cancelled')
        elif cancelled:
            kinds.add('partly_cancelled')
        for kind, word in (('hold', 'hold'), ('backorder', 'backorder'), ('shipped', 'ship')):
            if word in event_type or any(word in st for st in statuses):
                kinds.add(kind)
        return (resource.get('customerOrderNumber'), [n for n in numbers if n], errors,
                kinds)

    def _process(self):
        Order = self.env['m365.ingram.order'].sudo()
        for event in self.filtered(lambda e: e.state == 'pending'):
            try:
                with self.env.cr.savepoint():
                    payload = json.loads(event.payload or '{}')
                    orders = Order.browse()
                    for resource in self._resources(payload):
                        po, numbers, errors, kinds = self._parse_resource(resource)
                        order = Order.search([('account_id', '=', event.account_id.id),
                                              ('name', '=', po)], limit=1) if po else Order
                        if order:
                            name = " ".join(filter(None, (event.event,
                                                          resource.get('eventType'))))
                            order._apply_event(name, numbers, errors, kinds)
                            orders |= order
                    event.write({'state': 'done', 'order_ids': [(6, 0, orders.ids)],
                                 'message': False if orders else _("No order of Odoo.")})
            except Exception as exc:  # keep the other events going
                _logger.exception("Ingram Micro event %s failed", event.event_id)
                event.write({'state': 'error', 'message': str(exc)})

    @api.model
    def _cron_process(self):
        self.search([('state', '=', 'pending')], order='id')._process()

    @api.model
    def _cron_cleanup(self):
        """Purge the processed events older than m365.audit_retention_days (365)."""
        days = int(self.env['ir.config_parameter'].sudo().get_param(
            'm365.audit_retention_days', 365))
        if days > 0:
            self.sudo().search([('state', '=', 'done'), (
                'received', '<', fields.Datetime.now() - timedelta(days=days))]).unlink()

    def action_process_again(self):
        self.env['m365.tenant']._check_admin()
        self.filtered(lambda e: e.state == 'error').write({'state': 'pending'})
        self._process()
        return True


class M365TenantIngram(models.Model):
    _inherit = 'm365.tenant'

    ingram_order_ids = fields.One2many('m365.ingram.order', 'tenant_id',
                                       string="License Orders")
    ingram_order_count = fields.Integer(compute='_compute_ingram_order_count')
    ingram_relationship = fields.Boolean(
        string="Ingram Micro Relationship Confirmed", tracking=True, copy=False,
        help="The customer has a reseller relationship with Ingram Micro (indirect "
             "provider), e.g. by accepting its invitation in the Microsoft 365 admin center. "
             "Odoo cannot check it: Partner Center shows it only to a signed-in partner user.")
    ingram_relationship_date = fields.Date(string="Relationship Confirmed On", copy=False)
    mca_accepted = fields.Boolean(string="Customer Agreement Accepted", tracking=True,
                                  copy=False,
                                  help="The customer accepted the Microsoft Customer "
                                       "Agreement, required before buying subscriptions.")
    mca_first_name = fields.Char(string="Accepted By (First Name)", copy=False)
    mca_last_name = fields.Char(string="Accepted By (Last Name)", copy=False)
    mca_email = fields.Char(string="Accepted By (Email)", copy=False)
    mca_phone = fields.Char(string="Accepted By (Phone)", copy=False)
    mca_date = fields.Date(string="Accepted On", copy=False)

    def _mca_recorded(self):
        self.ensure_one()
        return bool(self.mca_accepted and self.mca_date and self.mca_email
                    and (self.mca_first_name or self.mca_last_name))

    @api.onchange('ingram_relationship')
    def _onchange_ingram_relationship(self):
        if self.ingram_relationship and not self.ingram_relationship_date:
            self.ingram_relationship_date = fields.Date.context_today(self)

    @api.depends('ingram_order_ids')
    def _compute_ingram_order_count(self):
        for rec in self:
            rec.ingram_order_count = len(rec.ingram_order_ids)

    def _product_units(self, product):
        """Licenses (total) of a catalogue product in the tenant."""
        self.ensure_one()
        skus = self.env['m365.sku'].sudo().search([('tenant_id', '=', self.id),
                                                    ('product_id', '=', product.id)])
        return sum(skus.filtered(lambda s: s.status != 'Deleted').mapped('enabled_units'))

    def _buy_action(self, product=None):
        """New license order of the tenant, with a plan of the product if one is known."""
        self.ensure_one()
        self._check_admin()
        account = self.env['m365.ingram.account']._get_default()
        if not account:
            raise UserError(_("Add an Ingram Micro account first (Configuration > Ingram "
                              "Micro Accounts)."))
        context = {'default_tenant_id': self.id, 'default_account_id': account.id}
        if product:
            plan = self.env['m365.ingram.plan'].search(
                [('account_id', '=', account.id), ('product_id', '=', product.id)],
                order='product_suggested, id', limit=1)
            if plan:
                context['default_line_ids'] = [(0, 0, {
                    'plan_id': plan.id, 'quantity': max(plan.min_units, 1),
                    'unit_price': plan.unit_price, 'currency_id': plan.currency_id.id})]
        return {
            'type': 'ir.actions.act_window',
            'name': _("Buy Licenses"),
            'res_model': 'm365.ingram.order',
            'view_mode': 'form',
            'target': 'current',
            'context': context,
        }

    def action_buy_licenses(self):
        return self._buy_action()

    def action_view_ingram_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_ingram_order')
        action['domain'] = [('tenant_id', '=', self.id)]
        action['context'] = {'default_tenant_id': self.id}
        return action


class M365SkuIngram(models.Model):
    _inherit = 'm365.sku'

    def action_buy_licenses(self):
        self.ensure_one()
        return self.tenant_id._buy_action(self.product_id)


class M365DashboardIngram(models.AbstractModel):
    _inherit = 'm365.dashboard'

    @api.model
    def _get_alerts(self, tenants, skus):
        """Alerts of products running out lead administrators to Buy Licenses."""
        alerts = super()._get_alerts(tenants, skus)
        if (self.env.user.has_group(ADMIN_GROUP)
                and self.env['m365.ingram.account'].search_count([])):
            for alert in alerts:
                if alert.get('kind') in ('sku_empty', 'sku_low') and alert.get('sku_id'):
                    alert['buy_sku_id'] = alert['sku_id']
        return alerts
