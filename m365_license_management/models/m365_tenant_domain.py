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
import re
import urllib.parse

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
# Microsoft's value for "passwords never expire"
NEVER_EXPIRES = 2147483647
DOMAIN_RE = re.compile(r'^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$')


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
        self.sudo().write({field: format_dns_records(self.env, records) or False,
                           'last_refresh': fields.Datetime.now()})

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
            try:
                self._refresh_records()
            except M365GraphError:
                pass  # the domain is verified; the service records can be loaded later
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
        vals = Domain._values(item)
        if domain:
            domain.write(vals)
        else:
            domain = Domain.create(dict(vals, tenant_id=tenant.id, name=name))
        tenant._log('domain', 'success', _("%s added; to verify.", name))
        try:
            domain._refresh_records()
        except M365GraphError:
            pass  # shown on the domain: Refresh DNS Records
        return domain
