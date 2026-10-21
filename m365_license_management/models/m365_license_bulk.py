# -*- coding: utf-8 -*-
from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

from .m365_tenant import M365GraphError


class M365LicenseBulkWizard(models.TransientModel):
    """Assign or remove licenses for several users at once.

    One license request is created per user and license, so every operation
    follows the approval workflow and is written to the audit log.
    """
    _name = 'm365.license.bulk.wizard'
    _description = 'Bulk Assign / Remove Microsoft 365 Licenses'

    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True)
    request_type = fields.Selection([
        ('assign', 'Assign License'),
        ('remove', 'Remove License'),
    ], string="Operation", required=True, default='assign')
    user_ids = fields.Many2many('m365.license.user', string="Users",
                                domain="[('tenant_id', '=', tenant_id)]")
    sku_ids = fields.Many2many('m365.sku', string="Licenses",
                               domain="[('tenant_id', '=', tenant_id)]")
    reason = fields.Text(string="Business Justification")
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')
    line_ids = fields.One2many('m365.license.bulk.line', 'wizard_id', string="Results")
    done_count = fields.Integer(compute='_compute_counts')
    submitted_count = fields.Integer(compute='_compute_counts')
    skipped_count = fields.Integer(compute='_compute_counts')
    failed_count = fields.Integer(compute='_compute_counts')

    @api.depends('line_ids.status')
    def _compute_counts(self):
        for wiz in self:
            statuses = wiz.line_ids.mapped('status')
            wiz.done_count = statuses.count('done')
            wiz.submitted_count = statuses.count('submitted')
            wiz.skipped_count = statuses.count('skipped')
            wiz.failed_count = statuses.count('failed')

    @api.onchange('tenant_id')
    def _onchange_tenant_id(self):
        self.user_ids = self.user_ids.filtered(lambda u: u.tenant_id == self.tenant_id)
        self.sku_ids = self.sku_ids.filtered(lambda s: s.tenant_id == self.tenant_id)

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Bulk Assign / Remove Licenses"),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def action_execute(self):
        self.ensure_one()
        if self.state == 'done':
            return self._reopen()
        if not self.user_ids or not self.sku_ids:
            raise UserError(_("Select at least one user and one license."))
        if (self.user_ids.tenant_id | self.sku_ids.tenant_id) != self.tenant_id:
            raise UserError(_("The users and the licenses must belong to the selected tenant."))
        # Inventory is synchronised once at the end instead of after every request,
        # so the free units are counted down here.
        Request = self.env['m365.license.request'].with_context(m365_defer_inventory_sync=True)
        available = {sku: sku.available_units for sku in self.sku_ids}
        assign = self.request_type == 'assign'
        lines = []
        for user in self.user_ids:
            for sku in self.sku_ids:
                vals = {'user_id': user.id, 'sku_id': sku.id}
                has_license = sku in user.sku_ids
                if assign and has_license:
                    vals.update(status='skipped', message=_("Already has this license."))
                elif not assign and not has_license:
                    vals.update(status='skipped', message=_("Does not have this license."))
                elif assign and available[sku] <= 0:
                    vals.update(status='failed',
                                message=_("No available license left in this tenant."))
                else:
                    vals.update(self._submit_request(Request, user, sku))
                    if assign and vals['status'] == 'done':
                        available[sku] -= 1
                lines.append(Command.create(vals))
        self.write({'state': 'done', 'line_ids': lines})
        if self.done_count:
            try:
                self.tenant_id._sync_inventory()
            except M365GraphError:
                pass  # the operations succeeded; the scheduled sync will catch up
        return self._reopen()

    def _submit_request(self, Request, user, sku):
        """Create and submit one request; a validation error only fails its own line."""
        try:
            with self.env.cr.savepoint():
                request = Request.create({
                    'tenant_id': self.tenant_id.id,
                    'user_id': user.id,
                    'sku_id': sku.id,
                    'request_type': self.request_type,
                    'reason': self.reason,
                })
                request.action_submit()
        except UserError as exc:
            return {'status': 'failed', 'message': str(exc)}
        return {
            'request_id': request.id,
            'status': request.state if request.state in ('done', 'submitted') else 'failed',
            'message': request.error_message or False,
        }


class M365LicenseBulkLine(models.TransientModel):
    _name = 'm365.license.bulk.line'
    _description = 'Bulk License Operation Result'
    _order = 'id'

    wizard_id = fields.Many2one('m365.license.bulk.wizard', required=True, ondelete='cascade')
    user_id = fields.Many2one('m365.license.user', string="User")
    sku_id = fields.Many2one('m365.sku', string="License")
    request_id = fields.Many2one('m365.license.request', string="Request")
    status = fields.Selection([
        ('done', 'Completed'),
        ('submitted', 'Waiting Approval'),
        ('skipped', 'Skipped'),
        ('failed', 'Failed'),
    ], string="Status")
    message = fields.Text(string="Message")
