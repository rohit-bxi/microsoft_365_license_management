# -*- coding: utf-8 -*-
"""Tenant self-service of the portal users (``/my/m365/tenants``).

Portal users of the group *Microsoft 365: Portal Tenant Self-Service* add tenants of
type New (created in Microsoft 365 with the Partner Center account of their company)
and Existing (connected with their app registration), one by one or from a CSV / Excel
file. They only see the tenants they added (record rule on ``portal_user_id``).

Each portal step runs the method of the backend button, unchanged:

==============================  ====================================================
Portal step                     Backend method
==============================  ====================================================
Check (New)                     ``action_check``
Create in Microsoft 365, Retry  ``action_create``, then *Create Queued Tenants*
Reset (New, failed)             ``action_reset_creation``
I have saved the password       ``action_clear_password``
Test Connection                 ``action_test_connection``
Bulk import                     ``m365.tenant.import.wizard``: ``_parse``, templates
==============================  ====================================================

``action_check``, ``action_create`` and ``action_clear_password`` are restricted to
Microsoft 365 administrators: they run as OdooBot (an administrator), on the tenants of
the portal user only.
"""
import base64
import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.sql import escape_psql

from .m365_tenant_creation import VERIFIED_ADDRESS
from .m365_tenant_import import (COLUMNS, NEW_COLUMNS, NEW_TEMPLATE_NAME, TEMPLATE_FIELDS,
                                 TEMPLATE_NAME, UNIQUE_FIELDS)

_logger = logging.getLogger(__name__)

PORTAL_GROUP = 'm365_license_management.group_m365_portal'
PORTAL_JOBS_CRON = 'm365_license_management.ir_cron_m365_portal_jobs'
# Connection of a created new tenant: its directory ID comes from Microsoft
CONNECTION_FIELDS = ('client_id', 'client_secret', 'authority_url', 'graph_url', 'graph_scope')
# Left blank in a form: the current value is kept (the secret is never shown again)
KEEP_IF_BLANK = {'client_secret', 'authority_url', 'graph_url', 'graph_scope'}
PORTAL_JOB_BATCH = 20  # tenants checked / tested per run of the scheduled action


class M365Tenant(models.Model):
    _inherit = 'm365.tenant'

    portal_user_id = fields.Many2one(
        'res.users', string="Portal User", readonly=True, copy=False, index=True,
        help="Portal user who added the tenant on the portal. Only this user sees it there.")
    portal_job = fields.Selection([
        ('check', 'Check'),
        ('test', 'Test Connection'),
    ], string="Portal Background Job", readonly=True, copy=False, index=True,
        help="Check or connection test queued on the portal, done by the scheduled action "
             "Microsoft 365: Portal Background Jobs.")

    # ------------------------------------------------------------------
    # Portal forms
    # ------------------------------------------------------------------
    @api.model
    def _portal_partner_account(self):
        """Partner Center account the new tenants of the portal are created with."""
        return self.env['m365.partner.account'].sudo().search(
            [('company_id', 'in', [False, self.env.company.id])], limit=1)

    @api.model
    def _portal_columns(self, form):
        """(field name, value type, required) of a portal form, as in the import files.

        :param form: ``new`` (customer data of a new tenant), ``existing`` (an existing
                     tenant) or ``connection`` (app registration of a created new tenant)
        """
        if form == 'new':
            return NEW_COLUMNS
        fnames = TEMPLATE_FIELDS if form == 'existing' else CONNECTION_FIELDS
        return [column for column in COLUMNS if column[0] in fnames]

    @api.model
    def _portal_form_values(self, form, posted, tenant=None):
        """Convert and check a posted portal form like a row of an import file.

        :param form: see ``_portal_columns``
        :param posted: posted texts by field name
        :param tenant: the edited tenant, if any
        :return: (m365.tenant values, errors by field name, other errors)
        """
        Wizard = self.env['m365.tenant.import.wizard']
        countries = self.env['res.country'].sudo().search([])
        lookups = {'country': {country.code.lower(): country for country in countries}}
        vals, errors = {}, {}
        for fname, kind, required in self._portal_columns(form):
            label = self._fields[fname].string
            text = (posted.get(fname) or '').strip()
            if not text:
                if fname == 'client_secret':
                    required = not (tenant and tenant.sudo().client_secret)
                if required:
                    errors[fname] = _("%s is required.", label)
                elif tenant and fname not in KEEP_IF_BLANK:
                    vals[fname] = False
                continue
            try:
                vals[fname] = Wizard._convert(kind, text, lookups)
            except ValueError as exc:
                errors[fname] = str(exc)
        other_errors = []
        if form == 'new':
            if tenant and tenant._portal_show_address_confirmed():
                vals['address_confirmed'] = bool(posted.get('address_confirmed'))
            # The checks of the Check button, without repeating the errors above
            account = self._portal_partner_account()
            record = self.sudo().new(dict({
                'tenant_type': 'new', 'partner_account_id': account.id,
                'culture': account.default_culture, 'language': account.default_language,
            }, **vals))
            skip = set(errors.values()) | {_("%s is required.", self._fields[fname].string)
                                           for fname in errors}
            other_errors = [error for error in record._check_values() if error not in skip]
        # Unique among all tenants, as in the import files
        Tenant = self.sudo().with_context(active_test=False)
        for fname in UNIQUE_FIELDS:
            if fname in errors or not vals.get(fname):
                continue
            domain = [(fname, '=ilike', escape_psql(vals[fname]))]
            if tenant:
                domain.append(('id', '!=', tenant.id))
            if Tenant.search_count(domain, limit=1):
                errors[fname] = _("%(field)s \"%(value)s\" already exists in Odoo.",
                                  field=self._fields[fname].string, value=vals[fname])
        return vals, errors, other_errors

    @api.model
    def _portal_form_defaults(self, form):
        """Texts of an empty portal form."""
        if form != 'new':
            return {}
        account = self._portal_partner_account()
        return {'culture': account.default_culture, 'language': account.default_language}

    def _portal_form_data(self, form):
        """Texts of the fields of a portal form, from the tenant (never the secret)."""
        self.ensure_one()
        rec = self.sudo()
        data = {}
        for fname, _kind, _required in self._portal_columns(form):
            if fname == 'client_secret':
                continue
            value = rec[fname]
            if fname == 'country_id':
                value = value.code
            data[fname] = value or ''
        data['address_confirmed'] = rec.address_confirmed
        return data

    def _portal_show_address_confirmed(self):
        """Microsoft could not verify the address: the user may confirm it."""
        self.ensure_one()
        return bool(self.address_confirmed
                    or (self.address_status and self.address_status not in VERIFIED_ADDRESS))

    @api.model
    def _portal_create(self, vals_list, filename=None):
        """Add tenants for the current portal user.

        New tenants are created in Microsoft 365 with the Partner Center account of the
        portal. The user is not subscribed to the tenants: the chatter stays internal.
        """
        account = self._portal_partner_account()
        vals_list = [dict(vals, portal_user_id=self.env.uid) for vals in vals_list]
        for vals in vals_list:
            if vals.get('tenant_type') == 'new':
                vals['partner_account_id'] = account.id
        tenants = self.sudo().with_context(mail_create_nosubscribe=True).create(vals_list)
        if filename:
            body = _("Tenant added on the portal by %(user)s, from the file %(file)s.",
                     user=self.env.user.name, file=filename)
        else:
            body = _("Tenant added on the portal by %s.", self.env.user.name)
        for tenant in tenants:
            tenant._message_log(body=body)
        return self.sudo().browse(tenants.ids)

    def _portal_save(self, vals):
        """Write the values of a portal form that changed."""
        self.ensure_one()
        rec = self.sudo()
        changed = {}
        for fname, value in vals.items():
            current = rec[fname]
            if isinstance(current, models.BaseModel):
                current = current.id
            if current != value:
                changed[fname] = value
        if changed:
            rec.write(changed)

    # ------------------------------------------------------------------
    # Portal status and steps
    # ------------------------------------------------------------------
    def _portal_url(self, suffix=''):
        self.ensure_one()
        return '/my/m365/tenants/%s%s' % (self.id, suffix)

    def _portal_status(self):
        """(label, Bootstrap colour) of the tenant on the portal."""
        self.ensure_one()
        if self.portal_job:
            return (_("Checking…") if self.portal_job == 'check' else _("Testing…")), 'info'
        if self.tenant_type == 'new' and self.provision_state != 'done':
            return {
                'ready': (_("Ready"), 'primary'),
                'queued': (_("Creating…"), 'info'),
                'failed': (_("Failed"), 'danger'),
            }.get(self.provision_state, (_("To Check"), 'warning'))
        return {
            'connected': (_("Connected"), 'success'),
            'error': (_("Connection Error"), 'danger'),
        }.get(self.state, (_("Not Connected"), 'secondary'))

    def _portal_busy(self):
        """A background step is running: the portal page reloads itself."""
        self.ensure_one()
        return bool(self.portal_job or self.provision_state == 'queued')

    def _portal_operations(self):
        """Steps the portal user can take on the tenant in its current status."""
        self.ensure_one()
        rec = self.sudo()
        if rec.portal_job:
            return set()
        if rec.tenant_type == 'new' and rec.provision_state != 'done':
            operations = {
                'draft': {'edit', 'check', 'remove'},
                'ready': {'edit', 'check', 'create', 'remove'},
                'failed': {'edit', 'create', 'reset'},
            }.get(rec.provision_state, set())
            if rec.provision_state == 'failed' and not rec.pc_request_id:
                # Without pending request, the tenant does not exist in Microsoft 365
                operations = operations | {'remove'}
            return operations
        operations = {'connect'}
        if rec.tenant_id and rec.client_id:
            operations.add('test')
        if rec.tenant_type == 'existing':
            operations.add('remove')
        if rec.admin_password:
            operations.add('password')
        return operations

    def _portal_as_admin(self):
        """The tenants as OdooBot, a Microsoft 365 administrator, for the backend buttons
        restricted to administrators."""
        return self.with_user(SUPERUSER_ID)

    def _portal_check(self):
        """Check the data, the domain and the address of new tenants (Check button)."""
        self._portal_as_admin().action_check()

    def _portal_create_in_microsoft(self):
        """Queue the creation of the checked or failed new tenants (Create button).

        :return: the queued tenants
        """
        todo = self.filtered(lambda t: 'create' in t._portal_operations())
        if todo:
            todo._portal_as_admin().action_create()
            # The audit log of the creation names the portal user, not OdooBot
            todo.sudo().write({'queued_by': self.env.uid})
        return todo

    def _portal_reset(self):
        self.sudo().action_reset_creation()

    def _portal_clear_password(self):
        self._portal_as_admin().action_clear_password()

    def _portal_test_connection(self):
        """Test Connection as the current user, who is named in the audit log."""
        self.sudo().action_test_connection()

    def _portal_remove(self):
        """Withdraw the tenant from the portal: archived, so that its audit log is kept."""
        self.sudo().write({'active': False, 'portal_job': False})

    def _portal_queue(self, job):
        """Check or test the tenants in the background, e.g. the tenants of a file."""
        if self:
            self.sudo().write({'portal_job': job})
            self.env.ref(PORTAL_JOBS_CRON).sudo()._trigger()

    @api.model
    def _cron_portal_jobs(self):
        """Run the checks and connection tests queued on the portal."""
        tenants = self.search([('portal_job', '!=', False)], order='id',
                              limit=PORTAL_JOB_BATCH + 1)
        for tenant in tenants[:PORTAL_JOB_BATCH]:
            try:
                if tenant.portal_job == 'check':
                    tenant._portal_as_admin().action_check()
                elif tenant.filtered_domain(self._live_domain()):
                    owner = tenant.portal_user_id or self.env.user
                    tenant.with_user(owner)._portal_test_connection()
            except Exception:  # unexpected (DB, bug): keep the other tenants running
                self.env.cr.rollback()
                _logger.exception("Microsoft 365 portal job failed for tenant %s", tenant.id)
            tenant.portal_job = False
            tenant._commit()
        if len(tenants) > PORTAL_JOB_BATCH:
            self.env.ref(PORTAL_JOBS_CRON)._trigger()

    # ------------------------------------------------------------------
    # Bulk import (Bulk Import Tenant wizard of the backend)
    # ------------------------------------------------------------------
    @api.model
    def _portal_import_wizard(self, tenant_type, content=None, filename=None):
        """Bulk Import Tenant wizard of the portal user (as sudo: administrators only)."""
        vals = {'tenant_type': tenant_type,
                'partner_account_id': self._portal_partner_account().id}
        if content is not None:
            vals.update(file=base64.b64encode(content), filename=filename)
        return self.env['m365.tenant.import.wizard'].sudo().create(vals)

    @api.model
    def _portal_parse(self, wizard):
        """Rows of the file of the wizard (see ``_parse``), restricted to the columns of
        the template and to ``m365.portal_import_max_rows`` rows."""
        rows = wizard._parse()
        form = 'new' if wizard.tenant_type == 'new' else 'existing'
        allowed = {fname for fname, _kind, _required in self._portal_columns(form)}
        extra = set().union(*(row['raw'] for row in rows)) - allowed
        if extra:
            raise UserError(_(
                "These columns cannot be imported on the portal: %s. Use the columns of "
                "the template.", ", ".join(sorted(self._fields[f].string for f in extra))))
        limit = int(self.env['ir.config_parameter'].sudo().get_param(
            'm365.portal_import_max_rows', 100) or 0)
        if limit and len(rows) > limit:
            raise UserError(_("The file has %(count)s tenants: at most %(limit)s can be "
                              "imported at once. Split the file.",
                              count=len(rows), limit=limit))
        return rows

    @api.model
    def _portal_import_template(self, tenant_type):
        """(content, file name) of the template of the import file."""
        wizard = self._portal_import_wizard(tenant_type)
        wizard.action_download_template()
        filename = NEW_TEMPLATE_NAME if tenant_type == 'new' else TEMPLATE_NAME
        return base64.b64decode(wizard.template_file), filename
