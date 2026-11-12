# -*- coding: utf-8 -*-
"""Users created in a tenant from Odoo (``POST /users``), one by one or from a file.

The user gets a generated password that must be changed at the first sign-in. The
password is shown once, in the wizard or in its result file, and removed with *Done*
(Odoo deletes the old wizards in any case). The licenses are then requested through the
usual license requests, so the approval rules apply.
"""
import base64
import io
import re
import secrets
import string
from collections import defaultdict

import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
UPN_RE = re.compile(r'^[A-Za-z0-9._%+\-\']{1,64}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$')
NICKNAME_STRIP_RE = re.compile(r'[^A-Za-z0-9._\-]')
SYMBOLS = '!@#$%^&*-_=+?'


def generate_password(length=16):
    """Random password meeting the Microsoft Entra complexity rules."""
    pools = (string.ascii_lowercase, string.ascii_uppercase, string.digits, SYMBOLS)
    chars = [secrets.choice(pool) for pool in pools]
    everything = ''.join(pools)
    chars += [secrets.choice(everything) for _i in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


class M365TenantUsers(models.Model):
    _inherit = 'm365.tenant'

    def graph_create_user(self, values):
        """POST /users"""
        self.ensure_one()
        self._check_operations_allowed()
        return self._graph_request('POST', '/users', json_body=values)

    def _verified_domains(self):
        """Verified domain names of the tenant, read from Microsoft when not known yet."""
        self.ensure_one()
        if not self.domain_ids:
            self._sync_domains()
        return set(self.domain_ids.filtered('is_verified').mapped('name'))

    def _default_usage_location(self):
        self.ensure_one()
        return (self.config_profile_id.usage_location_id.code or self.country_id.code
                or self.company_id.country_id.code or False)

    def _check_new_user(self, upn, display_name, usage_location=None, verified=None):
        """Errors of the data of a new user (empty: valid)."""
        self.ensure_one()
        errors = []
        if not display_name:
            errors.append(_("The display name is required."))
        if not UPN_RE.match(upn or ''):
            errors.append(_("%s is not a valid user principal name.", upn or '-'))
        else:
            domain = upn.rsplit('@', 1)[1].lower()
            verified = self._verified_domains() if verified is None else verified
            if domain not in verified:
                errors.append(_("%(domain)s is not a verified domain of tenant %(tenant)s.",
                                domain=domain, tenant=self.name))
            if self.env['m365.license.user'].sudo().with_context(active_test=False).search_count([
                    ('tenant_id', '=', self.id), ('user_principal_name', '=ilike', upn)]):
                errors.append(_("%s already exists in the tenant.", upn))
        if usage_location and not self.env['res.country'].sudo().search_count(
                [('code', '=', usage_location.upper())]):
            errors.append(_("%s is not a valid ISO 3166-1 alpha-2 country code.",
                            usage_location))
        return errors

    def _create_user(self, upn, display_name, given_name=None, surname=None,
                     usage_location=None, department=None, job_title=None):
        """Create the user in Microsoft 365 and its Odoo record.

        :return: (m365.license.user, password)
        :raise M365GraphError: the user is not created
        """
        self.ensure_one()
        password = generate_password()
        nickname = NICKNAME_STRIP_RE.sub('', upn.split('@', 1)[0])[:64] or 'user'
        body = {
            'accountEnabled': True,
            'displayName': display_name,
            'mailNickname': nickname,
            'userPrincipalName': upn,
            'passwordProfile': {'forceChangePasswordNextSignIn': True, 'password': password},
        }
        optional = {'givenName': given_name, 'surname': surname,
                    'usageLocation': (usage_location or '').upper() or None,
                    'department': department, 'jobTitle': job_title}
        body.update({key: value for key, value in optional.items() if value})
        try:
            item = self.graph_create_user(body)
        except M365GraphError as exc:
            self._log('create_user', 'failed', "%s: %s" % (upn, exc))
            raise
        user = self.env['m365.license.user'].sudo().create({
            'tenant_id': self.id,
            'graph_id': item['id'],
            'name': item.get('displayName') or display_name,
            'user_principal_name': item.get('userPrincipalName') or upn,
            'mail': item.get('mail') or False,
            'account_enabled': True,
            'usage_location': body.get('usageLocation') or False,
            'department': department or False,
            'job_title': job_title or False,
            'last_sync': fields.Datetime.now(),
        })
        self._log('create_user', 'success', _("%s created in Microsoft 365.", upn))
        return user, password

    def _request_licenses(self, user, skus, reason=False):
        """One license request per license, submitted; an invalid one only fails itself.

        :return: list of (sku, request or False, message)
        """
        self.ensure_one()
        Request = self.env['m365.license.request']
        results = []
        for sku in skus:
            try:
                with self.env.cr.savepoint():
                    request = Request.create({
                        'tenant_id': self.id, 'user_id': user.id, 'sku_id': sku.id,
                        'request_type': 'assign', 'reason': reason or False,
                    })
                    request.action_submit()
            except UserError as exc:
                results.append((sku, False, str(exc)))
                continue
            results.append((sku, request, request.error_message or False))
        return results

    def action_create_user(self):
        self._check_admin()
        self.ensure_one()
        self._check_operations_allowed()
        try:
            self._verified_domains()
        except M365GraphError as exc:
            raise UserError(_("The domains of the tenant cannot be read: %s", exc)) from None
        return {
            'type': 'ir.actions.act_window',
            'name': _("Create User"),
            'res_model': 'm365.user.create.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_tenant_id': self.id},
        }


class M365UserCreateWizard(models.TransientModel):
    _name = 'm365.user.create.wizard'
    _description = 'Create a Microsoft 365 User'

    tenant_id = fields.Many2one('m365.tenant', string="Tenant", required=True,
                                domain="['|', ('tenant_type', '=', 'existing'), "
                                       "('provision_state', '=', 'done')]")
    given_name = fields.Char(string="First Name")
    surname = fields.Char(string="Last Name")
    full_name = fields.Char(string="Name in Microsoft 365", required=True,
                            help="Display name of the user in Microsoft 365.")
    user_name = fields.Char(string="User Name", required=True,
                            help="Part before the @ of the user principal name.")
    domain_id = fields.Many2one('m365.tenant.domain', string="Domain", required=True,
                                domain="[('tenant_id', '=', tenant_id), "
                                       "('is_verified', '=', True)]")
    usage_location_id = fields.Many2one('res.country', string="Usage Location",
                                        help="Required to assign a license.")
    department = fields.Char()
    job_title = fields.Char()
    sku_ids = fields.Many2many('m365.sku', string="Licenses",
                               domain="[('tenant_id', '=', tenant_id)]",
                               help="One license request per license, with the usual "
                                    "approval.")
    reason = fields.Text(string="Business Justification")
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')
    user_id = fields.Many2one('m365.license.user', string="User", readonly=True)
    password = fields.Char(string="Temporary Password", readonly=True, groups=ADMIN_GROUP)
    result = fields.Text(readonly=True)
    error = fields.Text(readonly=True)

    @api.onchange('tenant_id')
    def _onchange_tenant_id(self):
        tenant = self.tenant_id
        if tenant and not tenant.domain_ids:
            try:
                tenant._sync_domains()
            except M365GraphError:
                pass  # no domain to choose: the tenant cannot be read
        domains = tenant.domain_ids.filtered('is_verified')
        self.domain_id = domains.filtered('is_default')[:1] or domains[:1]
        code = tenant._default_usage_location() if tenant else False
        self.usage_location_id = (self.env['res.country'].search([('code', '=', code)], limit=1)
                                  if code else False)
        self.sku_ids = self.sku_ids.filtered(lambda s: s.tenant_id == tenant)

    @api.onchange('given_name', 'surname')
    def _onchange_names(self):
        names = [n.strip() for n in (self.given_name, self.surname) if n and n.strip()]
        if names:
            self.full_name = " ".join(names)
            self.user_name = NICKNAME_STRIP_RE.sub('', ".".join(names).lower())

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Create User"),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_create(self):
        self.ensure_one()
        self.env['m365.tenant']._check_admin()
        if self.state == 'done':
            return self._reopen()
        tenant = self.tenant_id
        tenant._check_operations_allowed()
        upn = "%s@%s" % ((self.user_name or '').strip(), self.domain_id.name)
        usage = self.usage_location_id.code
        errors = tenant._check_new_user(upn, (self.full_name or '').strip(), usage)
        if self.sku_ids and not usage:
            errors.append(_("A usage location is required to assign a license."))
        if errors:
            raise ValidationError("\n".join(errors))
        try:
            user, password = tenant._create_user(
                upn, self.full_name.strip(), given_name=self.given_name,
                surname=self.surname, usage_location=usage, department=self.department,
                job_title=self.job_title)
        except M365GraphError as exc:
            # Shown in the wizard, not raised: the audit log line is kept
            self.error = _("Microsoft 365 did not create the user: %s", exc)
            return self._reopen()
        lines = [_("%s is created. Give the temporary password to the user: it must be "
                   "changed at the first sign-in.", upn)]
        for sku, request, message in tenant._request_licenses(user, self.sku_ids, self.reason):
            if request:
                state = dict(request._fields['state'].selection)[request.state]
                lines.append("%s: %s (%s)%s" % (sku.name, state, request.name,
                                                " - %s" % message if message else ''))
            else:
                lines.append(_("%(sku)s: not requested: %(err)s", sku=sku.name, err=message))
        self.write({'state': 'done', 'user_id': user.id, 'password': password,
                    'result': "\n".join(lines), 'error': False})
        return self._reopen()

    def action_done(self):
        """Forget the password and close."""
        self.sudo().write({'password': False})
        return {'type': 'ir.actions.act_window_close'}


# ----------------------------------------------------------------------
# Bulk creation from a file
# ----------------------------------------------------------------------
# (key, header, required, other accepted headers), matched case insensitively
USER_COLUMNS = [
    ('tenant', 'Tenant', True, ('tenant name', 'primary domain', 'directory (tenant) id')),
    ('upn', 'User Principal Name', True, ('upn', 'user name', 'username', 'email')),
    ('display_name', 'Display Name', True, ('name', 'full name')),
    ('given_name', 'First Name', False, ('given name', 'firstname')),
    ('surname', 'Last Name', False, ('surname', 'lastname')),
    ('usage_location', 'Usage Location', False, ('country', 'usage location code')),
    ('licenses', 'Licenses', False, ('license', 'licence', 'licences', 'sku', 'skus')),
    ('department', 'Department', False, ()),
    ('job_title', 'Job Title', False, ('title', 'job position')),
    ('reason', 'Business Justification', False, ('justification', 'reason')),
]
USER_TITLES = {key: title for key, title, _required, _aliases in USER_COLUMNS}
USER_SAMPLES = [
    ('contoso.com', 'adele.vance@contoso.com', 'Adele Vance', 'Adele', 'Vance', 'IN',
     'O365_BUSINESS_PREMIUM', 'Finance', 'Accountant', 'New joiner'),
    ('Contoso', 'alex.wilber@contoso.com', 'Alex Wilber', 'Alex', 'Wilber', 'IN',
     'Microsoft 365 Business Standard; Microsoft Teams Exploratory', 'Sales', '', ''),
]
LICENSE_SPLIT_RE = re.compile(r'[;,\n]')


class M365UserImportWizard(models.TransientModel):
    """Create the users listed in a file. Every row is verified first; nothing is created
    while a row is not valid. Each row is then created on its own: a row that Microsoft
    refuses does not stop the others."""
    _name = 'm365.user.import.wizard'
    _inherit = 'm365.file.import.mixin'
    _description = 'Bulk Create Microsoft 365 Users'

    file = fields.Binary(string="File", attachment=False)
    filename = fields.Char()
    template_file = fields.Binary(attachment=False)
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')
    line_ids = fields.One2many('m365.user.import.line', 'wizard_id', string="Rows")
    error_count = fields.Integer(compute='_compute_counts')
    done_count = fields.Integer(compute='_compute_counts')
    failed_count = fields.Integer(compute='_compute_counts')
    result_file = fields.Binary(string="Result File", attachment=False, readonly=True,
                                groups=ADMIN_GROUP)
    result_filename = fields.Char(default='m365_created_users.xlsx')

    @api.depends('line_ids.status')
    def _compute_counts(self):
        for wiz in self:
            statuses = wiz.line_ids.mapped('status')
            wiz.error_count = statuses.count('error')
            wiz.done_count = statuses.count('done')
            wiz.failed_count = statuses.count('failed')

    @api.onchange('file')
    def _onchange_file(self):
        self.line_ids = [Command.clear()]

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Bulk Create Users"),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': {'dialog_size': 'extra-large'},
        }

    def _map_header(self, header):
        lookup = {}
        for key, title, _required, aliases in USER_COLUMNS:
            for name in (key, title) + aliases:
                lookup[self._normalise(name)] = key
        mapping, unknown = {}, []
        for index, title in enumerate(header):
            if not title:
                continue
            key = lookup.get(self._normalise(title))
            if not key:
                unknown.append(title)
            elif key in mapping.values():
                raise UserError(_("The column \"%s\" appears more than once.", title))
            else:
                mapping[index] = key
        if unknown:
            raise UserError(_("Unknown column(s): %(unknown)s.\nAllowed columns: %(allowed)s.",
                              unknown=", ".join(unknown),
                              allowed=", ".join(USER_TITLES.values())))
        missing = [title for key, title, required, _aliases in USER_COLUMNS
                   if required and key not in mapping.values()]
        if missing:
            raise UserError(_("Missing required column(s): %s.", ", ".join(missing)))
        return mapping

    def _parse(self):
        """Read and verify every row. :return: list of row dicts"""
        self.ensure_one()
        rows = self._read_rows()
        if len(rows) < 2:
            raise UserError(_("The file contains no rows below the header."))
        mapping = self._map_header(rows[0])
        Tenant = self.env['m365.tenant']
        tenants = Tenant.search(Tenant._live_domain())
        tenant_index = defaultdict(lambda: Tenant)
        for tenant in tenants:
            for value in (tenant.name, tenant.domain, tenant.tenant_id):
                if value:
                    tenant_index[value.lower()] |= tenant
        verified, skus = {}, {}
        seen, requested = {}, defaultdict(int)
        result = []
        for sn, values in enumerate(rows[1:], start=1):
            texts = dict.fromkeys(USER_TITLES, '')
            texts.update({key: values[index] for index, key in mapping.items()
                          if index < len(values)})
            errors = [_("%s is required.", title) for key, title, required, _a in USER_COLUMNS
                      if required and not texts[key]]
            row = {'sn': sn, 'texts': texts, 'errors': errors, 'tenant': Tenant,
                   'skus': self.env['m365.sku']}
            result.append(row)
            if not texts['tenant']:
                continue
            matches = tenant_index.get(texts['tenant'].lower(), Tenant)
            if len(matches) != 1:
                errors.append(_("Tenant \"%s\" not found.", texts['tenant']) if not matches else
                              _("Tenant \"%s\" matches several tenants.", texts['tenant']))
                continue
            tenant = row['tenant'] = matches
            if not tenant.allow_license_operations:
                errors.append(_("License operations are disabled for tenant %s.", tenant.name))
                continue
            if tenant.state == 'draft':
                errors.append(_("Test the connection of tenant %s first.", tenant.name))
                continue
            if tenant.id not in verified:
                try:
                    verified[tenant.id] = tenant._verified_domains()
                except M365GraphError as exc:
                    verified[tenant.id] = set()
                    errors.append(_("The domains of tenant %(tenant)s cannot be read: %(err)s",
                                    tenant=tenant.name, err=exc))
                    continue
                skus[tenant.id] = self.env['m365.sku'].search([('tenant_id', '=', tenant.id)])
            upn = texts['upn'].strip()
            usage = texts['usage_location'].strip().upper() or tenant._default_usage_location()
            if len(usage or '') > 2:  # a country name
                country = self.env['res.country'].search([('name', '=ilike', usage)], limit=1)
                usage = country.code or usage
            row['usage'] = usage or False
            errors += tenant._check_new_user(upn, texts['display_name'].strip(), usage,
                                             verified=verified[tenant.id])
            key = (tenant.id, upn.lower())
            if key in seen:
                errors.append(_("Duplicate row, same as SN. %s.", seen[key]))
            seen.setdefault(key, sn)
            for name in filter(None, (n.strip() for n in LICENSE_SPLIT_RE.split(
                    texts['licenses']))):
                sku = skus[tenant.id].filtered(lambda s: name.lower() in (
                    (s.sku_part_number or '').lower(), (s.name or '').lower(),
                    (s.sku_id or '').lower()))[:1]
                if not sku:
                    errors.append(_("License \"%(name)s\" not found in tenant %(tenant)s.",
                                    name=name, tenant=tenant.name))
                    continue
                row['skus'] |= sku
                requested[sku] += 1
                if requested[sku] > sku.available_units:
                    errors.append(_("Not enough %(sku)s licenses in tenant %(tenant)s: "
                                    "%(count)s available.", sku=sku.name, tenant=tenant.name,
                                    count=sku.available_units))
            if row['skus'] and not usage:
                errors.append(_("A usage location is required to assign a license."))
        return result

    @staticmethod
    def _line_vals(row):
        return {
            'sn': row['sn'],
            'tenant_ref': row['texts']['tenant'],
            'upn': row['texts']['upn'],
            'full_name': row['texts']['display_name'],
            'licenses': row['texts']['licenses'] or False,
            'tenant_id': row['tenant'].id,
            'status': 'error' if row['errors'] else 'valid',
            'message': "\n".join(row['errors']) or False,
        }

    def action_verify(self):
        self.ensure_one()
        self.line_ids = [Command.clear()] + [Command.create(self._line_vals(row))
                                             for row in self._parse()]
        return self._reopen()

    def action_execute(self):
        self.ensure_one()
        self.env['m365.tenant']._check_admin()
        if self.state == 'done':
            return self._reopen()
        rows = self._parse()
        invalid = [row for row in rows if row['errors']]
        if invalid:
            raise ValidationError(_(
                "No user has been created. Correct these rows and verify the file again:\n%s",
                "\n".join("SN. %s: %s" % (row['sn'], " ".join(row['errors']))
                          for row in invalid)))
        lines, results = [], []
        for row in rows:
            texts, tenant = row['texts'], row['tenant']
            vals = self._line_vals(row)
            password = ''
            try:
                user, password = tenant._create_user(
                    texts['upn'].strip(), texts['display_name'].strip(),
                    given_name=texts['given_name'] or None, surname=texts['surname'] or None,
                    usage_location=row['usage'] or None,
                    department=texts['department'] or None,
                    job_title=texts['job_title'] or None)
            except M365GraphError as exc:
                vals.update(status='failed', message=str(exc))
            else:
                notes = []
                for sku, request, message in tenant._request_licenses(
                        user, row['skus'], texts['reason'] or False):
                    notes.append("%s: %s" % (sku.name, request.name if request else message))
                vals.update(status='done', user_id=user.id, message="\n".join(notes) or False)
            # The user exists in Microsoft 365: keep its Odoo record whatever comes next
            tenant._commit()
            lines.append(Command.create(vals))
            results.append((texts['upn'], texts['display_name'], password, vals['status'],
                            vals['message'] or ''))
        self.write({'state': 'done', 'line_ids': [Command.clear()] + lines})
        self.sudo().result_file = self._result_workbook(results)
        return self._reopen()

    def _result_workbook(self, results):
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = _("Users")
        sheet.append([_("User Principal Name"), _("Display Name"), _("Temporary Password"),
                      _("Status"), _("Message")])
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in results:
            sheet.append(list(row))
        for letter, width in zip('ABCDE', (36, 28, 22, 12, 60)):
            sheet.column_dimensions[letter].width = width
        out = io.BytesIO()
        book.save(out)
        return base64.b64encode(out.getvalue())

    def action_download_result(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s/%s/result_file/%s?download=true' % (
                self._name, self.id, self.result_filename),
            'target': 'download',
        }

    def action_done(self):
        """Forget the passwords and close."""
        self.sudo().write({'result_file': False})
        return {'type': 'ir.actions.act_window_close'}

    def action_download_template(self):
        self.ensure_one()
        hints = {
            'tenant': _("Required. Tenant name, primary domain or directory (tenant) ID."),
            'upn': _("Required. user@domain, on a verified domain of the tenant."),
            'display_name': _("Required."),
            'usage_location': _("Two letter country code (e.g. IN). Default: the usage "
                                "location of the configuration profile. Required with "
                                "licenses."),
            'licenses': _("Optional. SKU or product names, separated by ; - one license "
                          "request each."),
            'reason': _("Optional. Stored on the license requests."),
        }
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = _("Users")
        required_fill = PatternFill('solid', fgColor='FFF2CC')
        for col, (key, title, required, _aliases) in enumerate(USER_COLUMNS, start=1):
            cell = sheet.cell(row=1, column=col, value=title)
            cell.font = Font(bold=True)
            if required:
                cell.fill = required_fill
            if key in hints:
                cell.comment = Comment(hints[key], 'Odoo')
            sheet.column_dimensions[cell.column_letter].width = 26
        for sample in USER_SAMPLES:
            sheet.append(sample)
        sheet.freeze_panes = 'A2'
        return self._download_workbook(book, 'm365_create_users_template.xlsx')


class M365UserImportLine(models.TransientModel):
    _name = 'm365.user.import.line'
    _description = 'Bulk User Creation Row'
    _order = 'sn, id'

    wizard_id = fields.Many2one('m365.user.import.wizard', required=True, ondelete='cascade')
    sn = fields.Integer(string="SN.")
    tenant_ref = fields.Char(string="Tenant")
    upn = fields.Char(string="User Principal Name")
    full_name = fields.Char(string="Name in Microsoft 365")
    licenses = fields.Char(string="Licenses")
    tenant_id = fields.Many2one('m365.tenant', string="Matched Tenant")
    user_id = fields.Many2one('m365.license.user', string="Created User")
    status = fields.Selection([
        ('valid', 'Valid'),
        ('error', 'Error'),
        ('done', 'Created'),
        ('failed', 'Failed'),
    ], string="Status")
    message = fields.Text(string="Message")
