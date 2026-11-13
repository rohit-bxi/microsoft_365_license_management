# -*- coding: utf-8 -*-
"""New tenants created by Ingram Micro (19.0.1.10.0).

Ingram Micro has no API that only creates a customer: a new Microsoft tenant is created
with the first order of the customer, whose lines carry the vendor required information
(``vriAdditionalAttributes``) ``tenant_preference = new`` and the data of the tenant::

    POST /resellers/v7/vendorrequiredinfo   the fields Microsoft requires for the plans
    POST /resellers/v7/orders               order + new tenant (result by webhook)

A tenant of type New (the creation through Partner Center is disabled)::

    To Check --Check--> Ready --Create in Microsoft 365--> Creating --> Created
                                  (order of the Subscriptions     |
                                   to Buy, sent once approved)    +--order refused,
                                                                     cancelled--> Failed

* Check: the data of the tenant, the Customer Agreement recorded, the Subscriptions to
  Buy found in the Ingram Micro catalog, the .onmicrosoft.com domain free.
* Creating: the order waits for its approval, then for Ingram Micro. The tenant is
  Created once its domain exists in Microsoft 365: the OpenID configuration of the
  domain (public, no sign-in) gives its Directory ID. Checked after the confirmation of
  Ingram Micro, every hour and with Next Step / Check Creation.
* The tenant fields are sent flat in ``additionalAttributes``: the attributes of the
  ``new`` choice of ``tenant_preference`` given by the vendor required info, and its
  other attributes (e.g. ``tier1_mpn``) with the value Ingram Micro returns. A Vendor
  Field of the account with the same name overrides a value. Without vendor required
  info, the fields known to Odoo are sent.
* Ingram Micro is the indirect provider of the tenant it creates: the relationship is
  recorded. Ingram Micro gives the initial admin user, not Odoo.
"""
import logging
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .m365_ingram_account import M365IngramError
from .m365_tenant import M365GraphError

_logger = logging.getLogger(__name__)

DOMAIN_PREFIX_MAX = 27  # microsoft_domain: "Up to 27 characters long"
GUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
PERIOD_RE = re.compile(r'^\s*(\d+)\s*([a-z]+)', re.I)
CHECK_AFTER = timedelta(minutes=5)  # first look for the tenant after Ingram's answer
# Order statuses: the tenant is being created / Ingram Micro accepted the order
OPEN_STATES = ('submitted', 'sending', 'sent', 'confirmed', 'delivered')
ACCEPTED_STATES = ('sent', 'confirmed', 'delivered')
# Choice of the new tenant in the vendor required info
TENANT_PREFERENCE = 'tenant_preference'


class M365IngramAccountTenant(models.Model):
    _inherit = 'm365.ingram.account'

    def _vendor_required_info(self, plans):
        """Fields Microsoft requires to order the plans.

        :return: {plan ID: entry of POST /resellers/v7/vendorrequiredinfo}
        """
        self.ensure_one()
        if not plans:
            return {}
        _status, data = self._im_request(
            'POST', '/resellers/v7/vendorrequiredinfo',
            json_body={'products': [{'planID': plan.plan_id} for plan in plans]})
        entries = data if isinstance(data, list) else [data]
        return {str(entry['planId']): entry for entry in entries
                if isinstance(entry, dict) and entry.get('planId')}


class M365IngramOrderTenant(models.Model):
    _inherit = 'm365.ingram.order'

    create_tenant = fields.Boolean(
        string="Creates the Tenant", readonly=True, copy=False,
        help="Ingram Micro creates the Microsoft tenant with this order (tenant_preference "
             "= new).")

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals:
            self.filtered('create_tenant')._sync_tenant_creation()
        return res

    def _validate_tenant_creation(self):
        self.ensure_one()
        tenant = self.tenant_id
        if tenant.tenant_type != 'new':
            raise UserError(_("Only a new tenant is created by its order."))
        if tenant.provision_state == 'done':
            raise UserError(_("Tenant %s is already created.", tenant.name))
        if tenant.provision_state == 'draft':
            raise UserError(_("Check tenant %s first.", tenant.name))
        if not tenant._mca_recorded():
            raise UserError(_(
                "Record first who accepted the Microsoft Customer Agreement for tenant %s, "
                "and when: Microsoft requires it to create the tenant.", tenant.name))
        other = tenant.sudo().ingram_order_ids.filtered(
            lambda o: o.create_tenant and o.id != self.id
            and o.state not in ('cancelled', 'rejected'))
        if other:
            raise UserError(_("Order %(order)s already creates tenant %(tenant)s.",
                              order=other[0].name, tenant=tenant.name))

    def _sync_tenant_creation(self):
        """Creation status of the tenant, from the status of the order creating it."""
        for order in self:
            tenant = order.tenant_id.sudo()
            if tenant.tenant_type != 'new' or tenant.provision_state in (False, 'done'):
                continue
            if order.state in OPEN_STATES:
                vals = {'provision_state': 'queued', 'provision_error': False}
            elif order.state == 'failed':
                vals = {'provision_state': 'failed', 'provision_error': _(
                    "Order %(order)s failed: %(error)s", order=order.name,
                    error=order.error_message or '-')}
            elif order.state in ('cancelled', 'rejected'):
                vals = {'provision_state': 'failed', 'provision_error': _(
                    "Order %(order)s was %(status)s. Retry orders the Subscriptions to Buy "
                    "again.", order=order.name,
                    status=_("cancelled") if order.state == 'cancelled' else _("rejected"))}
            else:
                continue
            if any(tenant[key] != value for key, value in vals.items()):
                tenant.write(vals)
            if order.state in ACCEPTED_STATES:
                tenant._trigger_creation_check()

    def _creation_required_info(self):
        """Vendor required info of the plans; when Ingram Micro does not give it, the
        fields known to Odoo are sent."""
        self.ensure_one()
        try:
            return self.account_id._vendor_required_info(self.line_ids.plan_id)
        except M365IngramError as exc:
            _logger.info("Vendor required info of order %s not read: %s", self.name, exc)
            self.message_post(body=_("Ingram Micro gave no vendor required info (%s): the "
                                     "tenant fields known to Odoo are sent.", exc))
            return {}

    def _tenant_creation_values(self):
        """Value of the new tenant fields, by attribute name."""
        tenant = self.tenant_id
        return {
            TENANT_PREFERENCE: 'new',
            'microsoft_domain': (tenant.domain or '').split('.')[0],
            'effective_address': tenant.address_line1 or '',
            'effective_city': tenant.city or '',
            'effective_state': tenant.address_state or '',
            'effective_postal_code': tenant.postal_code or '',
            'effective_country': tenant.country_id.code or '',
            'effective_phonenumber': tenant.contact_phone or '',
            'mca_acceptance': 'yes',
            'first_name_agreement': tenant.mca_first_name or '',
            'last_name_agreement': tenant.mca_last_name or '',
            'email_address_agreement': tenant.mca_email or '',
            'agreement_date': tenant.mca_date.strftime('%m-%d-%Y') if tenant.mca_date else '',
            'partner_on_record_attestation_accepted': 'attestation_accepted',
            'special_qualifications': 'None',
            'address_approved_by_user': 'approved',
        }

    @staticmethod
    def _required_attributes(group):
        """(name, value given by Ingram Micro) of a group of the vendor required info; for
        ``tenant_preference``, the attributes of its ``new`` choice."""
        for attr in group.get('additionalAttributes') or []:
            name = attr.get('attributeName') if isinstance(attr, dict) else None
            if not name:
                continue
            if name != TENANT_PREFERENCE:
                yield name, attr.get('attributeValue')
                continue
            yield name, 'new'
            choice = next((c for c in attr.get('choices') or [] if isinstance(c, dict)
                           and c.get('attributeValue') == 'new'), {})
            for sub in choice.get('choices') or []:
                sub_name = isinstance(sub, dict) and (sub.get('attributeName')
                                                      or sub.get('attributename'))
                if sub_name:
                    yield sub_name, sub.get('attributeValue')

    def _tenant_creation_attributes(self, line, required):
        """``vriAdditionalAttributes`` of a line creating the tenant.

        :param required: entry of the vendor required info of the plan, or None
        :raise M365IngramError: a field Ingram Micro requires has no value
        """
        values = self._tenant_creation_values()
        overrides = {attr.name: attr._payload(self, line)['attributeValue']
                     for attr in self.account_id.vmf_attribute_ids}
        groups, missing = [], []
        for group in (required or {}).get('vriAdditionalAttributes') or []:
            if not isinstance(group, dict):
                continue
            attributes = []
            for name, given in self._required_attributes(group):
                if name in overrides:
                    value = overrides[name]
                elif name in values:
                    value = values[name]
                else:  # e.g. tier1_mpn; a list of choices ("yes,no") is not a value
                    value = '' if ',' in str(given or '') else str(given or '')
                if not value:
                    missing.append(name)
                attributes.append({'attributeName': name, 'attributeValue': value})
            groups.append(dict({key: group[key] for key in ('vendorName', 'productId')
                                if group.get(key)}, additionalAttributes=attributes))
        if not any(attr['attributeName'] == TENANT_PREFERENCE
                   for group in groups for attr in group['additionalAttributes']):
            # No vendor required info for the plan: the fields known to Odoo
            if not groups:
                groups.append({'vendorName': self.account_id.vendor_name,
                               'additionalAttributes': []})
            known = {attr['attributeName'] for attr in groups[0]['additionalAttributes']}
            groups[0]['additionalAttributes'] += [
                {'attributeName': name, 'attributeValue': overrides.get(name, value)}
                for name, value in values.items()
                if name not in known and overrides.get(name, value)]
        if missing:
            raise M365IngramError(_(
                "Ingram Micro requires %s to create the tenant: complete the tenant, or add "
                "the field as a Vendor Field of the Ingram Micro account.",
                ", ".join(sorted(set(missing)))))
        return groups

    @staticmethod
    def _subscription_period(term):
        """``subscriptionPeriod`` of a line: the period of the catalog, e.g. 1 YEARS."""
        if not term:
            return False
        match = PERIOD_RE.match(term.code or '')
        if match:
            return {'type': match.group(2).upper(), 'duration': int(match.group(1))}
        if term.months:
            return ({'type': 'YEARS', 'duration': term.months // 12} if term.months % 12 == 0
                    else {'type': 'MONTHS', 'duration': term.months})
        return False

    def _tenant_creation_line(self, line, required):
        """Values of a line of the order creating the tenant."""
        vals = {'planid': line.plan_id.plan_id,
                'vriAdditionalAttributes': self._tenant_creation_attributes(line, required)}
        period = self._subscription_period(line.term_id)
        if period:
            vals['subscriptionPeriod'] = period
        return vals


class M365TenantIngramCreation(models.Model):
    _inherit = 'm365.tenant'

    ingram_creation_order_id = fields.Many2one(
        'm365.ingram.order', string="Creation Order", compute='_compute_ingram_creation_order',
        help="Ingram Micro order that creates the tenant.")

    @api.depends('ingram_order_ids.state', 'ingram_order_ids.create_tenant')
    def _compute_ingram_creation_order(self):
        for rec in self:
            rec.ingram_creation_order_id = rec._ingram_creation_order()

    def _ingram_creation_order(self):
        """Order creating the tenant: the latest one not cancelled nor rejected."""
        self.ensure_one()
        return self.ingram_order_ids.filtered(
            lambda o: o.create_tenant and o.state not in ('cancelled', 'rejected'))[:1]

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------
    def _check_values(self):
        """Errors in the data (also when a form or a file row is saved)."""
        errors = super()._check_values()
        prefix = (self.domain or '').split('.')[0]
        if len(prefix) > DOMAIN_PREFIX_MAX:
            errors.append(_("Ingram Micro accepts %(max)s characters at most before "
                            ".onmicrosoft.com: %(prefix)s has %(count)s.",
                            max=DOMAIN_PREFIX_MAX, prefix=prefix, count=len(prefix)))
        return errors

    def _ingram_creation_errors(self):
        """What Ingram Micro needs to create the tenant (Check button)."""
        self.ensure_one()
        errors = []
        if not self.env['m365.ingram.account']._get_default():
            errors.append(_("Add an Ingram Micro account first."))
            return errors
        if not self.address_state and self.country_id.state_ids:
            errors.append(_("%s is required: Ingram Micro sends it to Microsoft.",
                            self._fields['address_state'].string))
        if not self._mca_recorded():
            errors.append(_("Record who accepted the Microsoft Customer Agreement, and when: "
                            "Ingram Micro sends it to Microsoft to create the tenant."))
        if not self._ingram_creation_order():
            if not (self.requested_subscriptions or '').strip():
                errors.append(_("Enter the Subscriptions to Buy: Ingram Micro creates the "
                                "tenant with the first order."))
            else:
                errors += self._parse_requested_ingram()[1]
        return errors

    def _check_with_microsoft(self):
        """What Ingram Micro needs, and the domain free (the address is checked by Ingram
        Micro and Microsoft with the order). :return: errors"""
        self.ensure_one()
        return self._ingram_creation_errors() or self._check_domain()

    def _domain_available(self, domain):
        """The .onmicrosoft.com domain has no tenant in Microsoft 365."""
        self.ensure_one()
        tenant_guid = self._lookup_tenant_id(domain)
        if tenant_guid is None:
            raise M365GraphError(_("Microsoft 365 did not tell whether %s is free: check "
                                   "again later.", domain))
        return not tenant_guid

    def _lookup_tenant_id(self, domain):
        """Directory ID of the tenant owning a domain, from the public OpenID configuration
        of the domain.

        :return: the ID; False when no tenant has the domain; None when Microsoft gave no
            answer
        """
        self.ensure_one()
        authority = (self.authority_url or 'https://login.microsoftonline.com').rstrip('/')
        url = "%s/%s/v2.0/.well-known/openid-configuration" % (
            authority, urllib.parse.quote(domain or '', safe=''))
        try:
            resp = requests.get(url, timeout=20)
        except requests.RequestException as exc:
            _logger.info("Tenant of %s not looked up: %s", domain, exc)
            return None
        if resp.status_code in (400, 404):  # AADSTS90002: tenant not found
            return False
        if resp.status_code != 200:
            return None
        try:
            issuer = (resp.json() or {}).get('issuer') or ''
        except ValueError:
            return None
        match = GUID_RE.search(issuer)
        return match.group(0).lower() if match else None

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def action_create(self):
        """Order the Subscriptions to Buy of the checked (or failed) new tenants from
        Ingram Micro, approved by the administrator: Ingram Micro creates the tenants."""
        self._check_admin()
        todo = self.filtered(lambda t: t.tenant_type == 'new'
                             and t.provision_state in ('ready', 'failed'))
        if not todo:
            raise UserError(_("Check the tenants first: only checked or failed new tenants "
                              "can be created."))
        messages, errors = [], []
        for rec in todo:
            try:
                rec._ingram_start_creation(approve=True)
            except UserError as exc:  # M365GraphError included
                errors.append("%s: %s" % (rec.name, exc))
                continue
            if rec.provision_state != 'queued':
                errors.append("%s: %s" % (rec.name, rec.provision_error or '-'))
        sent = todo.filtered(lambda t: t.provision_state == 'queued')
        if sent:
            messages.append(_(
                "%s tenant(s) ordered from Ingram Micro, which creates them. Odoo sees each "
                "tenant once its domain exists in Microsoft 365 (checked every hour).",
                len(sent)))
        if errors:
            messages.append(_("Not ordered: %s", "; ".join(errors)))
        return self._notify(" ".join(messages), 'warning' if errors else 'success')

    def _ingram_start_creation(self, approve):
        """Order of the Subscriptions to Buy that creates the tenant, submitted; sent when
        ``approve``. An order that failed is sent again (looked for at Ingram Micro first
        when it may exist there). :return: the order"""
        self.ensure_one()
        order = self._ingram_creation_order()
        if not order:
            order = self._order_requested_ingram(create_tenant=True)
        elif order.state == 'draft':
            order.action_submit()
        self.sudo().queued_by = self.env.uid
        if approve and order.state == 'submitted':
            order.action_approve()
        elif approve and order.state in ('failed', 'sending'):
            order.action_retry()
        order._sync_tenant_creation()
        return order

    def _trigger_creation_check(self):
        cron = self.env.ref('m365_license_management.ir_cron_m365_create_tenants',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(at=datetime.now(timezone.utc).replace(tzinfo=None)
                                 + CHECK_AFTER)

    def _ingram_check_created(self):
        """Tenants whose creation order Ingram Micro accepted: Created once their domain
        exists in Microsoft 365. :return: the tenants created"""
        created = self.browse()
        for rec in self.filtered(lambda t: t.provision_state == 'queued'):
            order = rec.sudo()._ingram_creation_order()
            if order.state not in ACCEPTED_STATES:
                continue
            tenant_guid = rec._lookup_tenant_id(rec.domain)
            if tenant_guid and rec._ingram_creation_done(tenant_guid, order):
                created |= rec
            rec._commit()
        return created

    def _ingram_creation_done(self, tenant_guid, order):
        self.ensure_one()
        sudo = self.sudo()
        other = sudo.with_context(active_test=False).search(
            [('tenant_id', '=ilike', tenant_guid), ('id', '!=', self.id)], limit=1)
        if other:
            sudo.write({'provision_state': 'failed', 'provision_error': _(
                "%(domain)s belongs to the tenant %(id)s, already in Odoo as %(name)s.",
                domain=self.domain, id=tenant_guid, name=other.name)})
            return False
        sudo.write({
            'provision_state': 'done',
            'provision_error': False,
            'tenant_id': tenant_guid,
            'created_date': fields.Datetime.now(),
            'ingram_relationship': True,
            'ingram_relationship_date': fields.Date.context_today(self),
        })
        sudo.message_post(body=_("Tenant created in Microsoft 365 by Ingram Micro (order "
                                 "%(order)s). Directory (tenant) ID: %(id)s. Ingram Micro "
                                 "gives the initial admin user.", order=order.name,
                                 id=tenant_guid))
        self._log('provision_tenant', 'success',
                  _("%(name)s (%(domain)s) created by Ingram Micro, order %(order)s.",
                    name=self.name, domain=self.domain, order=order.name),
                  user=sudo.queued_by or None)
        return True

    @api.model
    def _cron_provision_tenants(self):
        """Look for the tenants ordered from Ingram Micro in Microsoft 365."""
        self.search([('tenant_type', '=', 'new'), ('provision_state', '=', 'queued')],
                    order='id')._ingram_check_created()

    def action_check_ingram_creation(self):
        """Look for the tenants ordered from Ingram Micro in Microsoft 365."""
        self._check_admin()
        queued = self.filtered(lambda t: t.provision_state == 'queued')
        created = queued._ingram_check_created()
        if created:
            return self._notify(_("%s tenant(s) created in Microsoft 365.", len(created)))
        waiting = queued.filtered(
            lambda t: t.sudo()._ingram_creation_order().state == 'submitted')
        if waiting:
            return self._notify(_("Approve first the order(s) %s, which create the tenant.",
                                  ", ".join(waiting.sudo().mapped(
                                      lambda t: t._ingram_creation_order().name))), 'warning')
        return self._notify(_("Ingram Micro has not created the tenant yet: checked again "
                              "every hour."), 'info')

    # ------------------------------------------------------------------
    # Provisioning step
    # ------------------------------------------------------------------
    def _prov_order_failed(self, order, interactive):
        """Result of the step when the order creating the tenant failed."""
        if order.send_attempted and not interactive:
            raise M365GraphError(order.error_message or _("Order failed."))  # retried later
        return 'action', _("Order %(order)s failed: %(error)s", order=order.name,
                           error=order.error_message or '-')

    def _prov_run_tenant(self, interactive):
        if self.provision_state == 'draft':
            self.action_check()
            if self.provision_state != 'ready':
                return 'action', _("Check failed: %s", self.provision_error or '-')
        order = self._ingram_creation_order()
        if order.state == 'failed':
            if not (interactive or order.send_attempted):
                return 'action', _("Ingram Micro refused the order %(order)s: %(error)s",
                                   order=order.name, error=order.error_message or '-')
            order.action_retry()  # looks for it at Ingram Micro first when it may exist
            if order.state == 'failed':
                return self._prov_order_failed(order, interactive)
        elif not order and self.provision_state == 'failed' and not interactive:
            # The order was cancelled or rejected: ordering again is for a person
            return 'action', self.provision_error or _("Order cancelled.")
        elif not order or order.state == 'draft':
            order = self._ingram_start_creation(approve=False)
        if order.state == 'submitted':
            if not self.prov_approve_orders:
                return 'action', _("Approval of the order %s, which creates the tenant",
                                   order.name)
            order._log('info', _("approved automatically (option of the customer file "
                                 "imported by %s).", self.env.user.name))
            order.action_approve()
            if order.state == 'failed':
                return self._prov_order_failed(order, interactive)
        if self.provision_state == 'done' or self._ingram_check_created():
            return 'progress', _("Tenant created by Ingram Micro.")
        return 'waiting', _("Ingram Micro to create the tenant (order %s)", order.name)
