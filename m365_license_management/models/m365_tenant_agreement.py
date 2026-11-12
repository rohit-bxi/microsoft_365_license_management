# -*- coding: utf-8 -*-
"""Microsoft Customer Agreement accepted for a customer through Partner Center
(App+User: the partner user signed in on the Partner Center account).

=========================================  ===============================================
Step                                       Partner Center call (App+User)
=========================================  ===============================================
Agreement template                         GET  /v1/agreements?agreementType=MicrosoftCustomerAgreement
Already accepted?                          GET  /v1/customers/{id}/agreements?agreementType=...
Attest the acceptance of the customer      POST /v1/customers/{id}/agreements
=========================================  ===============================================

The partner attests that the customer accepted the agreement: Odoo sends it only for
tenants where an administrator confirmed it (*Customer Accepted the Agreement*, or the
option of the customer file). The acceptance is then recorded on the tenant
(*Purchasing* tab), as when it is entered by hand.

Reference: https://learn.microsoft.com/en-us/partner-center/developer/confirm-customer-consent-customer-agreement
"""
import urllib.parse

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .m365_tenant import M365GraphError

MCA_TYPE = 'MicrosoftCustomerAgreement'
MCA_LINK = 'https://aka.ms/customeragreement'


class M365PartnerAccountAgreement(models.Model):
    _inherit = 'm365.partner.account'

    mca_template_id = fields.Char(string="Customer Agreement Template", readonly=True,
                                  copy=False)

    def _mca_template(self):
        """Template ID of the current Microsoft Customer Agreement (cached)."""
        self.ensure_one()
        if self.mca_template_id:
            return self.mca_template_id
        _status, data = self._pc_user_request(
            'GET', '/v1/agreements?agreementType=%s' % MCA_TYPE)
        items = [i for i in data.get('items') or [] if isinstance(i, dict)
                 and i.get('templateId')]
        if not items:
            raise M365GraphError(_("Partner Center returned no Microsoft Customer Agreement "
                                   "template."))
        self.sudo().mca_template_id = items[0]['templateId']
        return self.mca_template_id


class M365TenantAgreement(models.Model):
    _inherit = 'm365.tenant'

    mca_customer_confirmed = fields.Boolean(
        string="Customer Accepted the Agreement", copy=False, tracking=True,
        help="You confirm that the customer accepted the Microsoft Customer Agreement: Odoo "
             "then attests it in Partner Center with the contact below.")
    mca_attested = fields.Boolean(string="Sent to Partner Center", readonly=True, copy=False)
    mca_attest_date = fields.Datetime(string="Sent On", readonly=True, copy=False)
    mca_error = fields.Char(string="Agreement Error", readonly=True, copy=False)

    def _pc_customer_key(self):
        """Partner Center customer ID (URL encoded): given at creation, else the
        directory ID."""
        self.ensure_one()
        rec = self.sudo()
        customer = (rec.customer_id or rec.tenant_id or '').strip()
        if not customer:
            raise UserError(_("The Partner Center customer ID of %s is not known.", rec.name))
        return urllib.parse.quote(customer, safe='')

    def _mca_contact(self):
        """(first name, last name, email, phone) of the person who accepted: the one
        recorded, else the contact of the tenant."""
        self.ensure_one()
        if self.mca_email:
            return (self.mca_first_name or '', self.mca_last_name or '', self.mca_email,
                    self.mca_phone or '')
        return (self.contact_first_name or '', self.contact_last_name or '',
                self.contact_email or '', self.contact_phone or '')

    def _attest_mca(self):
        """Record or send the acceptance of the agreement. :return: True when it was
        already accepted in Partner Center"""
        self.ensure_one()
        rec = self.sudo()
        account = rec.partner_account_id or rec._default_partner_account()
        if not account:
            raise UserError(_("Add a Partner Center account first."))
        customer = rec._pc_customer_key()
        _status, data = account._pc_user_request(
            'GET', '/v1/customers/%s/agreements?agreementType=%s' % (customer, MCA_TYPE))
        items = sorted([i for i in data.get('items') or [] if isinstance(i, dict)],
                       key=lambda i: i.get('dateAgreed') or '', reverse=True)
        if items:
            contact = items[0].get('primaryContact') or {}
            rec.write({
                'mca_accepted': True, 'mca_attested': True, 'mca_error': False,
                'mca_first_name': contact.get('firstName') or rec.mca_first_name,
                'mca_last_name': contact.get('lastName') or rec.mca_last_name,
                'mca_email': contact.get('email') or rec.mca_email,
                'mca_phone': contact.get('phoneNumber') or rec.mca_phone,
                'mca_date': fields.Date.to_date((items[0].get('dateAgreed') or '')[:10]
                                                or fields.Date.today()),
            })
            rec._log('agreement', 'info', _("The Microsoft Customer Agreement is already "
                                            "accepted in Partner Center."))
            return True
        if not rec.mca_customer_confirmed:
            raise UserError(_("Confirm first that the customer %s accepted the Microsoft "
                              "Customer Agreement (Customer Accepted the Agreement).",
                              rec.name))
        first, last, email, phone = rec._mca_contact()
        if not (email and (first or last)):
            raise UserError(_("Enter the name and the email of the person who accepted the "
                              "agreement for %s.", rec.name))
        date = rec.mca_date or fields.Date.context_today(rec)
        contact = {'firstName': first, 'lastName': last, 'email': email}
        if phone:
            contact['phoneNumber'] = phone
        account._pc_user_request('POST', '/v1/customers/%s/agreements' % customer, json_body={
            'primaryContact': contact,
            'templateId': account._mca_template(),
            'dateAgreed': '%sT00:00:00Z' % date.isoformat(),
            'type': MCA_TYPE,
            'agreementLink': MCA_LINK,
        })
        rec.write({'mca_accepted': True, 'mca_attested': True, 'mca_error': False,
                   'mca_attest_date': fields.Datetime.now(), 'mca_first_name': first,
                   'mca_last_name': last, 'mca_email': email, 'mca_phone': phone or False,
                   'mca_date': date})
        rec._log('agreement', 'success', _(
            "Microsoft Customer Agreement accepted by %(name)s (%(email)s) on %(date)s, "
            "attested in Partner Center.", name=" ".join(filter(None, (first, last))),
            email=email, date=date))
        return False

    def action_attest_mca(self):
        self._check_admin()
        errors = []
        for tenant in self:
            try:
                tenant._attest_mca()
            except UserError as exc:  # M365GraphError included
                tenant.sudo().mca_error = str(exc)[:250]
                tenant._log('agreement', 'failed', str(exc))
                errors.append("%s: %s" % (tenant.name, exc))
        if errors:
            return self._notify("\n".join(errors), 'danger')
        return self._notify(_("Microsoft Customer Agreement recorded."))

    @api.onchange('mca_customer_confirmed')
    def _onchange_mca_customer_confirmed(self):
        if self.mca_customer_confirmed and not self.mca_date:
            self.mca_date = fields.Date.context_today(self)
