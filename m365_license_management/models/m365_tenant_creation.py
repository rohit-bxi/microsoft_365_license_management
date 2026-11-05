# -*- coding: utf-8 -*-
"""Creation of new Microsoft 365 customer tenants through Microsoft Partner Center.

A tenant of type *New* is created in Microsoft 365 with the Partner Center REST API
(the API behind ``New-PartnerCustomer`` of the ``Bulk-M365Customers.ps1`` script), then
connected through Microsoft Graph like an existing tenant::

    To Check --Check--> Ready --Create in Microsoft 365--> Creating --> Created
                                                               +--error--> Failed --Retry--+

Once *Created*, the Directory (Tenant) ID comes from Microsoft: enter the client ID and
secret of an app consented in the new tenant and use *Test Connection*.
"""
import re
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config, email_normalize

from .m365_partner_account import M365PartnerCenterTimeout
from .m365_tenant import M365GraphError

ADMIN_GROUP = 'm365_license_management.group_m365_admin'
INITIAL_DOMAIN = '.onmicrosoft.com'
DOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.onmicrosoft\.com$')
CULTURE_RE = re.compile(r'^[a-z]{2}-[a-z]{2}$', re.I)
LANGUAGE_RE = re.compile(r'^[a-z]{2}$', re.I)
VERIFIED_ADDRESS = ('Verified', 'VerifiedShippable')
# Countries / regions where Microsoft requires the organization registration number
REGISTRATION_COUNTRIES = {
    'AM', 'AZ', 'BY', 'HU', 'KZ', 'KG', 'MD', 'RU', 'TJ', 'UZ', 'UA', 'BR', 'IN', 'ZA',
    'PL', 'AE', 'SA', 'TR', 'TH', 'VN', 'MM', 'IQ', 'SS', 'VE', 'CN',
}
REQUIRED_FIELDS = ('name', 'domain', 'partner_account_id', 'contact_first_name',
                   'contact_last_name', 'contact_email', 'address_line1', 'city',
                   'postal_code', 'country_id', 'culture', 'language')
# Data sent to Microsoft: a checked tenant must be checked again when it changes
CUSTOMER_FIELDS = set(REQUIRED_FIELDS) | {
    'contact_phone', 'address_line2', 'address_state', 'organization_registration_number',
    'enable_gdap', 'address_confirmed',
}
# Still editable in Odoo once the tenant is created
RENAMEABLE_FIELDS = {'name', 'domain'}
PROVISION_BATCH = 20  # tenants created per run of the scheduled action


class M365Tenant(models.Model):
    _inherit = 'm365.tenant'

    # Customer data (CompanyProfile / BillingProfile of the Partner Center customer)
    partner_account_id = fields.Many2one('m365.partner.account',
                                         string="Partner Center Account", tracking=True,
                                         groups=ADMIN_GROUP)
    contact_first_name = fields.Char(string="Contact First Name")
    contact_last_name = fields.Char(string="Contact Last Name")
    contact_email = fields.Char(string="Contact Email",
                                help="Billing contact of the customer at Microsoft.")
    contact_phone = fields.Char(string="Contact Phone")
    address_line1 = fields.Char(string="Address Line 1")
    address_line2 = fields.Char(string="Address Line 2")
    city = fields.Char(string="City")
    address_state = fields.Char(string="State / Region",
                                help="State or region code, e.g. KA or WA.")
    postal_code = fields.Char(string="Postal Code")
    country_id = fields.Many2one('res.country', string="Country")
    address_confirmed = fields.Boolean(
        string="Address Confirmed",
        help="Create the tenant with the address as entered even when Microsoft cannot "
             "verify it.")
    culture = fields.Char(string="Culture", help="e.g. en-US or en-IN.")
    language = fields.Char(string="Language", help="Two letter code, e.g. en.")
    organization_registration_number = fields.Char(
        string="Organization Registration Number",
        help="Required by Microsoft in some countries, e.g. India, Brazil or Poland.")
    enable_gdap = fields.Boolean(string="Create GDAP Relationship", default=True)

    # Creation (empty for existing tenants)
    provision_state = fields.Selection([
        ('draft', 'To Check'),
        ('ready', 'Ready'),
        ('queued', 'Creating'),
        ('done', 'Created'),
        ('failed', 'Failed'),
    ], string="Creation Status", readonly=True, copy=False, tracking=True, index=True)
    address_status = fields.Char(string="Address Verification", readonly=True, copy=False)
    provision_error = fields.Text(string="Creation Error", readonly=True, copy=False)
    queued_by = fields.Many2one('res.users', string="Requested By", readonly=True,
                                copy=False)
    pc_request_id = fields.Char(
        string="Pending Request ID", readonly=True, copy=False,
        help="MS-RequestId of a creation that got no answer, sent again on retry so that "
             "the tenant is not created twice.")
    customer_id = fields.Char(string="Customer ID", readonly=True, copy=False)
    created_date = fields.Datetime(string="Created On", readonly=True, copy=False)
    admin_username = fields.Char(string="Admin User", readonly=True, copy=False,
                                 groups=ADMIN_GROUP)
    admin_password = fields.Char(string="Initial Admin Password", readonly=True,
                                 copy=False, groups=ADMIN_GROUP)

    @api.model
    def _default_partner_account(self):
        return self.env['m365.partner.account'].search([], limit=1)

    @staticmethod
    def _normalise_domain(value):
        """``Contoso`` -> ``contoso.onmicrosoft.com``"""
        value = (value or '').strip().lower()
        if value and '.' not in value:
            value += INITIAL_DOMAIN
        return value

    @api.model
    def _prepare_new_tenant(self, vals):
        """Values of a new tenant: completed domain, defaults of the Partner Center account."""
        vals = dict(vals, provision_state=vals.get('provision_state') or 'draft')
        if vals.get('domain'):
            vals['domain'] = self._normalise_domain(vals['domain'])
        account = (self.env['m365.partner.account'].browse(vals['partner_account_id'])
                   if vals.get('partner_account_id') else self._default_partner_account())
        if account:
            vals['partner_account_id'] = account.id
            vals.setdefault('culture', account.default_culture)
            vals.setdefault('language', account.default_language)
            vals.setdefault('enable_gdap', account.enable_gdap)
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(['tenant_type', 'partner_account_id'])
        vals_list = [self._prepare_new_tenant(dict(defaults, **vals))
                     if vals.get('tenant_type', defaults.get('tenant_type')) == 'new' else vals
                     for vals in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('domain') and vals.get('tenant_type', self[:1].tenant_type) == 'new':
            vals = dict(vals, domain=self._normalise_domain(vals['domain']))
        changed = CUSTOMER_FIELDS & set(vals)
        if changed:
            if (self.filtered(lambda t: t.provision_state == 'queued')
                    or (changed - RENAMEABLE_FIELDS
                        and self.filtered(lambda t: t.provision_state == 'done'))):
                raise UserError(_("The Partner Center data of a tenant being created or "
                                  "already created cannot be changed."))
            if 'provision_state' not in vals:
                # Checked again with the new data
                self.filtered(lambda t: t.provision_state in ('ready', 'failed')).write(
                    {'provision_state': 'draft', 'pc_request_id': False})
        retyped = self.browse()
        if 'tenant_type' in vals:
            retyped = self.filtered(lambda t: t.tenant_type != vals['tenant_type'])
            if retyped.filtered(lambda t: t.state != 'draft'
                                or t.provision_state in ('queued', 'done')):
                raise UserError(_("The type of a connected or created tenant cannot be "
                                  "changed."))
        res = super().write(vals)
        if retyped and 'provision_state' not in vals:
            retyped.write({'provision_state': 'draft' if vals['tenant_type'] == 'new'
                           else False})
        return res

    @api.ondelete(at_uninstall=False)
    def _unlink_except_created(self):
        if self.filtered(lambda t: t.provision_state in ('queued', 'done')):
            raise UserError(_("A tenant being created or already created in Microsoft 365 "
                              "cannot be deleted. Archive it instead."))

    @api.onchange('tenant_type')
    def _onchange_tenant_type(self):
        self.provision_state = 'draft' if self.tenant_type == 'new' else False
        if self.tenant_type == 'new' and not self.partner_account_id:
            self.partner_account_id = self._default_partner_account()

    @api.onchange('partner_account_id')
    def _onchange_partner_account_id(self):
        account = self.partner_account_id
        if account:
            self.culture = self.culture or account.default_culture
            self.language = self.language or account.default_language
            self.enable_gdap = account.enable_gdap

    @api.onchange('domain')
    def _onchange_domain(self):
        if self.tenant_type == 'new':
            self.domain = self._normalise_domain(self.domain) or False

    def _commit(self):
        """Keep the result of Microsoft calls, which cannot be undone."""
        if not config['test_enable']:
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    def _check_values(self):
        """Errors in the data of the customer, before calling Microsoft."""
        self.ensure_one()
        errors = [_("%s is required.", self._fields[fname].string)
                  for fname in REQUIRED_FIELDS if not self[fname]]
        if self.domain and not DOMAIN_RE.match(self.domain):
            errors.append(_("Domain \"%s\" is not valid: use letters, digits and hyphens "
                            "followed by .onmicrosoft.com, e.g. contoso.onmicrosoft.com.",
                            self.domain))
        if self.contact_email and not email_normalize(self.contact_email):
            errors.append(_("\"%s\" is not a valid email address.", self.contact_email))
        if self.culture and not CULTURE_RE.match(self.culture):
            errors.append(_("Culture \"%s\" is not valid, e.g. en-US.", self.culture))
        if self.language and not LANGUAGE_RE.match(self.language):
            errors.append(_("Language \"%s\" is not valid, e.g. en.", self.language))
        if (self.country_id.code in REGISTRATION_COUNTRIES
                and not self.organization_registration_number):
            errors.append(_("Microsoft requires the organization registration number for "
                            "%s.", self.country_id.name))
        return errors

    def _address_payload(self):
        address = {
            'AddressLine1': self.address_line1,
            'AddressLine2': self.address_line2,
            'City': self.city,
            'State': self.address_state,
            'PostalCode': self.postal_code,
            'Country': self.country_id.code,
        }
        return {key: value for key, value in address.items() if value}

    def _check_with_microsoft(self):
        """Domain availability and address verification by Partner Center."""
        self.ensure_one()
        account = self.partner_account_id
        errors = []
        # 200: the domain exists, 404: it is available
        status, _data = account._pc_request('HEAD', '/v1/domains/%s' % self.domain,
                                            expect=(404,))
        if status != 404:
            errors.append(_("Domain %s is already used in Microsoft 365.", self.domain))
        if self.address_confirmed:
            return errors
        _status, result = account._pc_request('POST', '/v1/validations/address',
                                              json_body=self._address_payload())
        self.address_status = result.get('status')
        if self.address_status not in VERIFIED_ADDRESS:
            message = _("Microsoft could not verify the address (%(status)s). %(details)s",
                        status=self.address_status or _("no status"),
                        details=result.get('validationMessage') or '')
            suggested = (result.get('suggestedAddresses') or [{}])[0]
            if suggested:
                message += _(" Suggested address: %s.", ", ".join(
                    suggested[key] for key in ('addressLine1', 'addressLine2', 'city',
                                               'state', 'postalCode', 'country')
                    if suggested.get(key)))
            errors.append(message + _(" Correct the address, or tick Address Confirmed to "
                                      "use it as entered."))
        return errors

    def action_check(self):
        """Check the data, the domain availability and the address of new tenants."""
        self._check_admin()
        todo = self.filtered(lambda t: t.tenant_type == 'new'
                             and t.provision_state in ('draft', 'ready'))
        for rec in todo:
            errors = rec._check_values()
            if not errors:
                try:
                    errors = rec._check_with_microsoft()
                except M365GraphError as exc:
                    errors = [str(exc)]
            rec.write({'provision_state': 'draft' if errors else 'ready',
                       'provision_error': "\n".join(errors) or False})
        ready = todo.filtered(lambda t: t.provision_state == 'ready')
        message = _("%(ready)s of %(count)s tenant(s) ready to be created.",
                    ready=len(ready), count=len(todo))
        if ready != todo:
            message += " " + _("Open the others for the errors.")
        return self._notify(message, 'success' if ready == todo else 'warning')

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def action_create(self):
        """Queue the creation; the scheduled action creates the tenants one by one."""
        self._check_admin()
        todo = self.filtered(lambda t: t.tenant_type == 'new'
                             and t.provision_state in ('ready', 'failed'))
        if not todo:
            raise UserError(_("Check the tenants first: only checked or failed new tenants "
                              "can be created."))
        todo.write({'provision_state': 'queued', 'provision_error': False,
                    'queued_by': self.env.uid})
        cron = self.env.ref('m365_license_management.ir_cron_m365_create_tenants',
                            raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return self._notify(
            _("%s tenant(s) queued. They are created in Microsoft 365 in the background, "
              "in about a minute.", len(todo)))

    @api.model
    def _cron_provision_tenants(self):
        queued = self.search([('tenant_type', '=', 'new'), ('provision_state', '=', 'queued')],
                             order='id', limit=PROVISION_BATCH + 1)
        for rec in queued[:PROVISION_BATCH]:
            rec._provision()
        if len(queued) > PROVISION_BATCH:
            self.env.ref('m365_license_management.ir_cron_m365_create_tenants')._trigger()

    def _customer_payload(self):
        address = dict(self._address_payload(), FirstName=self.contact_first_name,
                       LastName=self.contact_last_name)
        if self.contact_phone:
            address['PhoneNumber'] = self.contact_phone
        company = {'Domain': self.domain}
        if self.organization_registration_number:
            company['OrganizationRegistrationNumber'] = self.organization_registration_number
        return {
            'enableGDAPByDefault': self.enable_gdap,
            'CompanyProfile': company,
            'BillingProfile': {
                'Culture': self.culture,
                'Email': self.contact_email,
                'Language': self.language,
                'CompanyName': self.name,
                'DefaultAddress': address,
            },
        }

    def _provision(self):
        """Create the customer tenant. Failures are stored, never raised."""
        self.ensure_one()
        if not self.pc_request_id:
            # Saved before the call: when the call gets no answer, the retry sends the same
            # ID and Microsoft returns the customer it created instead of a second one
            self.pc_request_id = str(uuid.uuid4())
            self._commit()
        try:
            _status, data = self.partner_account_id._pc_request(
                'POST', '/v1/customers', json_body=self._customer_payload(),
                request_id=self.pc_request_id)
        except M365GraphError as exc:
            vals = {'provision_state': 'failed', 'provision_error': str(exc)}
            if not isinstance(exc, M365PartnerCenterTimeout):
                vals['pc_request_id'] = False
            self.write(vals)
            self._log('provision_tenant', 'failed',
                      "%s (%s): %s" % (self.name, self.domain, exc), user=self.queued_by)
            self._commit()
            return False
        profile = data.get('companyProfile') or {}
        credentials = data.get('userCredentials') or {}
        domain = profile.get('domain') or self.domain
        username = credentials.get('userName') or ''
        if username and '@' not in username:
            username = '%s@%s' % (username, domain)
        self.write({
            'provision_state': 'done',
            'provision_error': False,
            'pc_request_id': False,
            'customer_id': data.get('id'),
            'tenant_id': profile.get('tenantId') or data.get('id'),
            'created_date': fields.Datetime.now(),
            'admin_username': username or False,
            'admin_password': credentials.get('password') or False,
        })
        self.message_post(body=_("Tenant created in Microsoft 365. Directory (tenant) ID: "
                                 "%s.", self.tenant_id))
        self._log('provision_tenant', 'success',
                  _("%(name)s (%(domain)s) created, customer ID %(id)s.",
                    name=self.name, domain=domain, id=self.customer_id),
                  user=self.queued_by)
        self._commit()
        return True

    # ------------------------------------------------------------------
    # Other buttons
    # ------------------------------------------------------------------
    def action_reset_creation(self):
        self.filtered(lambda t: t.provision_state == 'failed').write(
            {'provision_state': 'draft', 'pc_request_id': False})
        return True

    def action_clear_password(self):
        """Forget the initial admin password once it is handed over to the customer."""
        self._check_admin()
        for rec in self.filtered(lambda t: t.sudo().admin_password):
            rec.sudo().admin_password = False
            rec.message_post(body=_("Initial admin password removed from Odoo."))
        return True
