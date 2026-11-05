# -*- coding: utf-8 -*-
"""Tenant self-service of the portal users: ``/my/m365/tenants``.

Portal users of the group *Microsoft 365: Portal Tenant Self-Service* create new tenants
and connect existing ones, one by one or from a CSV / Excel file; the steps are those of
the backend (see ``models/m365_tenant_portal.py``). Every route checks the group, reads
the tenant as the user (record rule: own tenants only), then changes it as sudo, from a
whitelist of fields.
"""
import functools
from collections import OrderedDict

from odoo import _, http
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import content_disposition, request

from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.portal.controllers.portal import pager as portal_pager

from ..models.m365_tenant_import import NEW_HEADERS
from ..models.m365_tenant_portal import PORTAL_GROUP

URL = '/my/m365/tenants'
TENANT_TYPES = ('existing', 'new')
MAX_FILE_SIZE = 2 * 1024 * 1024  # bytes
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
IMPORT_SESSION_KEY = 'm365_portal_import'
MESSAGE_SESSION_KEY = 'm365_portal_message'
INPUT_TYPES = {'contact_email': 'email', 'contact_phone': 'tel', 'client_secret': 'password',
               'country_id': 'select'}
PLACEHOLDERS = {
    'name': 'Contoso Ltd', 'tenant_id': '00000000-0000-0000-0000-000000000000',
    'client_id': '00000000-0000-0000-0000-000000000000', 'contact_email': 'name@contoso.com',
    'culture': 'en-US', 'language': 'en',
}


def m365_portal(func):
    """Portal users of the self-service group only. Tenants of other users: 404."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if not request.env.user.has_group(PORTAL_GROUP):
            return request.redirect('/my')
        try:
            return func(self, *args, **kwargs)
        except (AccessError, MissingError):
            raise request.not_found()
    return wrapper


class M365TenantPortal(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'm365_tenant_count' in counters:
            values['m365_tenant_count'] = (
                request.env['m365.tenant'].search_count(self._m365_domain())
                if request.env.user.has_group(PORTAL_GROUP) else 0)
        return values

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _m365_domain():
        return [('portal_user_id', '=', request.env.uid)]

    @staticmethod
    def _m365_tenant(tenant_id):
        """The tenant of the portal user, as sudo (the user of the environment stays the
        portal user)."""
        tenant = request.env['m365.tenant'].browse(tenant_id).exists()
        if not tenant:
            raise MissingError(_("This tenant does not exist."))
        tenant.check_access('read')
        tenant = tenant.sudo()
        if tenant.portal_user_id != request.env.user or not tenant.active:
            raise AccessError(_("This tenant is not yours."))
        return tenant

    @staticmethod
    def _m365_posted():
        """Posted form fields. Not the keyword arguments of the route: the form field
        tenant_id (Directory ID) would be the ID of the tenant in the URL."""
        return request.httprequest.form.to_dict()

    @staticmethod
    def _m365_message(message, kind='success'):
        """Message shown on the next page."""
        request.session[MESSAGE_SESSION_KEY] = [kind, message]

    def _m365_step_message(self, tenant, step):
        """Message of the result of a step on the tenant."""
        if step == 'check':
            if tenant.provision_state == 'ready':
                return self._m365_message(_("The tenant is ready: you can create it in "
                                            "Microsoft 365."))
            if tenant.provision_state == 'draft':
                return self._m365_message(_("Correct the errors shown below, then check "
                                            "again."), 'warning')
            return self._m365_message(_("Changes saved."))
        if step == 'create':
            return self._m365_message(_("The tenant is being created in Microsoft 365. This "
                                        "takes about a minute."), 'info')
        if step == 'test':
            log = self._m365_last_test(tenant)
            if tenant.state == 'connected' and log.status == 'success':
                return self._m365_message(_("Connected to Microsoft 365."))
            if tenant.state == 'connected':
                return self._m365_message(_("Connected, with missing permissions: see "
                                            "below."), 'warning')
            return self._m365_message(_("The connection failed: see the error below."),
                                      'danger')
        if step == 'reset':
            return self._m365_message(_("The tenant is back to To Check: correct the data, "
                                        "then check it again."), 'info')
        if step == 'password':
            return self._m365_message(_("The initial admin password is removed from Odoo."))
        return None

    @staticmethod
    def _m365_last_test(tenant):
        return request.env['m365.audit.log'].sudo().search(
            [('tenant_id', '=', tenant.id), ('action', '=', 'test_connection')], limit=1)

    def _m365_render(self, template, **values):
        render_values = self._prepare_portal_layout_values()
        render_values.update({
            'page_name': 'm365_tenant',
            'm365_message': request.session.pop(MESSAGE_SESSION_KEY, None),
            'm365_can_create_new': bool(request.env['m365.tenant']._portal_partner_account()),
        })
        render_values.update(values)
        return request.render(template, render_values)

    def _m365_no_partner_account(self):
        self._m365_message(_("New tenants cannot be created yet: no Partner Center account "
                             "is configured. Please contact us."), 'danger')
        return request.redirect(URL)

    # ------------------------------------------------------------------
    # Tenants
    # ------------------------------------------------------------------
    @http.route([URL, URL + '/page/<int:page>'], type='http', auth='user', website=True)
    @m365_portal
    def portal_my_m365_tenants(self, page=1, sortby=None, filterby=None, search=None, **kw):
        Tenant = request.env['m365.tenant']
        searchbar_sortings = {
            'date': {'label': _("Newest"), 'order': 'create_date desc, id desc'},
            'name': {'label': _("Name"), 'order': 'name, id'},
        }
        searchbar_filters = OrderedDict([
            ('all', {'label': _("All"), 'domain': []}),
            ('new', {'label': _("New Tenants"), 'domain': [('tenant_type', '=', 'new')]}),
            ('existing', {'label': _("Existing Tenants"),
                          'domain': [('tenant_type', '=', 'existing')]}),
            ('connected', {'label': _("Connected"), 'domain': [('state', '=', 'connected')]}),
            ('not_connected', {'label': _("Not Connected"),
                               'domain': [('state', '!=', 'connected')]}),
        ])
        sortby = sortby if sortby in searchbar_sortings else 'date'
        filterby = filterby if filterby in searchbar_filters else 'all'
        domain = self._m365_domain() + searchbar_filters[filterby]['domain']
        if search:
            domain += ['|', ('name', 'ilike', search), ('domain', 'ilike', search)]
        pager = portal_pager(
            url=URL, url_args={'sortby': sortby, 'filterby': filterby, 'search': search},
            total=Tenant.search_count(domain), page=page, step=self._items_per_page)
        tenants = Tenant.search(domain, order=searchbar_sortings[sortby]['order'],
                                limit=self._items_per_page, offset=pager['offset']).sudo()
        return self._m365_render(
            'm365_license_management.portal_my_m365_tenants',
            page_name='m365_tenants',
            tenants=tenants,
            m365_busy=any(tenant._portal_busy() for tenant in tenants),
            pager=pager,
            default_url=URL,
            searchbar_sortings=searchbar_sortings,
            sortby=sortby,
            searchbar_filters=searchbar_filters,
            filterby=filterby,
            searchbar_inputs={'all': {'input': 'all', 'label': _("Search in Name and Domain")}},
            search_in='all',
            search=search,
        )

    @http.route(URL + '/batch', type='http', auth='user', website=True, methods=['POST'])
    @m365_portal
    def portal_m365_tenant_batch(self, operation=None, **kw):
        """Check, create or test the tenants selected in the list."""
        if operation not in ('check', 'create', 'test'):
            raise request.not_found()
        ids = [int(tenant_id) for tenant_id in request.httprequest.form.getlist('tenant_ids')
               if tenant_id.isdigit()]
        tenants = request.env['m365.tenant'].search(
            self._m365_domain() + [('id', 'in', ids)]).sudo()
        todo = tenants.filtered(lambda t: operation in t._portal_operations())
        if not tenants:
            self._m365_message(_("Select the tenants first."), 'warning')
        elif not todo:
            self._m365_message({
                'check': _("None of the selected tenants can be checked: only new tenants "
                           "To Check or Ready can."),
                'create': _("None of the selected tenants can be created: check them "
                            "first, only Ready or Failed new tenants can be created."),
                'test': _("None of the selected tenants can be tested: add the client ID "
                          "of their app registration first."),
            }[operation], 'warning')
        elif operation == 'create':
            todo._portal_create_in_microsoft()
            self._m365_message(_("%s tenant(s) are being created in Microsoft 365. This "
                                 "takes about a minute per tenant.", len(todo)), 'info')
        else:
            todo._portal_queue(operation)
            if operation == 'check':
                message = _("%s tenant(s) are being checked in the background.", len(todo))
            else:
                message = _("The connection of %s tenant(s) is being tested in the "
                            "background.", len(todo))
            self._m365_message(message, 'info')
        return request.redirect(URL)

    @http.route(URL + '/new', type='http', auth='user', website=True,
                methods=['GET', 'POST'])
    @m365_portal
    def portal_m365_tenant_new(self, tenant_type='existing', **kw):
        """Add one tenant: New (Save & Check) or Existing (Save & Test Connection)."""
        Tenant = request.env['m365.tenant']
        if tenant_type not in TENANT_TYPES:
            tenant_type = 'existing'
        if tenant_type == 'new' and not Tenant._portal_partner_account():
            return self._m365_no_partner_account()
        values, errors, other_errors = Tenant._portal_form_defaults(tenant_type), {}, []
        if request.httprequest.method == 'POST':
            values = self._m365_posted()
            vals, errors, other_errors = Tenant._portal_form_values(tenant_type, values)
            if not (errors or other_errors):
                tenant = Tenant._portal_create([dict(vals, tenant_type=tenant_type)])
                if tenant_type == 'new':
                    tenant._portal_check()
                    self._m365_step_message(tenant, 'check')
                else:
                    tenant._portal_test_connection()
                    self._m365_step_message(tenant, 'test')
                return request.redirect(tenant._portal_url())
        return self._m365_render_form(tenant_type, values, errors, other_errors)

    @http.route(URL + '/<int:tenant_id>', type='http', auth='user', website=True)
    @m365_portal
    def portal_m365_tenant(self, tenant_id, **kw):
        tenant = self._m365_tenant(tenant_id)
        return self._m365_render(
            'm365_license_management.portal_m365_tenant_page',
            m365_tenant=tenant,
            m365_operations=tenant._portal_operations(),
            m365_last_test=self._m365_last_test(tenant),
        )

    @http.route(URL + '/<int:tenant_id>/edit', type='http', auth='user', website=True,
                methods=['GET', 'POST'])
    @m365_portal
    def portal_m365_tenant_edit(self, tenant_id, **kw):
        """Customer data of a new tenant not created yet, then Check."""
        tenant = self._m365_tenant(tenant_id)
        if 'edit' not in tenant._portal_operations():
            return request.redirect(tenant._portal_url())
        return self._m365_edit(tenant, 'new')

    @http.route(URL + '/<int:tenant_id>/connect', type='http', auth='user', website=True,
                methods=['GET', 'POST'])
    @m365_portal
    def portal_m365_tenant_connect(self, tenant_id, **kw):
        """App registration of an existing tenant or of a created new one, then Test."""
        tenant = self._m365_tenant(tenant_id)
        if 'connect' not in tenant._portal_operations():
            return request.redirect(tenant._portal_url())
        return self._m365_edit(tenant, tenant.tenant_type == 'new' and 'connection'
                               or 'existing')

    @http.route(URL + '/<int:tenant_id>/<any(check,create,reset,test,remove,password):step>',
                type='http', auth='user', website=True, methods=['POST'])
    @m365_portal
    def portal_m365_tenant_step(self, tenant_id, step, **kw):
        """Buttons of the tenant page."""
        tenant = self._m365_tenant(tenant_id)
        if step not in tenant._portal_operations():
            self._m365_message(_("This step is not available in the current status of the "
                                 "tenant."), 'warning')
            return request.redirect(tenant._portal_url())
        if step == 'remove':
            tenant._portal_remove()
            self._m365_message(_("Tenant %s removed.", tenant.name))
            return request.redirect(URL)
        {
            'check': tenant._portal_check,
            'create': tenant._portal_create_in_microsoft,
            'reset': tenant._portal_reset,
            'test': tenant._portal_test_connection,
            'password': tenant._portal_clear_password,
        }[step]()
        self._m365_step_message(tenant, step)
        return request.redirect(tenant._portal_url())

    # ------------------------------------------------------------------
    # Forms
    # ------------------------------------------------------------------
    def _m365_edit(self, tenant, form):
        values, errors, other_errors = tenant._portal_form_data(form), {}, []
        if request.httprequest.method == 'POST':
            values = self._m365_posted()
            vals, errors, other_errors = tenant._portal_form_values(form, values, tenant)
            if not (errors or other_errors):
                tenant._portal_save(vals)
                if form == 'new':
                    tenant._portal_check()
                    self._m365_step_message(tenant, 'check')
                else:
                    tenant._portal_test_connection()
                    self._m365_step_message(tenant, 'test')
                return request.redirect(tenant._portal_url())
        return self._m365_render_form(form, values, errors, other_errors, tenant)

    def _m365_sections(self, form):
        """(title, field names, collapsed) of the sections of a portal form."""
        endpoints = (_("Advanced: sovereign clouds only"),
                     ('authority_url', 'graph_url', 'graph_scope'), True)
        if form == 'new':
            return [
                (_("Company"), ('name', 'domain', 'organization_registration_number'), False),
                (_("Contact"), ('contact_first_name', 'contact_last_name', 'contact_email',
                                'contact_phone'), False),
                (_("Registered Address"), ('address_line1', 'address_line2', 'city',
                                           'address_state', 'postal_code', 'country_id'),
                 False),
                (_("Advanced: language"), ('culture', 'language'), True),
            ]
        if form == 'existing':
            return [(_("Tenant"), ('name', 'domain', 'tenant_id'), False),
                    (_("App Registration"), ('client_id', 'client_secret'), False),
                    endpoints]
        return [(_("App Registration"), ('client_id', 'client_secret'), False), endpoints]

    def _m365_field_help(self, form, fname, tenant=None):
        Tenant = request.env['m365.tenant']
        helps = {
            'domain': (_("Initial domain of the new tenant: \"contoso\" becomes "
                         "contoso.onmicrosoft.com. It cannot be changed afterwards.")
                       if form == 'new' else _("Primary domain, e.g. contoso.com.")),
            'tenant_id': _("Microsoft Entra admin center > Overview > Tenant ID."),
            'client_id': _("Application (client) ID of the app registration, with the "
                           "application permissions Organization.Read.All and "
                           "User.Read.All granted by an administrator."),
            'client_secret': (_("Leave blank to keep the saved secret. It is never shown "
                                "again.")
                              if tenant and tenant.client_secret else
                              _("Value of a client secret of the app registration.")),
        }
        return helps.get(fname, Tenant._fields[fname].help or '')

    def _m365_render_form(self, form, values, errors, other_errors, tenant=None):
        Tenant = request.env['m365.tenant']
        required = {fname: req for fname, _kind, req in Tenant._portal_columns(form)}
        if 'client_secret' in required:
            required['client_secret'] = not (tenant and tenant.client_secret)
        sections = []
        for title, fnames, collapsed in self._m365_sections(form):
            fields = [{
                'name': fname,
                'label': Tenant._fields[fname].string,
                'required': required[fname],
                'type': INPUT_TYPES.get(fname, 'text'),
                'value': '' if fname == 'client_secret' else values.get(fname) or '',
                'placeholder': PLACEHOLDERS.get(fname, ''),
                'help': self._m365_field_help(form, fname, tenant),
                'error': errors.get(fname),
            } for fname in fnames]
            # Open a collapsed section with an error
            sections.append((title, fields, collapsed and not any(f['error'] for f in fields)))
        if tenant:
            subpage = _("Edit") if form == 'new' else _("Connection")
        else:
            subpage = _("Create a New Tenant") if form == 'new' else _("Add an Existing Tenant")
        return self._m365_render(
            'm365_license_management.portal_m365_tenant_form',
            m365_tenant=tenant,
            m365_form=form,
            m365_sections=sections,
            m365_errors=list(errors.values()) + other_errors,
            m365_values=values,
            m365_countries=request.env['res.country'].sudo().search([]),
            m365_show_address_confirmed=bool(
                tenant and form == 'new' and tenant._portal_show_address_confirmed()),
            m365_subpage=subpage,
        )

    # ------------------------------------------------------------------
    # Bulk import
    # ------------------------------------------------------------------
    def _m365_render_import(self, tenant_type, rows=None, error=None, filename=None):
        Tenant = request.env['m365.tenant']
        # (column title, required) of the files of each type
        columns = {
            'existing': [(Tenant._fields[fname].string, required)
                         for fname, _kind, required in Tenant._portal_columns('existing')],
            'new': [(NEW_HEADERS[fname], required)
                    for fname, _kind, required in Tenant._portal_columns('new')],
        }
        max_rows = int(request.env['ir.config_parameter'].sudo().get_param(
            'm365.portal_import_max_rows', 100) or 0)
        return self._m365_render(
            'm365_license_management.portal_m365_tenant_import',
            m365_subpage=_("Bulk Import"),
            m365_type=tenant_type,
            m365_columns=columns,
            m365_max_rows=max_rows,
            m365_rows=rows,
            m365_valid=bool(rows) and not any(row['errors'] for row in rows),
            m365_error=error,
            m365_filename=filename,
        )

    @http.route(URL + '/import', type='http', auth='user', website=True,
                methods=['GET', 'POST'])
    @m365_portal
    def portal_m365_tenant_import(self, tenant_type='existing', import_file=None, **kw):
        """Upload a file and show its rows with their errors (nothing is created)."""
        Tenant = request.env['m365.tenant']
        if tenant_type not in TENANT_TYPES:
            tenant_type = 'existing'
        if tenant_type == 'new' and not Tenant._portal_partner_account():
            return self._m365_no_partner_account()
        if request.httprequest.method != 'POST':
            return self._m365_render_import(tenant_type)
        if not getattr(import_file, 'filename', None):
            return self._m365_render_import(
                tenant_type, error=_("Select a CSV or Excel (.xlsx) file."))
        content = import_file.read(MAX_FILE_SIZE + 1)
        if len(content) > MAX_FILE_SIZE:
            return self._m365_render_import(
                tenant_type, error=_("The file is too large: at most 2 MB."))
        wizard = Tenant._portal_import_wizard(tenant_type, content, import_file.filename)
        try:
            rows = Tenant._portal_parse(wizard)
        except UserError as exc:
            return self._m365_render_import(tenant_type, error=str(exc))
        request.session[IMPORT_SESSION_KEY] = wizard.id
        return self._m365_render_import(tenant_type, rows=rows, filename=wizard.filename)

    @http.route(URL + '/import/submit', type='http', auth='user', website=True,
                methods=['POST'])
    @m365_portal
    def portal_m365_tenant_import_submit(self, **kw):
        """Add the tenants of the uploaded file: all of them, or none when a row has
        an error. Then check (New) or test (Existing) them in the background."""
        Tenant = request.env['m365.tenant']
        wizard = request.env['m365.tenant.import.wizard'].sudo().browse(
            request.session.get(IMPORT_SESSION_KEY)).exists()
        if not wizard or wizard.create_uid != request.env.user or not wizard.file:
            self._m365_message(_("Upload the file again."), 'warning')
            return request.redirect(URL + '/import')
        tenant_type = wizard.tenant_type
        # Read again: the tenants of Odoo may have changed since the upload
        try:
            rows = Tenant._portal_parse(wizard)
        except UserError as exc:
            return self._m365_render_import(tenant_type, error=str(exc))
        if any(row['errors'] for row in rows):
            return self._m365_render_import(tenant_type, rows=rows, filename=wizard.filename)
        tenants = Tenant._portal_create(
            [dict(row['vals'], tenant_type=tenant_type) for row in rows],
            filename=wizard.filename)
        wizard.file = False
        request.session.pop(IMPORT_SESSION_KEY, None)
        if tenant_type == 'new':
            tenants._portal_queue('check')
            message = _("%s new tenant(s) added. They are being checked in the background: "
                        "then select the Ready ones and create them in Microsoft 365.",
                        len(tenants))
        else:
            to_test = tenants.filtered('client_secret')
            to_test._portal_queue('test')
            message = _("%s existing tenant(s) added. Their connection is being tested in "
                        "the background.", len(tenants))
            if tenants - to_test:
                message += " " + _("Add the client secret of the %s tenant(s) without one "
                                   "to connect them.", len(tenants - to_test))
        self._m365_message(message, 'info')
        return request.redirect(URL)

    @http.route(URL + '/import/template', type='http', auth='user', website=True)
    @m365_portal
    def portal_m365_tenant_import_template(self, tenant_type='existing', **kw):
        if tenant_type not in TENANT_TYPES:
            tenant_type = 'existing'
        content, filename = request.env['m365.tenant']._portal_import_template(tenant_type)
        return request.make_response(content, headers=[
            ('Content-Type', XLSX),
            ('Content-Disposition', content_disposition(filename)),
        ])
