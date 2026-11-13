# -*- coding: utf-8 -*-
import base64
import re

import openpyxl
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError

from .m365_tenant_creation import INITIAL_DOMAIN

GUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
DOMAIN_RE = re.compile(r'^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$',
                       re.I)
TRUE_VALUES = ('1', 'true', 'yes', 'y')
CONNECTION_VALUES = {
    'own_app': 'own_app', 'own app': 'own_app', 'own': 'own_app',
    'own app registration': 'own_app', 'app registration': 'own_app',
    'shared_app': 'shared_app', 'shared app': 'shared_app', 'shared': 'shared_app',
    'partner app': 'shared_app',
}
FALSE_VALUES = ('0', 'false', 'no', 'n')
TEMPLATE_NAME = 'm365_tenant_import_template.xlsx'

# Importable m365.tenant fields: (field name, value type, required). The column header
# is the field label (or the technical name); blank optional cells keep the default.
COLUMNS = [
    ('name', 'text', True),
    ('domain', 'domain', True),
    ('tenant_id', 'guid', True),
    # Required for the own app registration (default), not for the shared app
    ('client_id', 'guid', False),
    ('client_secret', 'text', False),
    ('connection_mode', 'connection', False),
    ('authority_url', 'url', False),
    ('graph_url', 'url', False),
    ('graph_scope', 'url', False),
    ('allow_license_operations', 'bool', False),
    ('auto_approve_admins', 'bool', False),
    ('sync_enabled', 'bool', False),
    ('sync_users', 'bool', False),
    ('company_id', 'company', False),
    ('low_availability_percent', 'percent', False),
    ('low_availability_units', 'units', False),
    # Provisioning of existing tenants (licenses through Ingram Micro, users, domains)
    ('requested_subscriptions', 'text', False),
    ('custom_domains', 'domains', False),
    ('mca_first_name', 'text', False),
    ('mca_last_name', 'text', False),
    ('mca_email', 'text', False),
    ('mca_phone', 'text', False),
    ('mca_date', 'date', False),
    ('ingram_relationship', 'bool', False),
]
# Must be unique (case insensitive) within the file and among the existing tenants
UNIQUE_FIELDS = ('tenant_id', 'domain', 'name')
# Columns of the downloadable template; the other columns may be added to the file. The
# operation and synchronisation switches are left out: they are enabled by default and
# changed on the tenant.
TEMPLATE_FIELDS = ('name', 'domain', 'tenant_id', 'client_id', 'client_secret',
                   'authority_url', 'graph_url', 'graph_scope')
# Example rows of the template (valid, importable test data with placeholder IDs)
SAMPLE_ROWS = [
    {'name': 'Contoso Ltd', 'domain': 'contoso.com',
     'tenant_id': 'aaaaaaaa-0000-0000-0000-000000000001',
     'client_id': 'bbbbbbbb-0000-0000-0000-000000000001',
     'client_secret': 'replace-with-client-secret'},
    # Sovereign cloud (US Government) endpoints
    {'name': 'Northwind Traders', 'domain': 'northwindtraders.com',
     'tenant_id': 'aaaaaaaa-0000-0000-0000-000000000002',
     'client_id': 'bbbbbbbb-0000-0000-0000-000000000002',
     'client_secret': 'replace-with-client-secret',
     'authority_url': 'https://login.microsoftonline.us',
     'graph_url': 'https://graph.microsoft.us/v1.0',
     'graph_scope': 'https://graph.microsoft.us/.default'},
    # Required columns only: blank secret (added later), default URLs
    {'name': 'Adventure Works', 'domain': 'adventure-works.com',
     'tenant_id': 'aaaaaaaa-0000-0000-0000-000000000003',
     'client_id': 'bbbbbbbb-0000-0000-0000-000000000003'},
]

# Columns of the new tenants to create (Customers.csv of the provisioning script): (field
# name, value type, required). The header is the Customers.csv column name below, the
# field label or the technical name.
NEW_COLUMNS = [
    ('name', 'text', True),
    ('domain', 'initial_domain', True),
    ('contact_first_name', 'text', True),
    ('contact_last_name', 'text', True),
    ('contact_email', 'text', True),
    ('contact_phone', 'text', True),
    ('address_line1', 'text', True),
    ('address_line2', 'text', False),
    ('city', 'text', True),
    ('address_state', 'text', False),
    ('postal_code', 'text', True),
    ('country_id', 'country', True),
    # PARTNER CENTER (disabled): billing profile of the Partner Center customer
    # ('culture', 'text', False),
    # ('language', 'text', False),
    # ('organization_registration_number', 'text', False),
    ('requested_subscriptions', 'text', False),
    ('custom_domains', 'domains', False),
    ('mca_first_name', 'text', False),
    ('mca_last_name', 'text', False),
    ('mca_email', 'text', False),
    ('mca_phone', 'text', False),
    ('mca_date', 'date', False),
    ('ingram_relationship', 'bool', False),
]
NEW_HEADERS = {
    'name': 'CompanyName', 'domain': 'Domain', 'contact_first_name': 'FirstName',
    'contact_last_name': 'LastName', 'contact_email': 'Email', 'contact_phone': 'PhoneNumber',
    'address_line1': 'AddressLine1', 'address_line2': 'AddressLine2', 'city': 'City',
    'address_state': 'State', 'postal_code': 'PostalCode', 'country_id': 'Country',
    'requested_subscriptions': 'Subscriptions',
    # PARTNER CENTER (disabled)
    # 'culture': 'Culture', 'language': 'Language',
    # 'organization_registration_number': 'OrganizationRegistrationNumber',
    'custom_domains': 'CustomDomains',
    'mca_first_name': 'AgreementFirstName', 'mca_last_name': 'AgreementLastName',
    'mca_email': 'AgreementEmail', 'mca_phone': 'AgreementPhone', 'mca_date': 'AgreementDate',
    'ingram_relationship': 'IngramRelationship',
}
# Other accepted headers of the new tenant columns (normalised)
NEW_ALIASES = {'initialdomain': 'domain', 'initial domain': 'domain'}
# Columns of the creation through Partner Center (disabled): accepted, not read
NEW_IGNORED = {'culture', 'language', 'organizationregistrationnumber',
               'organization registration number'}
NEW_TEMPLATE_NAME = 'm365_customers_template.xlsx'
NEW_SAMPLE = {
    'name': 'Contoso Ltd', 'domain': 'contosoltd', 'contact_first_name': 'Adele',
    'contact_last_name': 'Vance', 'contact_email': 'adele.vance@contoso.com',
    'contact_phone': '+91 80 4000 1000', 'address_line1': '1 MG Road', 'address_line2': '',
    'city': 'Bengaluru', 'address_state': 'KA', 'postal_code': '560001', 'country_id': 'IN',
    'requested_subscriptions': 'Microsoft 365 Business Basic (no Teams) = 10',
    'custom_domains': 'contoso-example.com', 'mca_first_name': 'Adele',
    'mca_last_name': 'Vance', 'mca_email': 'adele.vance@contoso.com', 'mca_phone': '',
    'mca_date': '', 'ingram_relationship': '',
}
# Users sheet of the customer file: (key, header, required, other accepted headers)
USER_SHEET = 'Users'
USER_COLUMNS = [
    ('company', 'CompanyName', True, ('company', 'company name', 'tenant', 'tenant name',
                                      'customer', 'domain')),
    ('user_name', 'UserName', True, ('user name', 'upn', 'user principal name', 'alias',
                                     'email')),
    ('display_name', 'DisplayName', False, ('display name', 'name', 'full name')),
    ('given_name', 'FirstName', False, ('first name', 'given name')),
    ('surname', 'LastName', False, ('last name', 'surname')),
    ('usage_location', 'UsageLocation', False, ('usage location', 'country')),
    ('licenses', 'Licenses', False, ('license', 'licence', 'licences', 'sku', 'skus')),
    ('department', 'Department', False, ()),
    ('job_title', 'JobTitle', False, ('job title', 'title')),
]
USER_SAMPLES = [
    ('Contoso Ltd', 'adele.vance', 'Adele Vance', 'Adele', 'Vance', 'IN',
     'Microsoft 365 Business Basic (no Teams)', 'Finance', 'Accountant'),
    ('Contoso Ltd', 'alex.wilber', 'Alex Wilber', 'Alex', 'Wilber', 'IN', 'default', 'Sales',
     ''),
]
UPN_RE = re.compile(r'^[A-Za-z0-9._%+\-\']{1,64}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}$')
ALIAS_RE = re.compile(r'^[A-Za-z0-9._\-\']{1,64}$')


class M365TenantImportWizard(models.TransientModel):
    _name = 'm365.tenant.import.wizard'
    _inherit = 'm365.file.import.mixin'
    _description = 'Import Microsoft 365 Tenants'

    tenant_type = fields.Selection([
        ('existing', 'Existing'),
        ('new', 'New'),
    ], string="Type", default='existing', required=True,
        help="Existing: tenants to connect, with their app registration.\n"
             "New: tenants to create through Ingram Micro (Customers.csv).")
    partner_account_id = fields.Many2one(
        'm365.partner.account', string="Shared App",
        default=lambda self: self.env['m365.tenant']._default_partner_account(),
        help="The new tenants are created as customers of this account.")
    file = fields.Binary(string="File", attachment=False,
                         help="CSV or Excel (.xlsx) file with one tenant per row.")
    filename = fields.Char()
    users_file = fields.Binary(
        string="Users File", attachment=False,
        help="Optional CSV or Excel file of the users to create in the tenants of the file "
             "(columns of the Users sheet of the template: CompanyName, UserName, "
             "DisplayName, ...). Not the file of the tenants: an Excel file of the tenants "
             "can contain the users in a sheet named Users instead.")
    users_filename = fields.Char()
    template_file = fields.Binary(attachment=False)
    line_ids = fields.One2many('m365.tenant.import.line', 'wizard_id', string="Rows")
    line_count = fields.Integer(compute='_compute_counts')
    error_count = fields.Integer(compute='_compute_counts')
    user_count = fields.Integer(string="Users", readonly=True)
    # Options of the new tenants
    auto_provision = fields.Boolean(
        string="Run Automatically",
        help="Odoo runs every provisioning step of the new tenants by itself (scheduled "
             "action), from the creation to the license verification, and stops only "
             "where a person is needed.")
    approve_orders = fields.Boolean(
        string="Approve the Subscription Orders",
        help="The Subscriptions of the file are ordered from Ingram Micro without another "
             "approval: you approve them by importing the file. Recorded in the audit log.")
    mca_confirmed = fields.Boolean(
        string="The Customers Accepted the Microsoft Customer Agreement",
        help="You confirm that each customer of the file accepted the Microsoft Customer "
             "Agreement (Agreement columns, else the contact). Odoo sends it to Ingram Micro "
             "with the orders.")
    ingram_linked = fields.Boolean(
        string="The Customers Accepted the Reseller Relationship of Ingram Micro",
        help="Each customer of the file accepted the reseller relationship of Ingram Micro "
             "(indirect provider) in its Microsoft 365 admin center: Ingram Micro can order "
             "for it. Column IngramRelationship: per customer.")
    make_custom_default = fields.Boolean(
        string="Make the Custom Domain the Default", default=True,
        help="Once verified, the first custom domain of each customer becomes the default "
             "domain of its tenant.")
    pick_free_domain = fields.Boolean(
        string="Pick a Free Initial Domain", default=True,
        help="When a .onmicrosoft.com name is already used, Check takes the same name "
             "followed by 1 to 9.")

    @api.depends('line_ids.status')
    def _compute_counts(self):
        for wiz in self:
            wiz.line_count = len(wiz.line_ids)
            wiz.error_count = len(wiz.line_ids.filtered(lambda l: l.status == 'error'))

    @api.onchange('file', 'users_file', 'tenant_type', 'partner_account_id')
    def _onchange_file(self):
        # A new file must be validated again before tenants can be created
        self.line_ids = [Command.clear()]

    # ------------------------------------------------------------------
    # File reading
    # ------------------------------------------------------------------
    def _columns(self):
        """(field name, value type, required) of the columns of the selected type."""
        return NEW_COLUMNS if self.tenant_type == 'new' else COLUMNS

    def _column_title(self, fname):
        if self.tenant_type == 'new':
            return NEW_HEADERS[fname]
        return self.env['m365.tenant']._fields[fname].string

    def _map_header(self, header):
        """Map column positions to m365.tenant field names."""
        Tenant = self.env['m365.tenant']
        columns = self._columns()
        lookup = {}
        for fname, _kind, _required in columns:
            lookup[fname] = fname
            lookup[self._normalise(Tenant._fields[fname].string)] = fname
            lookup[self._normalise(self._column_title(fname))] = fname
            if fname in NEW_HEADERS:  # customer file titles, e.g. Subscriptions
                lookup[self._normalise(NEW_HEADERS[fname])] = fname
        if self.tenant_type == 'new':
            lookup.update(NEW_ALIASES)
        mapping, unknown = {}, []
        for index, title in enumerate(header):
            if not title:
                continue
            fname = lookup.get(self._normalise(title))
            if not fname and self.tenant_type == 'new' and \
                    self._normalise(title) in NEW_IGNORED:
                continue
            if not fname:
                unknown.append(title)
            elif fname in mapping.values():
                raise UserError(_("The column \"%s\" appears more than once.", title))
            else:
                mapping[index] = fname
        if unknown:
            raise UserError(_(
                "Unknown column(s): %(unknown)s.\nAllowed columns: %(allowed)s.",
                unknown=", ".join(unknown),
                allowed=", ".join(self._column_title(f) for f, _k, _r in columns)))
        missing = [self._column_title(f) for f, _k, required in columns
                   if required and f not in mapping.values()]
        if (self.tenant_type == 'existing' and 'client_id' not in mapping.values()
                and 'connection_mode' not in mapping.values()):
            # Own app registration unless the file says Shared App
            missing.append(self._column_title('client_id'))
        if missing:
            raise UserError(_("Missing required column(s): %s.", ", ".join(missing)))
        return mapping

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def _convert(self, kind, text, lookups):
        """Return the field value for a cell, raise ValueError when it is not valid.

        :param lookups: records by lower case name: ``{'company': {...}, 'country': {...}}``
        """
        if kind == 'guid':
            if not GUID_RE.match(text):
                raise ValueError(_("\"%s\" is not a valid ID (expected "
                                   "00000000-0000-0000-0000-000000000000).", text))
            return text.lower()
        if kind == 'domain':
            if not DOMAIN_RE.match(text):
                raise ValueError(_("\"%s\" is not a valid domain name.", text))
            return text.lower()
        if kind == 'url':
            if not text.lower().startswith('https://'):
                raise ValueError(_("\"%s\" must start with https://.", text))
            return text
        if kind == 'bool':
            if text.lower() in TRUE_VALUES:
                return True
            if text.lower() in FALSE_VALUES:
                return False
            raise ValueError(_("\"%s\" is not a Yes/No value.", text))
        if kind in ('percent', 'units'):
            try:
                number = float(text.replace(',', '.'))
            except ValueError:
                raise ValueError(_("\"%s\" is not a number.", text)) from None
            if kind == 'percent':
                if not 0 <= number <= 100:
                    raise ValueError(_("%s must be between 0 and 100.", text))
                return number
            if number < 0 or not number.is_integer():
                raise ValueError(_("%s must be a whole number of 0 or more.", text))
            return int(number)
        if kind == 'connection':
            mode = CONNECTION_VALUES.get(' '.join(text.lower().split()))
            if not mode:
                raise ValueError(_("\"%s\" is not a connection: Own App or Shared App.", text))
            return mode
        if kind == 'company':
            company = lookups['company'].get(text.lower())
            if not company:
                raise ValueError(_("Company \"%s\" not found in your allowed companies.", text))
            return company.id
        if kind == 'country':
            country = lookups['country'].get(text.lower())
            if not country:
                raise ValueError(_("Country \"%s\" not found: use the ISO code, e.g. IN.", text))
            return country.id
        if kind == 'initial_domain':
            # Validated with the other data of the new tenant (_check_values)
            return self.env['m365.tenant']._normalise_domain(text)
        if kind == 'domains':
            names = self.env['m365.tenant']._split_domains(text)
            bad = [n for n in names if not DOMAIN_RE.match(n) or n.endswith(INITIAL_DOMAIN)]
            if bad:
                raise ValueError(_("\"%s\" is not a valid custom domain, e.g. contoso.com.",
                                   ", ".join(bad)))
            return '; '.join(dict.fromkeys(names))
        if kind == 'date':
            try:
                return fields.Date.to_date(text[:10])
            except ValueError:
                raise ValueError(_("\"%s\" is not a date (YYYY-MM-DD).", text)) from None
        return text

    def _parse(self):
        """Read and validate the uploaded file.

        :return: list of dicts with the serial number (1 = first row below the
                 header), the raw cell texts, the m365.tenant values and the error
                 messages of each data row
        """
        rows = self._read_rows()
        if len(rows) < 2:
            raise UserError(_("The file contains no tenant rows below the header."))
        mapping = self._map_header(rows[0])
        Tenant = self.env['m365.tenant']
        columns = self._columns()
        kinds = {fname: kind for fname, kind, _required in columns}
        required = {fname for fname, _kind, req in columns if req}
        lookups = {'company': {c.name.lower(): c for c in self.env.user.company_ids}}
        defaults = self._option_defaults()
        if self.tenant_type == 'new':
            account = self.partner_account_id
            # PARTNER CENTER (disabled): the new tenants are created by Ingram Micro
            # if not account:
            #     raise UserError(_("Select the Partner Center account of the new tenants."))
            lookups['country'] = {}
            for country in self.env['res.country'].search([]):
                lookups['country'][country.code.lower()] = country
                lookups['country'][country.name.lower()] = country
            defaults.update(tenant_type='new', partner_account_id=account.id,
                            # PARTNER CENTER (disabled)
                            # culture=account.default_culture,
                            # language=account.default_language,
                            # enable_gdap=account.enable_gdap,
                            auto_initial_domain=self.pick_free_domain)
        cache = {}

        def shared_account():
            if 'account' not in cache:
                cache['account'] = self.env['m365.partner.account'].search(
                    [('graph_client_id', '!=', False)], limit=1)
            return cache['account']

        # The unique constraint is global: also check archived / other company tenants
        tenants = Tenant.sudo().with_context(active_test=False).search([])
        existing = {fname: {(value or '').lower() for value in tenants.mapped(fname)}
                    for fname in UNIQUE_FIELDS}
        seen = {fname: {} for fname in UNIQUE_FIELDS}
        customs = self._custom_domains_taken(tenants)
        seen_customs = {}
        result = []
        for sn, values in enumerate(rows[1:], start=1):
            raw, vals, errors, invalid = {}, dict(defaults), [], set()
            for index, fname in mapping.items():
                text = raw[fname] = values[index] if index < len(values) else ''
                label = Tenant._fields[fname].string
                if not text:
                    if fname in required:
                        errors.append(_("%s is required.", label))
                    continue
                try:
                    vals[fname] = self._convert(kinds[fname], text, lookups)
                except ValueError as exc:
                    invalid.add(label)
                    errors.append(_("%(field)s: %(error)s", field=label, error=exc))
            if self.tenant_type == 'existing':
                if vals.get('connection_mode') == 'shared_app':
                    # Connected later with Grant Consent: no app registration of its own
                    account = shared_account()
                    if account:
                        vals['partner_account_id'] = account.id
                    else:
                        errors.append(_("Connection Shared App: add the shared app first "
                                        "(Configuration > Tenants > Shared App)."))
                elif not vals.get('client_id') and \
                        Tenant._fields['client_id'].string not in invalid:
                    errors.append(_("%s is required.", Tenant._fields['client_id'].string))
            errors += self._apply_agreement(vals)
            if self.tenant_type == 'existing':
                errors += self._check_custom_domains(vals, customs, seen_customs, sn)
            if self.tenant_type == 'new':
                errors += self._split_initial_domain(vals, existing, seen, customs,
                                                     seen_customs, sn)
                # The checks of the Check button, without repeating the errors above
                skip = set(errors) | {_("%s is required.", label) for label in invalid}
                errors += [error for error in Tenant.new(vals)._check_values()
                           if error not in skip]
            for fname in UNIQUE_FIELDS:
                key = (vals.get(fname) or '').lower()
                if not key:
                    continue
                label = Tenant._fields[fname].string
                if key in existing[fname]:
                    errors.append(_("%(field)s \"%(value)s\" already exists in Odoo.",
                                    field=label, value=vals[fname]))
                elif key in seen[fname]:
                    errors.append(_("Duplicate %(field)s, same as SN. %(sn)s.",
                                    field=label, sn=seen[fname][key]))
                seen[fname].setdefault(key, sn)
            result.append({'sn': sn, 'raw': raw, 'vals': vals, 'errors': errors,
                           'users': []})
        self._parse_users(result)
        return result

    def _import_channel(self):
        """Where the licenses of the file are bought: Partner Center or Ingram Micro."""
        return 'ingram'
        # PARTNER CENTER (disabled): channel of the Partner Center account
        # if self.tenant_type == 'new':
        #     return 'ingram'  # new tenants are created by Ingram Micro with their first order
        # account = self.partner_account_id or self.env['m365.tenant']._default_partner_account()
        # return account.purchase_channel if account else 'ingram'

    def _option_defaults(self):
        """Values of the options of the wizard, for every tenant of the file."""
        today = fields.Date.context_today(self)
        vals = {'custom_domain_default': self.make_custom_default,
                'mca_customer_confirmed': self.mca_confirmed,
                'auto_provision': self.auto_provision,
                'prov_approve_orders': self.approve_orders,
                'prov_user_id': self.env.uid}
        if self.ingram_linked:
            vals.update(ingram_relationship=True, ingram_relationship_date=today)
        return vals

    def _apply_agreement(self, vals):
        """Through Ingram Micro, the acceptance of the Customer Agreement confirmed by the
        file is recorded on the tenant (sent with the orders); through Partner Center it is
        attested later with the partner user. :return: errors"""
        if vals.get('ingram_relationship') and not vals.get('ingram_relationship_date'):
            vals['ingram_relationship_date'] = fields.Date.context_today(self)
        if not self.mca_confirmed or self._import_channel() != 'ingram':
            return []
        email = vals.get('mca_email') or vals.get('contact_email')
        first = vals.get('mca_first_name') or vals.get('contact_first_name')
        last = vals.get('mca_last_name') or vals.get('contact_last_name')
        if not (email and (first or last)):
            return [_("Customer Agreement: AgreementFirstName or AgreementLastName and "
                      "AgreementEmail are required (who accepted it).")]
        vals.update(mca_accepted=True, mca_email=email, mca_first_name=first or False,
                    mca_last_name=last or False,
                    mca_phone=vals.get('mca_phone') or vals.get('contact_phone') or False,
                    mca_date=vals.get('mca_date') or fields.Date.context_today(self))
        return []

    # ------------------------------------------------------------------
    # Domains of the new tenants
    # ------------------------------------------------------------------
    def _custom_domains_taken(self, tenants):
        """Custom domains already known in Odoo: {domain: tenant name}."""
        taken = {}
        for tenant in tenants:
            for name in tenant._custom_domain_names():
                taken.setdefault(name, tenant.name)
        domains = self.env['m365.tenant.domain'].sudo().search([('is_initial', '=', False)])
        for domain in domains:
            taken.setdefault(domain.name, domain.tenant_id.name)
        return taken

    def _split_initial_domain(self, vals, existing, seen, customs, seen_customs, sn):
        """A custom domain in the Domain column (contoso.com) becomes the first custom
        domain; the initial domain is derived from it (contoso.onmicrosoft.com), with a
        number when the name is taken in Odoo or in the file.

        :return: errors of the custom domains
        """
        domain = vals.get('domain') or ''
        names = self.env['m365.tenant']._split_domains(vals.get('custom_domains'))
        if domain and '.' in domain and not domain.endswith(INITIAL_DOMAIN):
            if not DOMAIN_RE.match(domain):
                return []  # reported by the checks of the new tenant
            names = [domain] + [n for n in names if n != domain]
            label = re.sub(r'[^a-z0-9]', '', domain.split('.')[0])[:50] or 'tenant'
            taken = existing['domain'] | set(seen['domain'])
            candidate, number = label + INITIAL_DOMAIN, 1
            while candidate in taken:
                candidate, number = '%s%s%s' % (label, number, INITIAL_DOMAIN), number + 1
            vals['domain'] = candidate
            vals['auto_initial_domain'] = True
        if names:
            vals['custom_domains'] = '; '.join(names)
        return self._check_custom_domains(vals, customs, seen_customs, sn)

    def _check_custom_domains(self, vals, customs, seen_customs, sn):
        """Custom domains used by another tenant or another row. :return: errors"""
        names = self.env['m365.tenant']._split_domains(vals.get('custom_domains'))
        errors = []
        for name in names:
            if name in customs:
                errors.append(_("Custom domain %(domain)s is already used by tenant "
                                "%(tenant)s.", domain=name, tenant=customs[name]))
            elif name in seen_customs:
                errors.append(_("Custom domain %(domain)s is also in SN. %(sn)s.",
                                domain=name, sn=seen_customs[name]))
            seen_customs.setdefault(name, sn)
        return errors

    # ------------------------------------------------------------------
    # Users of the new tenants
    # ------------------------------------------------------------------
    @staticmethod
    def _header_key(title):
        return re.sub(r'[\s_]+', '', str(title or '')).lower()

    def _read_user_rows(self):
        """Rows of the Users sheet of the customer file, else of the users file (its Users
        sheet, else its first sheet)."""
        self.ensure_one()
        rows = None
        if self.file and (self.filename or '').lower().endswith('.xlsx'):
            rows = self._read_xlsx_sheet(base64.b64decode(self.file), USER_SHEET)
        if rows is None and self.users_file:
            if self.users_file == self.file:
                raise UserError(_("The Users File is the file of the tenants. Upload the file "
                                  "of the users to create (columns of the Users sheet of the "
                                  "template), or remove it."))
            raw = base64.b64decode(self.users_file)
            if (self.users_filename or '').lower().endswith('.xlsx'):
                rows = self._read_xlsx_sheet(raw, USER_SHEET)
            if rows is None:
                rows = self._rows_of(raw, self.users_filename)
        if rows is None:
            return []
        return [values for values in ([self._cell_text(v) for v in row] for row in rows)
                if any(values)]

    def _parse_users(self, result):
        """Attach the users of the file to their tenant rows (``row['users']``). Their
        errors are added to the row of their tenant, or to a last row *Users*."""
        rows = self._read_user_rows()
        if not rows:
            return
        keys = {}
        for key, title, _required, aliases in USER_COLUMNS:
            for name in (key, title) + tuple(aliases):
                keys[self._header_key(name)] = key
        mapping, unknown = {}, []
        for index, title in enumerate(rows[0]):
            if not title:
                continue
            key = keys.get(self._header_key(title))
            if not key:
                unknown.append(title)
            elif key not in mapping.values():
                mapping[index] = key
        allowed = ", ".join(t for _k, t, _r, _a in USER_COLUMNS)
        Tenant = self.env['m365.tenant']
        tenant_titles = {self._header_key(title) for fname, _kind, _req in self._columns()
                         for title in (fname, Tenant._fields[fname].string,
                                       self._column_title(fname))}
        if unknown and any(self._header_key(title) in tenant_titles for title in unknown):
            # e.g. the template of the tenants uploaded as Users File
            raise UserError(_("The Users File contains tenants (columns %(columns)s), not "
                              "users. Upload the file of the users to create, with the "
                              "columns %(allowed)s (Users sheet of the template), or remove "
                              "it.", columns=", ".join(unknown), allowed=allowed))
        if unknown:
            raise UserError(_("Unknown column(s) in the users: %(unknown)s.\nAllowed "
                              "columns: %(allowed)s.", unknown=", ".join(unknown),
                              allowed=allowed))
        missing = [title for key, title, required, _a in USER_COLUMNS
                   if required and key not in mapping.values()]
        if missing:
            raise UserError(_("Missing required column(s) in the users: %s.",
                              ", ".join(missing)))
        if len(rows) < 2:
            return  # no user, e.g. the empty Users sheet of the template
        tenants = {}
        for row in result:
            vals = row['vals']
            names = [vals.get('name'), vals.get('domain'), row['raw'].get('domain')]
            names += self.env['m365.tenant']._split_domains(vals.get('custom_domains'))
            for name in filter(None, names):
                tenants.setdefault(name.strip().lower(), row)
        countries = {c.code.lower() for c in self.env['res.country'].search([])}
        orphans, seen = [], {}
        for sn, values in enumerate(rows[1:], start=1):
            cells = {key: values[index] if index < len(values) else ''
                     for index, key in mapping.items()}
            errors = []
            row = tenants.get((cells.get('company') or '').lower())
            if not row:
                orphans.append(_("Users SN. %(sn)s: customer \"%(name)s\" is not in the "
                                 "file.", sn=sn, name=cells.get('company') or ''))
                continue
            vals = row['vals']
            user_name = (cells.get('user_name') or '').strip()
            if '@' in user_name:
                domain = user_name.rsplit('@', 1)[1].lower()
                allowed = [vals.get('domain')] + self.env['m365.tenant']._split_domains(
                    vals.get('custom_domains'))
                if not UPN_RE.match(user_name):
                    errors.append(_("%s is not a valid user principal name.", user_name))
                elif domain not in allowed:
                    errors.append(_("The domain of %(upn)s is not a domain of the customer "
                                    "(%(domains)s).", upn=user_name,
                                    domains=", ".join(filter(None, allowed))))
            elif not ALIAS_RE.match(user_name):
                errors.append(_("\"%s\" is not a valid user name.", user_name))
            display = (cells.get('display_name') or ' '.join(filter(None, (
                cells.get('given_name'), cells.get('surname'))))).strip()
            if not display:
                errors.append(_("DisplayName (or FirstName and LastName) is required."))
            usage = (cells.get('usage_location') or '').strip().upper()
            if usage and usage.lower() not in countries:
                errors.append(_("%s is not a valid ISO country code.", usage))
            key = (row['sn'], user_name.lower())
            if key in seen:
                errors.append(_("Duplicate user, same as users SN. %s.", seen[key]))
            seen.setdefault(key, sn)
            if errors:
                row['errors'].append(_("Users SN. %(sn)s (%(user)s): %(errors)s", sn=sn,
                                       user=user_name or '-', errors=" ".join(errors)))
                continue
            row['users'].append({
                'sequence': sn, 'user_name': user_name, 'name': display,
                'given_name': cells.get('given_name') or False,
                'surname': cells.get('surname') or False,
                'usage_location': usage or False,
                'licenses': cells.get('licenses') or False,
                'department': cells.get('department') or False,
                'job_title': cells.get('job_title') or False,
            })
        if orphans:
            result.append({'sn': 0, 'raw': {'name': _("Users")}, 'vals': None,
                           'errors': orphans, 'users': []})

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------
    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Import Tenants"),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_validate(self):
        self.ensure_one()
        parsed = self._parse()
        self.line_ids = [Command.clear()] + [Command.create({
            'sn': row['sn'],
            'name': row['raw'].get('name'),
            'domain': row['raw'].get('domain'),
            'directory_id': row['raw'].get('tenant_id'),
            'client_id': row['raw'].get('client_id'),
            'email': row['raw'].get('contact_email'),
            'country': row['raw'].get('country_id'),
            'status': 'error' if row['errors'] else 'valid',
            'message': "\n".join(row['errors']) or False,
            'user_count': len(row['users']),
        }) for row in parsed]
        self.user_count = sum(len(row['users']) for row in parsed)
        return self._reopen()

    def _link_customers(self, tenants):
        """Each new tenant belongs to the Odoo customer of the same name (created with the
        address and contact of the row when there is none)."""
        Customer = self.env['customer.customer']
        for tenant in tenants.filtered(lambda t: not t.tenant_customer_id):
            customer = Customer.search([('name', '=ilike', tenant.name),
                                        ('parent_id', '=', False)], limit=1)
            if not customer:
                customer = Customer.create({
                    'name': tenant.name, 'email': tenant.contact_email or False,
                    'phone': tenant.contact_phone or False,
                    'street': tenant.address_line1 or False,
                    'street2': tenant.address_line2 or False, 'city': tenant.city or False,
                    'zip': tenant.postal_code or False,
                    'country_id': tenant.country_id.id or False,
                })
                contact = " ".join(filter(None, (tenant.contact_first_name,
                                                 tenant.contact_last_name)))
                if contact:
                    Customer.create({'name': contact, 'parent_id': customer.id,
                                     'email': tenant.contact_email or False,
                                     'phone': tenant.contact_phone or False})
            tenant.tenant_customer_id = customer

    def action_import(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Validate the file before creating the tenants."))
        # Parse again: the file or the existing tenants may have changed since validation
        parsed = self._parse()
        invalid = [row for row in parsed if row['errors']]
        if invalid:
            raise UserError(_(
                "No tenant has been created. Correct these rows and validate the file "
                "again:\n%s", "\n".join("SN. %s: %s" % (row['sn'], " ".join(row['errors']))
                                         for row in invalid)))
        if self.error_count:
            raise UserError(_("Validate the file again before creating the tenants."))
        rows = [row for row in parsed if row['vals'] is not None]
        tenants = self.env['m365.tenant'].create([row['vals'] for row in rows])
        if self.tenant_type == 'new':
            self._link_customers(tenants)
        self.env['m365.tenant.pending.user'].create([
            dict(user, tenant_id=tenant.id, requested_by=self.env.uid)
            for row, tenant in zip(rows, tenants) for user in row['users']])
        for tenant in tenants:
            tenant._message_log(body=_("Tenant created by bulk import from %s.",
                                       self.filename))
        self._log_import_options(tenants)
        # Do not keep client secrets in the wizard any longer than needed
        self.write({'file': False, 'users_file': False})
        action = self.env['ir.actions.act_window']._for_xml_id(
            'm365_license_management.action_m365_tenant')
        action.update(name=_("Imported Tenants"), domain=[('id', 'in', tenants.ids)],
                      target='main')
        if self.auto_provision:
            message = _("%(count)s tenant(s) and %(users)s user(s) added. Odoo provisions "
                        "them automatically: follow them in Provisioning.",
                        count=len(tenants), users=sum(len(row['users']) for row in rows))
        elif self.tenant_type == 'new':
            message = _("%s tenant(s) added. Select them and use Check, then Create in "
                        "Microsoft 365.", len(tenants))
        else:
            message = _("%s tenant(s) created. Test their connection to start "
                        "synchronising.", len(tenants))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Microsoft 365"),
                'message': message,
                'type': 'success',
                'next': action,
            },
        }

    def _template_hints(self):
        return {
            'text': _("Text"),
            'domain': _("Domain name, e.g. contoso.com"),
            'guid': _("GUID, e.g. 11111111-2222-3333-4444-555555555555"),
            'url': _("https:// address"),
        }

    def _log_import_options(self, tenants):
        """Audit log of what the administrator approved by importing the file."""
        options = []
        if self.mca_confirmed:
            options.append(_("the customers accepted the Microsoft Customer Agreement"))
        if self.ingram_linked:
            options.append(_("the customers accepted the reseller relationship of Ingram "
                             "Micro"))
        if self.approve_orders:
            options.append(_("their subscription orders are approved"))
        if self.auto_provision:
            options.append(_("they are provisioned automatically"))
        for tenant in tenants:
            tenant._log('provision_tenant', 'info', _(
                "Imported from %(file)s by %(user)s%(options)s.", file=self.filename,
                user=self.env.user.name,
                options=(": " + "; ".join(options)) if options else ''))
        if self.auto_provision:
            cron = self.env.ref('m365_license_management.ir_cron_m365_auto_provision',
                                raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()

    def _new_tenant_template(self):
        """Customers.csv columns, with one example customer, and the Users sheet."""
        Tenant = self.env['m365.tenant']
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = _("Customers")
        required_fill = PatternFill('solid', fgColor='FFF2CC')
        for col, (fname, _kind, required) in enumerate(NEW_COLUMNS, start=1):
            title = NEW_HEADERS[fname]
            cell = sheet.cell(row=1, column=col, value=title)
            cell.font = Font(bold=True)
            if required:
                cell.fill = required_fill
            hint = Tenant._fields[fname].help or Tenant._fields[fname].string
            cell.comment = Comment(_("Required. %s", hint) if required else hint, 'Odoo')
            sheet.column_dimensions[cell.column_letter].width = max(14, len(title) + 4)
        sheet.append([NEW_SAMPLE.get(fname, '') for fname, _kind, _required in NEW_COLUMNS])
        sheet.freeze_panes = 'A2'
        self._add_users_sheet(book, samples=True)
        return book

    def _add_users_sheet(self, book, samples):
        """Users sheet of a template: the users to create in the tenants of the first
        sheet, with example rows when ``samples``."""
        required_fill = PatternFill('solid', fgColor='FFF2CC')
        users = book.create_sheet(USER_SHEET)
        hints = {
            'company': (_("CompanyName (or a domain) of the customer in the first sheet.")
                        if self.tenant_type == 'new' else
                        _("Tenant Name (or Primary Domain) of the tenant in the first sheet.")),
            'user_name': _("Part before the @ (the first custom domain, else the "
                           ".onmicrosoft.com domain, is added), or the full user name."),
            'licenses': _("Licenses (separated by ;): name or SKU of the license, name of "
                          "the Ingram Micro plan, or \"default\" (default plan of the Ingram "
                          "Micro account)."),
            'usage_location': _("ISO country code; blank: the country of the customer."),
        }
        for col, (key, title, required, _aliases) in enumerate(USER_COLUMNS, start=1):
            cell = users.cell(row=1, column=col, value=title)
            cell.font = Font(bold=True)
            if required:
                cell.fill = required_fill
            if key in hints:
                cell.comment = Comment(hints[key], 'Odoo')
            users.column_dimensions[cell.column_letter].width = max(14, len(title) + 4)
        for sample in (USER_SAMPLES if samples else ()):
            users.append(list(sample))
        users.freeze_panes = 'A2'
        return users

    def action_download_template(self):
        self.ensure_one()
        if self.tenant_type == 'new':
            return self._download_workbook(self._new_tenant_template(), NEW_TEMPLATE_NAME)
        Tenant = self.env['m365.tenant']
        hints = self._template_hints()
        defaults = Tenant.default_get(list(TEMPLATE_FIELDS))
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = _("Tenants")
        required_fill = PatternFill('solid', fgColor='FFF2CC')
        columns = [column for column in COLUMNS if column[0] in TEMPLATE_FIELDS]
        for col, (fname, kind, required) in enumerate(columns, start=1):
            label = Tenant._fields[fname].string
            cell = sheet.cell(row=1, column=col, value=label)
            cell.font = Font(bold=True)
            hint = hints[kind]
            if required:
                cell.fill = required_fill
                hint = _("Required. %s", hint)
            elif fname in defaults:
                hint = _("%(hint)s. Blank = %(default)s", hint=hint, default=defaults[fname])
            else:
                hint = _("Optional. %s", hint)
            cell.comment = Comment(hint, 'Odoo')
            sheet.column_dimensions[cell.column_letter].width = max(16, len(label) + 4)
        for sample in SAMPLE_ROWS:
            sheet.append([sample.get(fname) for fname, _kind, _required in columns])
        sheet.freeze_panes = 'A2'
        # The users to create in these tenants (optional): no example, the example tenants
        # stay valid as they are
        self._add_users_sheet(book, samples=False)
        return self._download_workbook(book, TEMPLATE_NAME)


class M365TenantImportLine(models.TransientModel):
    _name = 'm365.tenant.import.line'
    _description = 'Microsoft 365 Tenant Import Row'
    _order = 'sn'

    wizard_id = fields.Many2one('m365.tenant.import.wizard', required=True,
                                ondelete='cascade')
    sn = fields.Integer(string="SN.")
    name = fields.Char(string="Tenant Name")
    domain = fields.Char(string="Primary Domain")
    directory_id = fields.Char(string="Directory (Tenant) ID")
    client_id = fields.Char(string="Application (Client) ID")
    email = fields.Char(string="Contact Email")
    country = fields.Char(string="Country")
    status = fields.Selection([('valid', 'Valid'), ('error', 'Error')], string="Status")
    message = fields.Text(string="Errors")
    user_count = fields.Integer(string="Users")
