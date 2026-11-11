# -*- coding: utf-8 -*-
"""Configuration of a tenant from its configuration profile, and its domains.

=========================================  ===============================================
Setting of the profile                     Graph call
=========================================  ===============================================
Security Defaults: keep / on / off         GET / PATCH /policies/identitySecurityDefaultsEnforcementPolicy
Passwords never expire                     PATCH /domains/{id} passwordValidityPeriodInDays
Technical notification email               PATCH /organization/{id} technicalNotificationMails
Default usage location                     none: used for the users created from Odoo
=========================================  ===============================================

*Apply Configuration* reads every current value first and only writes what differs, so
it can run again safely; each change is written to the audit log and the first failure
stops the run. It runs automatically once a tenant is connected through the shared app.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .m365_tenant import M365GraphError
from .m365_tenant_domain import NEVER_EXPIRES

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
SECURITY_DEFAULTS_PATH = '/policies/identitySecurityDefaultsEnforcementPolicy'


class M365TenantConfig(models.Model):
    _inherit = 'm365.tenant'

    config_profile_id = fields.Many2one(
        'm365.config.profile', string="Configuration Profile", tracking=True,
        ondelete='restrict',
        help="Baseline of the tenant, applied with Apply Configuration.")
    config_state = fields.Selection([
        ('not_applied', 'Not Applied'),
        ('applied', 'Applied'),
        ('failed', 'Failed'),
    ], string="Configuration", default='not_applied', readonly=True, copy=False)
    config_date = fields.Datetime(string="Configuration Applied On", readonly=True,
                                  copy=False)
    config_error = fields.Text(string="Configuration Error", readonly=True, copy=False)
    security_defaults_state = fields.Selection(
        [('on', 'On'), ('off', 'Off')], string="Security Defaults", readonly=True,
        copy=False, help="As last read from Microsoft 365 (empty: not read yet).")
    domain_ids = fields.One2many('m365.tenant.domain', 'tenant_id', string="Domains")
    domain_count = fields.Integer(compute='_compute_domain_count')

    @api.depends('domain_ids')
    def _compute_domain_count(self):
        for rec in self:
            rec.domain_count = len(rec.domain_ids)

    @api.model
    def default_get(self, fields):
        # Not a field default: that one would run when the column is added to the
        # existing tenants, before the profiles table exists
        res = super().default_get(fields)
        if 'config_profile_id' in fields and 'config_profile_id' not in res:
            res['config_profile_id'] = self.env['m365.config.profile']._get_default().id
        return res

    def write(self, vals):
        if 'config_profile_id' in vals:
            vals = dict(vals, config_state='not_applied')
        return super().write(vals)

    # ------------------------------------------------------------------
    # Graph calls
    # ------------------------------------------------------------------
    def graph_get_security_defaults(self):
        self.ensure_one()
        enabled = bool(self._graph_request('GET', SECURITY_DEFAULTS_PATH).get('isEnabled'))
        self.sudo().security_defaults_state = 'on' if enabled else 'off'
        return enabled

    def graph_set_security_defaults(self, enabled):
        """Turn Security Defaults on or off. Turning them off needs Enable Security
        Operations."""
        self.ensure_one()
        if not enabled:
            self._check_security_operations_allowed()
        self._graph_request('PATCH', SECURITY_DEFAULTS_PATH,
                            json_body={'isEnabled': bool(enabled)})
        self.sudo().security_defaults_state = 'on' if enabled else 'off'

    def _sync_domains(self):
        """Read the domains of the tenant. :return: the Graph items"""
        self.ensure_one()
        items = self._graph_request('GET', '/domains', paginate=True)
        Domain = self.env['m365.tenant.domain'].sudo()
        existing = {d.name: d for d in Domain.search([('tenant_id', '=', self.id)])}
        for item in items:
            name = (item.get('id') or '').lower()
            if not name:
                continue
            vals = Domain._values(item)
            if name in existing:
                existing.pop(name).write(vals)
            else:
                Domain.create(dict(vals, tenant_id=self.id, name=name))
        if existing:  # removed in Microsoft 365
            Domain.browse([d.id for d in existing.values()]).unlink()
        return items

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _apply_configuration(self):
        """Bring the tenant to its profile. Raises at the first failing step.

        :return: list of the changes made (each one is in the audit log)
        """
        self.ensure_one()
        profile = self.config_profile_id
        if not profile:
            raise UserError(_("Select a configuration profile for the tenant %s.", self.name))
        changes = []

        def changed(action, message):
            self._log(action, 'success', message)
            changes.append(message)

        # 1. Security Defaults
        if profile.security_defaults == 'keep':
            try:
                self.graph_get_security_defaults()
            except M365GraphError:
                pass  # informative only: no permission to read the policies
        else:
            wanted = profile.security_defaults == 'enable'
            if self.graph_get_security_defaults() != wanted:
                self.graph_set_security_defaults(wanted)
                changed('security_defaults',
                        _("Security Defaults turned on (profile %s).", profile.name) if wanted
                        else _("Security Defaults turned off (profile %s).", profile.name))
        # 2. Password expiry of the verified, managed domains
        if profile.password_never_expires:
            for item in self._sync_domains():
                if (item.get('isVerified') and item.get('authenticationType') != 'Federated'
                        and item.get('passwordValidityPeriodInDays') != NEVER_EXPIRES):
                    self._graph_request(
                        'PATCH', '/domains/%s' % self._graph_key(item['id']),
                        json_body={'passwordValidityPeriodInDays': NEVER_EXPIRES})
                    self.domain_ids.filtered(lambda d: d.name == item['id'].lower()).sudo() \
                        .password_validity_days = NEVER_EXPIRES
                    changed('configure_tenant',
                            _("Passwords of %s never expire.", item['id']))
        # 3. Technical contact
        email = (profile.technical_email or '').strip()
        if email:
            orgs = self._graph_request('GET', '/organization', paginate=True,
                                       params={'$select': 'id,technicalNotificationMails'})
            org = orgs[0] if orgs else {}
            mails = org.get('technicalNotificationMails') or []
            if org.get('id') and email.lower() not in {m.lower() for m in mails}:
                self._graph_request('PATCH', '/organization/%s' % org['id'],
                                    json_body={'technicalNotificationMails': mails + [email]})
                changed('configure_tenant', _("%s added to the technical contacts.", email))
        return changes

    def _apply_configuration_safe(self):
        """Apply the profile; never raises. :return: (ok, message)"""
        self.ensure_one()
        profile = self.config_profile_id
        try:
            changes = self._apply_configuration()
        except UserError as exc:  # M365GraphError included
            self.sudo().write({'config_state': 'failed', 'config_error': str(exc)})
            self._log('configure_tenant', 'failed', _(
                "Configuration %(profile)s failed: %(err)s", profile=profile.name or '-',
                err=exc))
            return False, "%s: %s" % (self.name, exc)
        self.sudo().write({'config_state': 'applied', 'config_date': fields.Datetime.now(),
                           'config_error': False})
        if not changes:
            self._log('configure_tenant', 'info', _(
                "Configuration %s checked: nothing to change.", profile.name))
        return True, "%s: %s" % (self.name, " ".join(changes) or _("nothing to change."))

    def action_apply_configuration(self):
        self._check_admin()
        ok, messages = True, []
        for tenant in self:
            if not tenant.filtered_domain(self._live_domain()):
                ok = False
                messages.append(_("%s: create the tenant in Microsoft 365 first.", tenant.name))
                continue
            t_ok, message = tenant._apply_configuration_safe()
            ok = ok and t_ok
            messages.append(message)
        return self._notify("\n".join(messages), 'success' if ok else 'danger')

    def _after_shared_app_connected(self):
        """A tenant just connected through the shared app gets its profile."""
        super()._after_shared_app_connected()
        if self.config_profile_id and self.config_state != 'applied':
            self._apply_configuration_safe()

    # ------------------------------------------------------------------
    # Domains
    # ------------------------------------------------------------------
    def action_sync_domains(self):
        self._check_admin()
        for tenant in self:
            tenant._sync_domains()
            tenant._log('domain', 'info', _("%s domain(s) read.", tenant.domain_count))
        return self._notify(_("Domains refreshed."))

    def action_add_domain(self):
        self._check_admin()
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Add Domain"),
            'res_model': 'm365.tenant.domain.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_tenant_id': self.id},
        }

    # ------------------------------------------------------------------
    # Permissions used by the configuration
    # ------------------------------------------------------------------
    def _probe_permissions(self):
        warnings = super()._probe_permissions()
        profile = self.config_profile_id
        probes = []
        if profile.security_defaults not in (False, 'keep') or self.allow_security_operations:
            probes.append((SECURITY_DEFAULTS_PATH, _("security policies (Policy.Read.All)")))
        if profile.password_never_expires or self.domain_ids:
            probes.append(('/domains', _("domains (Domain.Read.All)")))
        for path, label in probes:
            try:
                data = self._graph_request('GET', path)
            except M365GraphError as exc:
                warnings.append(_("No access to the %(what)s: %(err)s", what=label, err=exc))
                continue
            if path == SECURITY_DEFAULTS_PATH:
                self.sudo().security_defaults_state = 'on' if data.get('isEnabled') else 'off'
        return warnings
