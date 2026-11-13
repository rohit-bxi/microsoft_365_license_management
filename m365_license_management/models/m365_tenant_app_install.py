# -*- coding: utf-8 -*-
"""Shared app installed in a customer tenant without a sign-in of the customer.

The signed-in partner user (App+User, see ``m365_partner_signin.py``) does it for the
customer, through the partner relationship and GDAP:

=========================================  ===============================================
Step                                       Call
=========================================  ===============================================
1. Add the shared app to the customer,     Partner Center (App+User)
   with the delegated scopes Odoo needs    POST /v1/customers/{id}/applicationconsents
   for step 2
2. Grant its application permissions       Graph in the customer tenant (partner user,
                                           GDAP Privileged Role Administrator)
   - our service principal                 GET  /servicePrincipals?$filter=appId eq '...'
   - Microsoft Graph and its app roles     GET  /servicePrincipals?$filter=appId eq '0000..'
   - roles already granted                 GET  /servicePrincipals/{ours}/appRoleAssignments
   - each missing role                     POST /servicePrincipals/{graph}/appRoleAssignedTo
3. Connect                                 the shared app reads the tenant (app-only), as
                                           after an admin consent
=========================================  ===============================================

Idempotent: an app already added, or a role already granted, is kept. The app role IDs are
read from the Microsoft Graph service principal of the tenant, never hardcoded.
"""
from odoo import _, fields, models
from odoo.exceptions import UserError

from .m365_tenant import M365GraphError

GRAPH_APP_ID = '00000003-0000-0000-c000-000000000000'
DEFAULT_PERMISSIONS = ('Organization.Read.All, Organization.ReadWrite.All, User.ReadWrite.All, '
                       'LicenseAssignment.ReadWrite.All, Domain.ReadWrite.All, Policy.Read.All, '
                       'Policy.ReadWrite.ConditionalAccess, '
                       'UserAuthenticationMethod.ReadWrite.All, '
                       'Policy.ReadWrite.AuthenticationMethod')
DEFAULT_INSTALL_SCOPES = 'Application.Read.All, AppRoleAssignment.ReadWrite.All'


def split_list(text):
    return [item.strip() for item in (text or '').replace(';', ',').split(',') if item.strip()]


class M365PartnerAccountInstall(models.Model):
    _inherit = 'm365.partner.account'

    install_permissions = fields.Char(
        string="Application Permissions", default=DEFAULT_PERMISSIONS,
        help="Microsoft Graph application permissions granted to the shared app in each "
             "customer tenant by Install Shared App (comma separated).")
    install_scopes = fields.Char(
        string="Delegated Scopes", default=DEFAULT_INSTALL_SCOPES,
        help="Microsoft Graph delegated scopes given to the shared app with the Partner "
             "Center application consent; Odoo uses them, as the partner user, to grant the "
             "application permissions.")


class M365TenantAppInstall(models.Model):
    _inherit = 'm365.tenant'

    app_install_state = fields.Selection([
        ('none', 'Not Installed'),
        ('done', 'Installed'),
        ('failed', 'Failed'),
    ], string="Shared App", default='none', readonly=True, copy=False, tracking=True,
        help="Shared app installed in the tenant by the partner user, without a sign-in of "
             "the customer.")
    app_install_date = fields.Datetime(string="Installed On", readonly=True, copy=False)
    app_install_error = fields.Text(string="Installation Error", readonly=True, copy=False)

    def _install_account(self):
        self.ensure_one()
        account = self.sudo().partner_account_id or self._default_partner_account()
        if not account:
            raise UserError(_("Add the shared app first (Configuration > Tenants > Shared "
                              "App)."))
        return account.sudo()

    def _can_install_shared_app(self):
        """Odoo can install the shared app itself (partner user signed in)."""
        self.ensure_one()
        account = self.sudo().partner_account_id or self._default_partner_account()
        return bool(account and account.graph_client_id and account._partner_signed_in()
                    and self.tenant_id and self.filtered_domain(self._live_domain()))

    def _grant_app_roles(self, account, client_id):
        """Step 2: application permissions of the shared app. :return: names added"""
        self.ensure_one()
        tid = self.sudo().tenant_id.strip()

        def graph(method, path, **kw):
            return account._customer_graph_request(tid, method, path, **kw)

        wanted = split_list(account.install_permissions)
        ours = graph('GET', '/servicePrincipals', paginate=True, params={
            '$filter': "appId eq '%s'" % client_id, '$select': 'id,appId'})
        if not ours:
            ours = [graph('POST', '/servicePrincipals', json_body={'appId': client_id})]
        graph_sps = graph('GET', '/servicePrincipals', paginate=True, params={
            '$filter': "appId eq '%s'" % GRAPH_APP_ID, '$select': 'id,appRoles'})
        if not graph_sps:
            raise M365GraphError(_("Microsoft Graph is not found in the tenant %s.", self.name))
        resource = graph_sps[0]['id']
        roles = {r.get('value'): r.get('id') for r in graph_sps[0].get('appRoles') or []
                 if 'Application' in (r.get('allowedMemberTypes') or [])}
        unknown = [name for name in wanted if name not in roles]
        if unknown:
            raise UserError(_("Unknown Microsoft Graph application permission(s): %s.",
                              ", ".join(unknown)))
        granted = {a.get('appRoleId') for a in graph(
            'GET', '/servicePrincipals/%s/appRoleAssignments' % ours[0]['id'], paginate=True)
            if a.get('resourceId') == resource}
        added = []
        for name in wanted:
            if roles[name] in granted:
                continue
            graph('POST', '/servicePrincipals/%s/appRoleAssignedTo' % resource, json_body={
                'principalId': ours[0]['id'], 'resourceId': resource, 'appRoleId': roles[name]})
            added.append(name)
        return added

    def _install_shared_app(self):
        """Install the shared app and connect the tenant through it.

        :return: True once connected, False when Microsoft does not let the app read the
            tenant yet (installed: Check Consent or the next run connects it)
        :raise M365GraphError, UserError: not installed
        """
        self.ensure_one()
        rec = self.sudo()
        if not rec.filtered_domain(rec._live_domain()) or not rec.tenant_id:
            raise UserError(_("Create the tenant in Microsoft 365 first."))
        account = rec._install_account()
        client_id, _secret = account._shared_app_credentials()
        if rec.app_install_state != 'done':
            scopes = split_list(account.install_scopes)
            account._pc_user_request(
                'POST', '/v1/customers/%s/applicationconsents' % rec._pc_customer_key(),
                json_body={'applicationId': client_id, 'applicationGrants': [{
                    'enterpriseApplicationId': GRAPH_APP_ID, 'scope': ','.join(scopes)}]},
                expect=(409,))  # 409: already added
            added = rec._grant_app_roles(account, client_id)
            rec.write({'app_install_state': 'done', 'app_install_date': fields.Datetime.now(),
                       'app_install_error': False})
            rec._log('consent', 'success', _(
                "Shared app installed by the partner user %(user)s. Permissions granted: "
                "%(perms)s.", user=account.pc_user_name or '-',
                perms=", ".join(added) or _("none missing")))
        if rec.partner_account_id != account:
            rec.partner_account_id = account
        error = rec._activate_shared_app()
        if error:
            return False
        self.action_test_connection()
        self._after_shared_app_connected()
        return True

    def action_install_shared_app(self):
        self._check_admin()
        messages, ok = [], True
        for tenant in self:
            try:
                connected = tenant._install_shared_app()
            except UserError as exc:  # M365GraphError included
                ok = False
                tenant.sudo().write({'app_install_state': 'failed',
                                     'app_install_error': str(exc)})
                tenant._log('consent', 'failed', _("Shared app not installed: %s", exc))
                messages.append("%s: %s" % (tenant.name, exc))
                continue
            messages.append(_("%s: shared app installed and connected.", tenant.name)
                            if connected else
                            _("%s: shared app installed. Microsoft can take a few minutes "
                              "to apply it: use Check Consent later.", tenant.name))
            ok = ok and connected
        return self._notify("\n".join(messages), 'success' if ok else 'warning')
