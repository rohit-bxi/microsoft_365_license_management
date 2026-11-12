# -*- coding: utf-8 -*-
"""MFA of a Microsoft 365 user: live view of the methods, MFA requests, Temporary Access
Pass. Nothing about the methods is stored on the user: the wizard reads them from
Microsoft Graph each time (phone numbers and emails masked)."""
from odoo import Command, _, fields, models

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'


class M365LicenseUserMfa(models.Model):
    _inherit = 'm365.license.user'

    allow_security_operations = fields.Boolean(
        related='tenant_id.allow_security_operations')

    def action_view_mfa(self):
        """Methods and per-user MFA state, read live."""
        self._check_admin()
        self.ensure_one()
        wizard = self.env['m365.user.mfa.wizard'].create({'user_id': self.id})
        wizard._load()
        return wizard._reopen()

    def action_request_mfa_reset(self):
        return self._action_request('mfa_reset')

    def action_request_mfa_disable(self):
        return self._action_request('mfa_disable')


class M365UserMfaWizard(models.TransientModel):
    _name = 'm365.user.mfa.wizard'
    _description = 'MFA of a Microsoft 365 User'

    user_id = fields.Many2one('m365.license.user', string="User", required=True,
                              readonly=True)
    tenant_id = fields.Many2one(related='user_id.tenant_id')
    allow_security_operations = fields.Boolean(
        related='user_id.tenant_id.allow_security_operations')
    method_ids = fields.One2many('m365.user.mfa.method', 'wizard_id', string="Methods",
                                 readonly=True)
    per_user_mfa_state = fields.Char(string="Per-user MFA", readonly=True)
    error = fields.Text(readonly=True)
    tap_lifetime = fields.Integer(string="Valid For (Minutes)", default=60)
    tap_usable_once = fields.Boolean(string="Usable Once", default=True)
    tap_code = fields.Char(string="Temporary Access Pass", readonly=True, groups=ADMIN_GROUP)
    tap_expiry = fields.Char(string="Valid Until", readonly=True)
    tap_policy_state = fields.Selection(
        [('enabled', 'Enabled'), ('disabled', 'Disabled')], string="Temporary Access Pass "
        "Policy", readonly=True, help="Method policy of the tenant (empty: not readable).")
    tap_policy_once = fields.Boolean(string="One-time Passes Only", readonly=True)
    tap_policy_min = fields.Integer(string="Minimum Lifetime", readonly=True)
    tap_policy_max = fields.Integer(string="Maximum Lifetime", readonly=True)

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("MFA of %s", self.user_id.name),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _load(self):
        self.ensure_one()
        tenant, user = self.tenant_id, self.user_id
        errors, lines, state = [], [Command.clear()], False
        try:
            for method in tenant.graph_get_auth_methods(user.graph_id):
                lines.append(Command.create({
                    'method_type': method['type'], 'detail': method['detail'] or False,
                    'created': (method['created'] or '')[:19].replace('T', ' ') or False,
                    'removable': not method['is_password'] and bool(method['segment']),
                }))
        except M365GraphError as exc:
            errors.append(_("Methods: %s", exc))
        try:
            state = tenant.graph_get_per_user_mfa(user.graph_id)
        except M365GraphError as exc:
            errors.append(_("Per-user MFA: %s", exc))
        policy = {}
        if tenant.allow_security_operations:
            try:
                policy = tenant.graph_get_tap_policy()
            except M365GraphError as exc:
                errors.append(_("Temporary Access Pass policy: %s", exc))
        self.write({
            'method_ids': lines, 'per_user_mfa_state': state or False,
            'error': "\n".join(errors) or False,
            'tap_policy_state': policy.get('state') if policy.get('state') in (
                'enabled', 'disabled') else False,
            'tap_policy_once': bool(policy.get('isUsableOnce')),
            'tap_policy_min': policy.get('minimumLifetimeInMinutes') or 0,
            'tap_policy_max': policy.get('maximumLifetimeInMinutes') or 0,
        })
        if policy.get('defaultLifetimeInMinutes') and not self.tap_code:
            self.tap_lifetime = policy['defaultLifetimeInMinutes']
        if self.tap_policy_once:
            self.tap_usable_once = True

    def action_refresh(self):
        self._load()
        return self._reopen()

    def action_request_reset(self):
        return self.user_id.action_request_mfa_reset()

    def action_request_disable(self):
        return self.user_id.action_request_mfa_disable()

    def action_issue_tap(self):
        """Temporary Access Pass, shown once in the wizard."""
        self.ensure_one()
        self.env['m365.tenant']._check_admin()
        tenant, user = self.tenant_id, self.user_id
        if not 10 <= self.tap_lifetime <= 43200:
            raise M365GraphError(_("A Temporary Access Pass is valid 10 minutes to 30 days."))
        if self.tap_policy_state == 'disabled':
            raise M365GraphError(_("The Temporary Access Pass method is disabled in the "
                                   "tenant: use Enable Temporary Access Pass first."))
        if self.tap_policy_min and self.tap_lifetime < self.tap_policy_min or \
                self.tap_policy_max and self.tap_lifetime > self.tap_policy_max:
            raise M365GraphError(_("The policy of the tenant allows %(min)s to %(max)s "
                                   "minutes.", min=self.tap_policy_min or 10,
                                   max=self.tap_policy_max or 43200))
        if self.tap_policy_once and not self.tap_usable_once:
            raise M365GraphError(_("The policy of the tenant allows one-time passes only."))
        try:
            item = tenant.graph_create_temporary_access_pass(
                user.graph_id, self.tap_lifetime, self.tap_usable_once)
        except M365GraphError as exc:
            # Shown in the wizard, not raised: the audit log line is kept
            tenant._log('tap', 'failed', "%s: %s" % (user.name, exc))
            self.error = _("Temporary Access Pass not issued: %s", exc)
            return self._reopen()
        tenant._log('tap', 'success', _(
            "Temporary Access Pass issued for %(user)s, valid %(minutes)s minutes%(once)s.",
            user=user.name, minutes=self.tap_lifetime,
            once=_(", usable once") if self.tap_usable_once else ''))
        start = (item.get('startDateTime') or '')[:19].replace('T', ' ')
        self.write({'tap_code': item.get('temporaryAccessPass'),
                    'tap_expiry': _("%(minutes)s minutes from %(start)s (UTC)",
                                    minutes=item.get('lifetimeInMinutes') or self.tap_lifetime,
                                    start=start or _("now"))})
        self._load()
        return self._reopen()

    def action_enable_tap(self):
        """Enable the Temporary Access Pass method of the tenant."""
        self.ensure_one()
        self.env['m365.tenant']._check_admin()
        tenant = self.tenant_id
        try:
            tenant.graph_enable_tap_policy()
        except M365GraphError as exc:
            tenant._log('tap', 'failed', _("Temporary Access Pass not enabled: %s", exc))
            self.error = _("Temporary Access Pass not enabled: %s", exc)
            return self._reopen()
        tenant._log('tap', 'success', _("Temporary Access Pass method enabled in the tenant."))
        self._load()
        return self._reopen()

    def action_done(self):
        """Forget the Temporary Access Pass and close."""
        self.sudo().write({'tap_code': False})
        return {'type': 'ir.actions.act_window_close'}


class M365UserMfaMethod(models.TransientModel):
    _name = 'm365.user.mfa.method'
    _description = 'MFA Method of a Microsoft 365 User'

    wizard_id = fields.Many2one('m365.user.mfa.wizard', required=True, ondelete='cascade')
    method_type = fields.Char(string="Method")
    detail = fields.Char(string="Detail")
    created = fields.Char(string="Registered On")
    removable = fields.Boolean(string="Removed by Reset MFA")
