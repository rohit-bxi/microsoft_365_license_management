# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'


class M365LicenseRequest(models.Model):
    """License assignment / removal request with approval and execution."""
    _name = 'm365.license.request'
    _description = 'Microsoft 365 License Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(string="Reference", default=lambda self: _('New'), readonly=True,
                       copy=False)
    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True, tracking=True,
                                index=True)
    request_type = fields.Selection([
        ('assign', 'Assign License'),
        ('remove', 'Remove License'),
    ], string="Operation", required=True, default='assign', tracking=True)
    user_id = fields.Many2one('m365.license.user', string="Microsoft 365 User", required=True,
                              domain="[('tenant_id', '=', tenant_id)]", tracking=True)
    sku_id = fields.Many2one('m365.sku', string="License", required=True,
                             domain="[('tenant_id', '=', tenant_id)]", tracking=True)
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
    company_id = fields.Many2one(related='tenant_id.company_id', store=True)
    is_admin = fields.Boolean(compute='_compute_is_admin')

    @api.depends_context('uid')
    def _compute_is_admin(self):
        is_admin = self.env.user.has_group(ADMIN_GROUP)
        for rec in self:
            rec.is_admin = is_admin

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

    @api.constrains('tenant_id', 'user_id', 'sku_id')
    def _check_same_tenant(self):
        for rec in self:
            if rec.user_id.tenant_id != rec.tenant_id or rec.sku_id.tenant_id != rec.tenant_id:
                raise ValidationError(_("The user and the license must belong to the selected tenant."))

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_("Only Microsoft 365 administrators can perform this action."))

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _validate_request(self):
        self.ensure_one()
        if not self.tenant_id.allow_license_operations:
            raise UserError(_("License operations are disabled for tenant %s.", self.tenant_id.name))
        if self.tenant_id.state == 'draft':
            raise UserError(_("Test the connection of tenant %s first.", self.tenant_id.name))
        has_license = self.sku_id in self.user_id.sku_ids
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
            if rec.tenant_id.auto_approve_admins and self.env.user.has_group(ADMIN_GROUP):
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
                    note=_("%(type)s - %(user)s - %(sku)s",
                           type=dict(rec._fields['request_type'].selection)[rec.request_type],
                           user=rec.user_id.name, sku=rec.sku_id.name))
        return True

    def action_approve(self):
        self._check_admin()
        for rec in self:
            if rec.state not in ('submitted', 'failed'):
                continue
            rec.write({'state': 'approved', 'approver_id': self.env.user.id})
            rec.sudo().activity_unlink(['mail.mail_activity_data_todo'])
            rec._execute()
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
            rec._execute()
        return True

    def action_reset_draft(self):
        for rec in self:
            if rec.state in ('rejected', 'failed'):
                rec.write({'state': 'draft', 'error_message': False, 'approver_id': False})
        return True

    # ------------------------------------------------------------------
    # Execution against Microsoft Graph
    # ------------------------------------------------------------------
    def _execute(self):
        """Run an approved request. Failures are stored, never raised."""
        self.ensure_one()
        tenant, sku, user = self.tenant_id, self.sku_id, self.user_id
        action = 'assign_license' if self.request_type == 'assign' else 'remove_license'
        try:
            if not tenant.allow_license_operations:
                raise M365GraphError(_("License operations are disabled for this tenant."))
            if self.request_type == 'assign':
                body = {'addLicenses': [{'skuId': sku.sku_id, 'disabledPlans': []}],
                        'removeLicenses': []}
            else:
                body = {'addLicenses': [], 'removeLicenses': [sku.sku_id]}
            tenant._graph_request('POST', '/users/%s/assignLicense' % user.graph_id,
                                  json_body=body)
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
        try:
            tenant._sync_inventory()
        except M365GraphError:
            pass  # the operation itself succeeded; the scheduled sync will catch up
        return True

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
