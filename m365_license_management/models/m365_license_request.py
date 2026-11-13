# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
LICENSE_TYPES = ('assign', 'remove')
# Remove MFA: need Enable Security Operations, a justification, never approved automatically
MFA_TYPES = ('mfa_reset', 'mfa_disable')


class M365LicenseRequest(models.Model):
    """Request with approval and execution: license assignment / removal, MFA reset or
    per-user MFA turned off."""
    _name = 'm365.license.request'
    _description = 'Microsoft 365 License Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string="Reference", default=lambda self: _('New'), readonly=True,
                       copy=False)
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True, tracking=True,
                                index=True, domain="['|', ('tenant_type', '=', 'existing'), "
                                                   "('provision_state', '=', 'done')]")
    request_type = fields.Selection([
        ('assign', 'Assign License'),
        ('remove', 'Remove License'),
        ('mfa_reset', 'Reset MFA'),
        ('mfa_disable', 'Turn Off Per-user MFA'),
    ], string="Operation", required=True, default='assign', tracking=True,
        help="Reset MFA: every MFA method of the user is deleted; the user registers MFA "
             "again at the next sign-in.\n"
             "Turn Off Per-user MFA: the legacy per-user MFA of the user is disabled.")
    user_id = fields.Many2one('m365.license.user', string="Microsoft 365 User", required=True,
                              domain="[('tenant_id', '=', tenant_id)]", tracking=True)
    sku_id = fields.Many2one('m365.sku', string="License",
                             domain="[('tenant_id', '=', tenant_id)]", tracking=True,
                             help="Required to assign or remove a license.")
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Waiting Approval'),
        ('approved', 'Approved'),
        ('done', 'Completed'),
        ('failed', 'Failed'),
        ('rejected', 'Rejected'),
    ], string="Status", default='draft', tracking=True, copy=False, index=True)
    requested_by = fields.Many2one('res.users', string="Requested By",
                                   default=lambda self: self.env.user, readonly=True,
                                   tracking=True)
    approver_id = fields.Many2one('res.users', string="Approved / Rejected By", readonly=True,
                                  copy=False)
    reason = fields.Text(string="Business Justification")
    reject_reason = fields.Text(string="Rejection Reason", copy=False)
    executed_date = fields.Datetime(string="Executed On", readonly=True, copy=False)
    error_message = fields.Text(string="Error", readonly=True, copy=False)
    allow_operations = fields.Boolean(related='tenant_id.allow_license_operations')
    allow_security_operations = fields.Boolean(
        related='tenant_id.allow_security_operations')
    is_mfa = fields.Boolean(compute='_compute_is_mfa')
    can_buy = fields.Boolean(compute='_compute_can_buy',
                             help="No license left: it can be bought from Ingram Micro.")
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)
    is_admin = fields.Boolean(compute='_compute_is_admin')

    @api.depends_context('uid')
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group(ADMIN_GROUP)
        for rec in self:
            rec.is_admin = is_admin

    @api.depends('request_type')
    def _compute_is_mfa(self):
        for rec in self:
            rec.is_mfa = rec.request_type in MFA_TYPES

    @api.depends('request_type', 'sku_id.available_units', 'state')
    @api.depends_context('uid')
    def _compute_can_buy(self):
        is_admin = self.env.user.has_group(ADMIN_GROUP)
        for rec in self:
            rec.can_buy = (is_admin and rec.request_type == 'assign' and rec.sku_id
                           and rec.sku_id.available_units <= 0
                           and rec.state in ('draft', 'failed'))

    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('m365.license.request') or _('New')
        return super().create(vals_list)

    @api.onchange('tenant_id')
    def _onchange_tenant_id(self):
        if self.user_id and self.user_id.tenant_id != self.tenant_id:
            self.user_id = False
        if self.sku_id and self.sku_id.tenant_id != self.tenant_id:
            self.sku_id = False

    @api.onchange('request_type')
    def _onchange_request_type(self):
        if self.request_type in MFA_TYPES:
            self.sku_id = False

    @api.constrains('tenant_id', 'user_id', 'sku_id')
    def _check_same_tenant(self):
        for rec in self:
            if (rec.user_id.tenant_id != rec.tenant_id
                    or (rec.sku_id and rec.sku_id.tenant_id != rec.tenant_id)):
                raise ValidationError(_("The user and the license must belong to the selected tenant."))

    @api.constrains('request_type', 'sku_id')
    def _check_license(self):
        for rec in self:
            if rec.request_type in LICENSE_TYPES and not rec.sku_id:
                raise ValidationError(_("Select the license to assign or remove."))
            if rec.request_type in MFA_TYPES and rec.sku_id:
                raise ValidationError(_("An MFA request has no license."))

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _validate_request(self):
        self.ensure_one()
        if self.request_type in MFA_TYPES:
            return self._validate_mfa_request()
        if not self.tenant_id.allow_license_operations:
            raise UserError(_("License operations are disabled for tenant %s.", self.tenant_id.name))
        if self.tenant_id.state == 'draft':
            raise UserError(_("Test the connection of tenant %s first.", self.tenant_id.name))
        has_license = self.sku_id in self.user_id.sku_ids
        if self.request_type == 'assign' and (self.sku_id.applies_to or 'User') != 'User':
            raise UserError(_("%(sku)s is a license of the whole tenant (applies to "
                              "%(scope)s): Microsoft 365 does not assign it to users.",
                              sku=self.sku_id.name, scope=self.sku_id.applies_to))
        if self.request_type == 'assign':
            if has_license:
                raise UserError(_("%(user)s already has %(sku)s.",
                                  user=self.user_id.name, sku=self.sku_id.name))
            if self.sku_id.available_units <= 0:
                raise UserError(_("No available %s license left in this tenant.", self.sku_id.name))
            if self.user_id.last_sync and not self.user_id.usage_location:
                raise UserError(_("%s has no usage location. Microsoft requires it before a license "
                                  "can be assigned.", self.user_id.name))
        elif not has_license:
            raise UserError(_("%(user)s does not have %(sku)s.",
                              user=self.user_id.name, sku=self.sku_id.name))
        duplicate = self.search_count([
            ('id', '!=', self.id), ('tenant_id', '=', self.tenant_id.id),
            ('user_id', '=', self.user_id.id), ('sku_id', '=', self.sku_id.id),
            ('request_type', '=', self.request_type), ('state', 'in', ('submitted', 'approved')),
        ])
        if duplicate:
            raise UserError(_("A similar request is already pending for this user and license."))

    def _validate_mfa_request(self):
        self.ensure_one()
        tenant = self.tenant_id
        if not tenant.allow_security_operations:
            raise UserError(_("Security operations are disabled for tenant %s.", tenant.name))
        if tenant.state == 'draft':
            raise UserError(_("Test the connection of tenant %s first.", tenant.name))
        if not (self.reason or '').strip():
            raise UserError(_("Give the business justification of the MFA operation."))
        # sudo: the requests of other users are hidden by the record rules
        duplicate = self.sudo().search_count([
            ('id', '!=', self.id), ('tenant_id', '=', tenant.id),
            ('user_id', '=', self.user_id.id), ('request_type', '=', self.request_type),
            ('state', 'in', ('submitted', 'approved')),
        ])
        if duplicate:
            raise UserError(_("A similar request is already pending for this user."))

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def action_submit(self):
        admin_users = None
        for rec in self:
            if rec.state != 'draft':
                continue
            rec._validate_request()
            rec.state = 'submitted'
            rec.tenant_id._log('request', 'info', _("%(ref)s submitted by %(user)s.",
                                                    ref=rec.name, user=self.env.user.name),
                               request=rec)
            if (rec.request_type not in MFA_TYPES and rec.tenant_id.auto_approve_admins
                    and self.env.user.has_group(ADMIN_GROUP)):
                rec.action_approve()
                continue
            # route to IT administrators
            if admin_users is None:
                users = self.env['res.users'].sudo().search([('share', '=', False)])
                admin_users = users.filtered(lambda u: u.has_group(ADMIN_GROUP))
            for admin in admin_users:
                rec.sudo().activity_schedule(
                    'mail.mail_activity_data_todo', user_id=admin.id,
                    summary=_("Approve license request %s", rec.name),
                    note=rec._summary())
        return True

    def action_approve(self):
        """Approve and run the submitted requests (a failed one is run again with Retry)."""
        self._check_admin()
        for rec in self:
            if rec.state != 'submitted':
                continue
            rec.write({'state': 'approved', 'approver_id': self.env.user.id})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
            rec._execute_if_valid()
        return True

    def action_reject(self):
        self._check_admin()
        for rec in self:
            if rec.state not in ('submitted', 'failed'):
                continue
            rec.write({'state': 'rejected', 'approver_id': self.env.user.id})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
            rec.tenant_id._log('request', 'info', _("%(ref)s rejected by %(user)s. %(reason)s",
                                                    ref=rec.name, user=self.env.user.name,
                                                    reason=rec.reject_reason or ''),
                               request=rec)
        return True

    def action_retry(self):
        self._check_admin()
        for rec in self.filtered(lambda r: r.state == 'failed'):
            rec.state = 'approved'
            rec._execute_if_valid()
        return True

    def action_reset_draft(self):
        for rec in self:
            if rec.state in ('rejected', 'failed'):
                rec.write({'state': 'draft', 'error_message': False, 'approver_id': False})
        return True

    def _summary(self):
        self.ensure_one()
        operation = dict(self._fields['request_type'].selection)[self.request_type]
        if self.sku_id:
            return _("%(type)s - %(user)s - %(sku)s", type=operation, user=self.user_id.name,
                     sku=self.sku_id.name)
        return _("%(type)s - %(user)s", type=operation, user=self.user_id.name)

    # ------------------------------------------------------------------
    # Execution against Microsoft Graph
    # ------------------------------------------------------------------
    def _execute_if_valid(self):
        """Check the request again (things changed since it was submitted: license given
        meanwhile, no license left, operations turned off...), then run it. Failures are
        stored, never raised."""
        self.ensure_one()
        try:
            self._validate_request()
        except UserError as exc:
            self.write({'state': 'failed', 'error_message': str(exc)})
            action = (self.request_type if self.request_type in MFA_TYPES else
                      'assign_license' if self.request_type == 'assign' else 'remove_license')
            self.tenant_id._log(action, 'failed', _("%(ref)s - %(summary)s: %(err)s",
                                                    ref=self.name, summary=self._summary(),
                                                    err=exc), request=self)
            return False
        return self._execute()

    def _execute(self):
        """Run an approved request. Failures are stored, never raised."""
        self.ensure_one()
        if self.request_type in MFA_TYPES:
            return self._execute_mfa()
        tenant, sku, user = self.tenant_id, self.sku_id, self.user_id
        action = 'assign_license' if self.request_type == 'assign' else 'remove_license'
        try:
            if not tenant.allow_license_operations:
                raise M365GraphError(_("License operations are disabled for this tenant."))
            if self.request_type == 'assign':
                tenant.graph_assign_license(user.graph_id, add_sku_ids=[sku.sku_id])
            else:
                tenant.graph_assign_license(user.graph_id, remove_sku_ids=[sku.sku_id])
        except M365GraphError as exc:
            self.write({'state': 'failed', 'error_message': str(exc)})
            tenant._log(action, 'failed', _("%(ref)s - %(user)s / %(sku)s: %(err)s",
                                            ref=self.name, user=user.name, sku=sku.name,
                                            err=exc), request=self)
            return False
        # local mirror of the result + refresh counters
        user.sudo().write({'sku_ids': [(4 if self.request_type == 'assign' else 3, sku.id)]})
        self.write({'state': 'done', 'executed_date': fields.Datetime.now(),
                    'error_message': False})
        tenant._log(action, 'success', _("%(ref)s - %(user)s / %(sku)s completed.",
                                         ref=self.name, user=user.name, sku=sku.name),
                    request=self)
        if self.env.context.get('m365_defer_inventory_sync'):
            return True  # bulk operations synchronise once at the end
        try:
            tenant._sync_inventory()
        except M365GraphError:
            pass  # the operation itself succeeded; the scheduled sync will catch up
        return True

    def _execute_mfa(self):
        """Run an approved MFA request. Failures are stored, never raised."""
        self.ensure_one()
        tenant, user, action = self.tenant_id, self.user_id, self.request_type
        try:
            tenant._check_security_operations_allowed()
            if action == 'mfa_reset':
                removed, left = tenant.graph_reset_mfa(user.graph_id)
                done = ", ".join("%s %s" % (m['type'], m['detail']) for m in removed).strip()
                if left:
                    raise M365GraphError(_(
                        "Removed: %(done)s. Not removed: %(left)s",
                        done=done or _("none"),
                        left="; ".join("%s %s (%s)" % (m['type'], m['detail'], err)
                                       for m, err in left)))
                result = _("%(ref)s - MFA of %(user)s reset; removed: %(done)s.",
                           ref=self.name, user=user.name, done=done or _("no method"))
            else:
                tenant.graph_set_per_user_mfa(user.graph_id, 'disabled')
                result = _("%(ref)s - per-user MFA of %(user)s turned off.",
                           ref=self.name, user=user.name)
        except M365GraphError as exc:
            self.write({'state': 'failed', 'error_message': str(exc)})
            tenant._log(action, 'failed', _("%(ref)s - %(user)s: %(err)s", ref=self.name,
                                            user=user.name, err=exc), request=self)
            return False
        self.write({'state': 'done', 'executed_date': fields.Datetime.now(),
                    'error_message': False})
        tenant._log(action, 'success', result, request=self)
        return True

    def action_buy_licenses(self):
        self.ensure_one()
        return self.sku_id.action_buy_licenses()

    # ------------------------------------------------------------------
    # Dashboard entry point
    # ------------------------------------------------------------------
    @api.model
    def create_from_dashboard(self, tenant_id, user_id, sku_id, request_type, reason=False):
        rec = self.create({
            'tenant_id': tenant_id,
            'user_id': user_id,
            'sku_id': sku_id,
            'request_type': request_type,
            'reason': reason,
        })
        rec.action_submit()
        return {'id': rec.id, 'name': rec.name, 'state': rec.state,
                'error': rec.error_message or ''}
