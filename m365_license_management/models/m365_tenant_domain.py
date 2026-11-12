# -*- coding: utf-8 -*-
"""Domains of a tenant (Microsoft Graph ``/domains``).

=========================================  ===============================================
Action                                     Graph endpoint
=========================================  ===============================================
Load the domains                           GET   /domains
Add a custom domain                        POST  /domains
DNS records to verify it                   GET   /domains/{id}/verificationDnsRecords
Verify it (after the DNS change)           POST  /domains/{id}/verify
DNS records of the services (MX, SPF...)   GET   /domains/{id}/serviceConfigurationRecords
Make it the default domain                 PATCH /domains/{id}
=========================================  ===============================================
"""
import logging
import re
import urllib.parse
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import config

from .m365_tenant import M365GraphError

_logger = logging.getLogger(__name__)

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
# Microsoft's value for "passwords never expire"
NEVER_EXPIRES = 2147483647
DOMAIN_RE = re.compile(r'^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')
DOMAIN_STATES = [
    ('to_add', 'To Add'),
    ('pending', 'Waiting for DNS'),
    ('verified', 'Verified'),
    ('action', 'Action Needed'),
    ('failed', 'Failed'),
]
# Automatic verification: first check after FIRST_CHECK, then after each delay of
# RETRY_DELAYS (the last one repeats) until VERIFY_LIMIT after the domain was added
FIRST_CHECK = timedelta(minutes=15)
RETRY_DELAYS = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=4))
VERIFY_LIMIT = timedelta(hours=72)
DOMAIN_BATCH = 50


def is_client_error(exc):
    """Microsoft refused the request itself (HTTP 4xx except 429): retrying is useless."""
    match = re.match(r'HTTP (\d{3})', str(exc))
    return bool(match) and match.group(1).startswith('4') and match.group(1) != '429'


def format_dns_records(env, records):
    """One readable line per DNS record, to give to the customer."""
    lines = []
    for rec in records or []:
        kind = rec.get('recordType') or ''
        if kind == 'Mx':
            value = "%s (priority %s)" % (rec.get('mailExchange'), rec.get('preference'))
        elif kind == 'CName':
            value = rec.get('canonicalName') or ''
        elif kind == 'Srv':
            value = "%s %s %s %s:%s" % (rec.get('service'), rec.get('protocol'),
                                        rec.get('priority'), rec.get('nameTarget'),
                                        rec.get('port'))
        else:
            value = rec.get('text') or ''
        line = "%s  %s  %s  (TTL %s)" % (kind.upper(), rec.get('label') or '@', value,
                                         rec.get('ttl') or '-')
        if rec.get('supportedService'):
            line += "  [%s]" % rec['supportedService']
        if rec.get('isOptional'):
            line += "  " + env._("optional")
        lines.append(line)
    return "\n".join(lines)


class M365TenantDomain(models.Model):
    _name = 'm365.tenant.domain'
    _description = 'Microsoft 365 Tenant Domain'
    _order = 'tenant_id, is_initial desc, name'

    name = fields.Char(string="Domain", required=True, index=True, readonly=True)
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True,
                                ondelete='cascade', index=True, readonly=True)
    is_verified = fields.Boolean(string="Verified", readonly=True)
    is_default = fields.Boolean(string="Default", readonly=True)
    is_initial = fields.Boolean(string="Initial", readonly=True,
                                help="The .onmicrosoft.com domain created with the tenant.")
    authentication_type = fields.Char(string="Authentication", readonly=True)
    supported_services = fields.Char(string="Services", readonly=True)
    password_validity_days = fields.Integer(string="Password Validity (Days)", readonly=True)
    password_never_expires = fields.Boolean(compute='_compute_password_never_expires')
    verification_records = fields.Text(
        string="DNS Records to Verify", readonly=True,
        help="Add these records at the DNS host of the domain, then press Verify.")
    service_records = fields.Text(
        string="DNS Records of the Services", readonly=True,
        help="Records for mail, Teams and device enrolment, once the domain is verified.")
    last_refresh = fields.Datetime(string="Last Refresh", readonly=True)
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)
    state = fields.Selection(DOMAIN_STATES, string="Status", default='pending', readonly=True,
                             index=True,
                             help="To Add: custom domain of the customer, added to Microsoft "
                                  "365 once the tenant is connected. Waiting for DNS: Odoo "
                                  "checks it again (15 minutes, 1 hour, then every 4 hours) "
                                  "for 72 hours.")
    txt_record = fields.Char(string="TXT Record", readonly=True,
                             help="Value of the TXT record proving the ownership (MS=ms...).")
    make_default = fields.Boolean(string="Make Default Once Verified")
    verify_attempts = fields.Integer(string="Checks", readonly=True)
    added_date = fields.Datetime(string="Added On", readonly=True)
    next_check = fields.Datetime(string="Next Check", readonly=True, index=True)
    error = fields.Text(string="Error", readonly=True)

    _tenant_name_uniq = models.Constraint(
        'unique(tenant_id, name)', 'This domain already exists for the tenant.')

    @api.depends('password_validity_days')
    def _compute_password_never_expires(self):
        for rec in self:
            rec.password_never_expires = rec.password_validity_days == NEVER_EXPIRES

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    def _key(self):
        return urllib.parse.quote(self.name, safe='')

    def _refresh_records(self):
        """DNS records to verify the domain, or of its services once verified."""
        self.ensure_one()
        tenant = self.tenant_id
        kind = 'serviceConfigurationRecords' if self.is_verified else 'verificationDnsRecords'
        records = tenant._graph_request('GET', '/domains/%s/%s' % (self._key(), kind),
                                        paginate=True)
        field = 'service_records' if self.is_verified else 'verification_records'
        vals = {field: format_dns_records(self.env, records) or False,
                'last_refresh': fields.Datetime.now()}
        if not self.is_verified:
            txt = [r.get('text') for r in records or []
                   if (r.get('recordType') or '').lower() == 'txt' and r.get('text')]
            vals['txt_record'] = txt[0] if txt else False
        self.sudo().write(vals)

    def action_refresh_records(self):
        self._check_admin()
        for rec in self:
            rec._refresh_records()
        return True

    def action_verify(self):
        """Ask Microsoft to check the DNS records of the domain."""
        self._check_admin()
        self.ensure_one()
        tenant = self.tenant_id
        try:
            item = tenant._graph_request('POST', '/domains/%s/verify' % self._key())
        except M365GraphError as exc:
            # A notification, not an error: the audit log line is kept
            tenant._log('domain', 'failed', _("Verification of %(domain)s failed: %(err)s",
                                              domain=self.name, err=exc))
            return tenant._notify(_(
                "Microsoft could not verify %(domain)s yet: %(err)s Check the DNS records "
                "(changes can take a few hours), then try again.", domain=self.name, err=exc),
                'danger', reload=False)
        self.sudo().write(self._values(item))
        tenant._log('domain', 'success', _("%s verified.", self.name))
        if self.is_verified:
            self._after_verified()
        return tenant._notify(_("%s is verified.", self.name))

    def action_make_default(self):
        self._check_admin()
        self.ensure_one()
        if not self.is_verified:
            raise UserError(_("Verify %s before making it the default domain.", self.name))
        tenant = self.tenant_id
        tenant._graph_request('PATCH', '/domains/%s' % self._key(),
                              json_body={'isDefault': True})
        tenant._log('domain', 'success', _("%s is now the default domain.", self.name))
        tenant._sync_domains()
        return tenant._notify(_("%s is now the default domain.", self.name))

    # ------------------------------------------------------------------
    # Automatic add and verification
    # ------------------------------------------------------------------
    def _after_added(self, item):
        """The domain exists in Microsoft 365: wait for its DNS record."""
        self.ensure_one()
        now = fields.Datetime.now()
        self.sudo().write(dict(self._values(item), state='pending', added_date=now,
                               verify_attempts=0, next_check=now + FIRST_CHECK, error=False))
        if self.is_verified:
            self._after_verified()
            return
        try:
            self._refresh_records()
        except M365GraphError as exc:
            _logger.info("DNS records of %s not read yet: %s", self.name, exc)

    def _after_verified(self):
        """Default domain (when asked) and records of the services."""
        self.ensure_one()
        self.sudo().write({'state': 'verified', 'next_check': False, 'error': False})
        if self.make_default and not self.is_default:
            try:
                self.tenant_id._graph_request('PATCH', '/domains/%s' % self._key(),
                                              json_body={'isDefault': True})
                self.tenant_id.domain_ids.filtered('is_default').sudo().is_default = False
                self.sudo().is_default = True
                self.tenant_id._log('domain', 'success',
                                    _("%s is now the default domain.", self.name))
            except M365GraphError as exc:
                self.sudo().error = _("Not made the default domain: %s", exc)
                self.tenant_id._log('domain', 'failed', self.error)
        try:
            self._refresh_records()
        except M365GraphError:
            pass  # the domain is verified; the service records can be loaded later

    def _auto_step(self):
        """Add or verify the domain once. Failures are stored, never raised.

        :return: the new state
        """
        self.ensure_one()
        # Locked for this transaction: two scheduled actions never add the same domain
        self.env.cr.execute("SELECT id FROM m365_tenant_domain WHERE id = %s "
                            "FOR UPDATE SKIP LOCKED", (self.id,))
        if not self.env.cr.fetchone():
            return self.state
        self.invalidate_recordset(['state', 'next_check'])
        tenant = self.tenant_id
        now = fields.Datetime.now()
        if self.state == 'to_add':
            try:
                item = tenant._graph_request('POST', '/domains', json_body={'id': self.name})
            except M365GraphError as exc:
                if is_client_error(exc):
                    # e.g. the domain is in another tenant: a person must sort it out
                    self.sudo().write({'state': 'failed', 'error': str(exc),
                                       'next_check': False})
                    tenant._log('domain', 'failed', _("%(domain)s not added: %(err)s",
                                                      domain=self.name, err=exc))
                else:
                    self.sudo().write({'error': str(exc), 'next_check': now + FIRST_CHECK})
                return self.state
            tenant._log('domain', 'success', _("%s added; to verify.", self.name))
            self._after_added(item)
            return self.state
        if self.state != 'pending':
            return self.state
        if not self.txt_record:
            try:
                self._refresh_records()
            except M365GraphError:
                pass
        try:
            item = tenant._graph_request('POST', '/domains/%s/verify' % self._key())
        except M365GraphError as exc:
            attempts = self.verify_attempts + 1
            vals = {'verify_attempts': attempts, 'error': str(exc)}
            if now - (self.added_date or now) >= VERIFY_LIMIT:
                vals.update(state='action', next_check=False)
                tenant._log('domain', 'failed', _(
                    "%(domain)s still not verified after %(hours)s hours: %(err)s",
                    domain=self.name, hours=int(VERIFY_LIMIT.total_seconds() // 3600),
                    err=exc))
                tenant._schedule_prov_activity(
                    _("DNS of %s not verified", self.name),
                    _("Ask the customer to add the TXT record %(txt)s to %(domain)s, then "
                      "press Verify on the domain.", txt=self.txt_record or '-',
                      domain=self.name))
            else:
                vals['next_check'] = now + RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1]
            self.sudo().write(vals)
            return self.state
        self.sudo().write(self._values(item))
        if not self.is_verified:
            self.sudo().next_check = now + RETRY_DELAYS[0]
            return self.state
        tenant._log('domain', 'success', _("%s verified.", self.name))
        self._after_verified()
        return self.state

    @api.model
    def _cron_auto_domains(self):
        """Add the custom domains waiting for it and verify the pending ones."""
        now = fields.Datetime.now()
        domains = self.sudo().search([
            ('state', 'in', ('to_add', 'pending')),
            '|', ('next_check', '=', False), ('next_check', '<=', now),
            ('tenant_id.state', 'in', ('connected', 'error')),
            ('tenant_id.active', '=', True),
            '|', ('state', '=', 'to_add'), ('added_date', '!=', False),
        ], order='next_check, id', limit=DOMAIN_BATCH)
        for domain in domains:
            try:
                domain._auto_step()
            except Exception:  # unexpected (DB, bug): keep the other domains running
                self.env.cr.rollback()
                _logger.exception("Automatic verification of domain %s failed", domain.id)
            if not config['test_enable']:
                self.env.cr.commit()

    def action_retry(self):
        """Try again a domain that failed or waited too long."""
        self._check_admin()
        now = fields.Datetime.now()
        for rec in self.filtered(lambda d: d.state in ('failed', 'action')):
            rec.sudo().write({'state': 'to_add' if rec.state == 'failed' else 'pending',
                              'added_date': now if rec.state == 'action' else rec.added_date,
                              'verify_attempts': 0, 'next_check': now, 'error': False})
            rec._auto_step()
        return True

    @staticmethod
    def _values(item):
        return {
            'is_verified': bool(item.get('isVerified')),
            'is_default': bool(item.get('isDefault')),
            'is_initial': bool(item.get('isInitial')),
            'authentication_type': item.get('authenticationType') or False,
            'supported_services': ", ".join(item.get('supportedServices') or []) or False,
            'password_validity_days': item.get('passwordValidityPeriodInDays') or 0,
            'last_refresh': fields.Datetime.now(),
        }


class M365TenantDomainWizard(models.TransientModel):
    _name = 'm365.tenant.domain.wizard'
    _description = 'Add a Domain to a Microsoft 365 Tenant'

    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True, readonly=True)
    name = fields.Char(string="Domain", required=True, help="e.g. contoso.com")

    def action_add(self):
        self.ensure_one()
        try:
            domain = self._add()
        except M365GraphError as exc:
            # A notification, not an error: the audit log line is kept
            return self.tenant_id._notify(str(exc), 'danger', reload=False)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'm365.tenant.domain',
            'res_id': domain.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _add(self):
        """Add the domain in Microsoft 365. :return: the domain record"""
        self.ensure_one()
        self.env['m365.tenant.domain']._check_admin()
        name = (self.name or '').strip().lower()
        if not DOMAIN_RE.match(name):
            raise UserError(_("%s is not a valid domain name.", self.name))
        if name.endswith('.onmicrosoft.com'):
            raise UserError(_("Microsoft creates the .onmicrosoft.com domains itself."))
        tenant = self.tenant_id
        try:
            item = tenant._graph_request('POST', '/domains', json_body={'id': name})
        except M365GraphError as exc:
            message = _("%(domain)s not added: %(err)s", domain=name, err=exc)
            tenant._log('domain', 'failed', message)
            raise M365GraphError(message) from None
        Domain = self.env['m365.tenant.domain'].sudo()
        domain = Domain.search([('tenant_id', '=', tenant.id), ('name', '=', name)])
        if not domain:
            domain = Domain.create({'tenant_id': tenant.id, 'name': name, 'state': 'pending'})
        tenant._log('domain', 'success', _("%s added; to verify.", name))
        domain._after_added(item)
        return domain
