# -*- coding: utf-8 -*-
"""GDAP (granular delegated admin privileges) of the customer tenants.

The relationships are read and created in the **partner** tenant with Microsoft Graph,
using the shared app of the Partner Center account (app-only token for the partner
directory):

=========================================  ===============================================
Step                                       Graph call (partner tenant)
=========================================  ===============================================
Status of the customer's relationships     GET  /tenantRelationships/delegatedAdminRelationships
                                                ?$filter=customer/tenantId eq '{tenant}'
Request a relationship                     POST /tenantRelationships/delegatedAdminRelationships
Send it for the customer's approval        POST .../delegatedAdminRelationships/{id}/requests
                                                {"action": "lockForApproval"}
=========================================  ===============================================

Requirements in the partner tenant: the application permission
``DelegatedAdminRelationship.ReadWrite.All`` (``.Read.All`` to read only) on the shared
app, and, to create relationships, the service principal of *Partner Customer Delegated
Administration* (appId ``2832473f-ec63-45fb-976f-5d45a7d4bb91``). New tenants created
with *Create GDAP Relationship* get Microsoft's default relationship automatically.

Reference: https://learn.microsoft.com/en-us/graph/api/resources/delegatedadminrelationship
"""
import time
from datetime import timedelta

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

from .m365_tenant import M365GraphError

GRAPH_URL = 'https://graph.microsoft.com/v1.0'
GRAPH_SCOPE = 'https://graph.microsoft.com/.default'
RELATIONSHIPS = '/tenantRelationships/delegatedAdminRelationships'
APPROVAL_URL = ('https://admin.microsoft.com/AdminPortal/Home#/partners/invitation/'
                'granularAdminRelationships/%s')
# User Administrator, License Administrator, Helpdesk Administrator, Directory Readers,
# Global Reader: what the module needs to manage users and licenses
OLD_DEFAULT_GDAP_ROLES = ('fe930be7-5e62-47db-91af-98c3a49a38b1,'
                          '4d6ac14f-3453-41d0-bef9-a3e0c569773a,'
                          '729827e3-9c14-49f7-bb1b-9608f156bbb8,'
                          '88d8e3e3-8f55-4a1e-953a-9b9898b8876b,'
                          'f2ef992c-3afb-46b9-b7cf-a126ee74c451')
# + Privileged Role Administrator: grants the application permissions of the shared app
# in the customer (Install Shared App)
PRIVILEGED_ROLE_ADMIN = 'e8611ab8-c189-46e8-94e1-60213ab1f814'
DEFAULT_GDAP_ROLES = OLD_DEFAULT_GDAP_ROLES + ',' + PRIVILEGED_ROLE_ADMIN
ASSIGNMENTS = RELATIONSHIPS + '/%s/accessAssignments'
ASSIGNMENT_STATES = [
    ('none', 'Not Assigned'),
    ('pending', 'Pending'),
    ('active', 'Active'),
    ('deleting', 'Deleting'),
    ('deleted', 'Deleted'),
    ('error', 'Error'),
]
GDAP_STATES = [
    ('none', 'No Relationship'),
    ('created', 'Created'),
    ('approvalPending', 'Approval Pending'),
    ('activating', 'Activating'),
    ('active', 'Active'),
    ('expiring', 'Expiring'),
    ('expired', 'Expired'),
    ('terminationRequested', 'Termination Requested'),
    ('terminating', 'Terminating'),
    ('terminated', 'Terminated'),
]
# Preference when a customer has several relationships
GDAP_RANK = {'active': 0, 'expiring': 1, 'activating': 2, 'approvalPending': 3,
             'created': 4, 'expired': 5, 'terminationRequested': 6, 'terminating': 7,
             'terminated': 8}


class M365PartnerAccountGraph(models.Model):
    _inherit = 'm365.partner.account'

    graph_access_token = fields.Char(copy=False, groups='base.group_system')
    graph_token_expiry = fields.Datetime(copy=False, groups='base.group_system')
    gdap_role_ids = fields.Char(
        string="GDAP Roles", default=DEFAULT_GDAP_ROLES,
        help="Role definition IDs (comma separated) requested in new GDAP relationships. "
             "Default: User, License and Helpdesk Administrator, Directory Readers, Global "
             "Reader.")
    gdap_duration_days = fields.Integer(string="GDAP Duration (Days)", default=730,
                                       help="1 to 730 days.")
    gdap_auto_extend = fields.Boolean(string="Extend GDAP Automatically", default=True,
                                      help="Extend the relationship by 180 days at its end.")
    gdap_group_id = fields.Char(
        string="GDAP Security Group (Object ID)",
        help="Security group of the partner tenant given the GDAP roles in each customer "
             "(access assignment). Put the partner service user and the administrators in "
             "it. Empty: no access assignment is made.")
    gdap_assignment_role_ids = fields.Char(
        string="Roles of the Group",
        help="Role definition IDs (comma separated) given to the group. Empty: all the roles "
             "of the relationship.")

    def write(self, vals):
        res = super().write(vals)
        if {'graph_client_id', 'graph_client_secret', 'partner_tenant_id',
                'authority_url'} & set(vals):
            self.sudo().write({'graph_access_token': False, 'graph_token_expiry': False})
        return res

    def _get_partner_graph_token(self, force=False):
        """App-only Graph token of the shared app in the partner tenant."""
        self.ensure_one()
        rec = self.sudo()
        now = fields.Datetime.now()
        if (not force and rec.graph_access_token and rec.graph_token_expiry
                and rec.graph_token_expiry > now + timedelta(minutes=5)):
            return rec.graph_access_token
        client_id, secret = rec._shared_app_credentials()
        url = "%s/%s/oauth2/v2.0/token" % (rec.authority_url.rstrip('/'),
                                           rec.partner_tenant_id.strip())
        try:
            resp = requests.post(url, data={
                'client_id': client_id, 'client_secret': secret, 'scope': GRAPH_SCOPE,
                'grant_type': 'client_credentials'}, timeout=30)
        except requests.RequestException as exc:
            raise M365GraphError(_("Unable to reach the Microsoft identity platform: %s",
                                   exc)) from None
        if resp.status_code != 200:
            raise M365GraphError(_("Authentication in the partner tenant failed. %s",
                                   self.env['m365.tenant']._format_error(resp)))
        payload = resp.json()
        rec.write({'graph_access_token': payload['access_token'],
                   'graph_token_expiry': now + timedelta(
                       seconds=int(payload.get('expires_in', 3600)))})
        return rec.graph_access_token

    def _partner_graph_request(self, method, path, params=None, json_body=None,
                               paginate=False):
        """Microsoft Graph in the partner tenant (token refresh, throttling, paging)."""
        self.ensure_one()
        url = path if path.startswith('http') else GRAPH_URL + path
        results, retries, refreshed = [], 0, False
        while url:
            headers = {'Authorization': 'Bearer %s' % self._get_partner_graph_token(),
                       'Content-Type': 'application/json'}
            try:
                resp = requests.request(method, url, headers=headers, params=params,
                                        json=json_body, timeout=60)
            except requests.RequestException as exc:
                raise M365GraphError(_("Microsoft Graph request failed: %s", exc)) from None
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._get_partner_graph_token(force=True)
                continue
            if resp.status_code in (429, 503, 504) and retries < 4:
                retries += 1
                try:
                    wait = int(resp.headers.get('Retry-After', 2))
                except ValueError:
                    wait = 2
                time.sleep(min(max(wait, 1), 30))
                continue
            if resp.status_code >= 400:
                raise M365GraphError(self.env['m365.tenant']._format_error(resp))
            data = resp.json() if resp.content else {}
            if not paginate:
                return data
            results.extend(data.get('value', []))
            url, params = data.get('@odata.nextLink'), None
        return results


class M365TenantGdap(models.Model):
    _inherit = 'm365.tenant'

    gdap_status = fields.Selection(GDAP_STATES, string="GDAP Status", readonly=True,
                                   copy=False, tracking=True,
                                   help="Granular delegated admin relationship with the "
                                        "customer, as read from the partner tenant.")
    gdap_relationship_id = fields.Char(string="GDAP Relationship ID", readonly=True,
                                       copy=False)
    gdap_name = fields.Char(string="GDAP Relationship", readonly=True, copy=False)
    gdap_end_date = fields.Datetime(string="GDAP Ends On", readonly=True, copy=False)
    gdap_role_count = fields.Integer(string="GDAP Roles", readonly=True, copy=False)
    gdap_approval_url = fields.Char(
        string="GDAP Approval Link", readonly=True, copy=False,
        help="Send it to a Global Administrator of the customer, who approves the "
             "relationship in the Microsoft 365 admin center.")
    gdap_checked = fields.Datetime(string="GDAP Checked On", readonly=True, copy=False)
    gdap_error = fields.Char(string="GDAP Error", readonly=True, copy=False)
    gdap_role_ids = fields.Char(string="GDAP Relationship Roles", readonly=True, copy=False)
    gdap_assignment_id = fields.Char(string="GDAP Access Assignment", readonly=True,
                                     copy=False)
    gdap_assignment_status = fields.Selection(
        ASSIGNMENT_STATES, string="GDAP Group Access", readonly=True, copy=False,
        help="Access assignment of the security group of the Partner Center account in the "
             "relationship.")

    def _gdap_account(self):
        self.ensure_one()
        account = self.sudo().partner_account_id or self._default_partner_account()
        if not account:
            raise UserError(_("Add a Partner Center account with its shared app first."))
        if not account.partner_tenant_id:
            raise UserError(_("GDAP needs the Partner Directory (Tenant) ID of the Partner "
                              "Center account %s.", account.name))
        return account

    def _refresh_gdap(self):
        """Read the GDAP relationships of the customer in the partner tenant."""
        self.ensure_one()
        if not self.tenant_id:
            raise UserError(_("The Directory (Tenant) ID of %s is not known yet.", self.name))
        account = self._gdap_account()
        items = account._partner_graph_request('GET', RELATIONSHIPS, paginate=True, params={
            '$filter': "customer/tenantId eq '%s'" % self.tenant_id.strip().lower()})
        items = [i for i in items if (i.get('customer') or {}).get('tenantId', '').lower()
                 == self.tenant_id.strip().lower()]
        vals = {'gdap_checked': fields.Datetime.now(), 'gdap_error': False}
        if not items:
            vals.update(gdap_status='none', gdap_relationship_id=False, gdap_name=False,
                        gdap_end_date=False, gdap_role_count=0, gdap_role_ids=False,
                        gdap_assignment_id=False, gdap_assignment_status=False)
        else:
            best = sorted(items, key=lambda i: (GDAP_RANK.get(i.get('status'), 9),
                                                i.get('createdDateTime') or ''))[0]
            status = best.get('status')
            roles = [r.get('roleDefinitionId') for r in
                     (best.get('accessDetails') or {}).get('unifiedRoles') or []
                     if r.get('roleDefinitionId')]
            if best.get('id') != self.gdap_relationship_id:
                vals.update(gdap_assignment_id=False, gdap_assignment_status=False)
            vals.update(
                gdap_status=status if status in GDAP_RANK else False,
                gdap_relationship_id=best.get('id'),
                gdap_name=best.get('displayName') or False,
                gdap_end_date=fields.Datetime.to_datetime(
                    (best.get('endDateTime') or '')[:19].replace('T', ' ') or False),
                gdap_role_count=len(roles),
                gdap_role_ids=','.join(roles) or False,
            )
            if status in ('active', 'expiring'):
                vals['gdap_approval_url'] = False
        self.sudo().write(vals)
        if self.gdap_status in ('active', 'expiring') and account.gdap_group_id:
            self._refresh_gdap_assignment(account)
        return self.gdap_status

    def _refresh_gdap_assignment(self, account):
        """Access assignment of the group of the account in the relationship.

        :return: its status, or 'none'
        """
        self.ensure_one()
        group = (account.gdap_group_id or '').strip().lower()
        items = account._partner_graph_request(
            'GET', ASSIGNMENTS % self.gdap_relationship_id, paginate=True)
        mine = [i for i in items if ((i.get('accessContainer') or {}).get('accessContainerId')
                                     or '').lower() == group
                and i.get('status') not in ('deleted', 'deleting')]
        status = mine[0].get('status') if mine else 'none'
        self.sudo().write({
            'gdap_assignment_id': mine[0].get('id') if mine else False,
            'gdap_assignment_status': status if status in dict(ASSIGNMENT_STATES) else 'error',
        })
        return self.gdap_assignment_status

    def _gdap_assignment_roles(self, account):
        """Roles given to the group: those wanted on the account, among the roles of the
        relationship."""
        self.ensure_one()
        relationship = [r.strip().lower() for r in (self.gdap_role_ids or '').split(',')
                        if r.strip()]
        wanted = [r.strip().lower() for r in (account.gdap_assignment_role_ids or '').split(',')
                  if r.strip()] or relationship
        return [r for r in wanted if r in relationship]

    def _ensure_gdap_assignment(self):
        """Give the GDAP roles to the security group of the Partner Center account, once
        the relationship is active. :return: assignment status, or False when the
        account has no group (not needed)"""
        self.ensure_one()
        account = self._gdap_account()
        if not account.gdap_group_id:
            return False
        if self.gdap_status not in ('active', 'expiring'):
            raise UserError(_("The GDAP relationship of %s is not active.", self.name))
        status = self._refresh_gdap_assignment(account)
        if status != 'none':
            return status
        roles = self._gdap_assignment_roles(account)
        if not roles:
            raise UserError(_("None of the roles to give to the GDAP group is in the "
                              "relationship of %s.", self.name))
        item = account._partner_graph_request(
            'POST', ASSIGNMENTS % self.gdap_relationship_id, json_body={
                'accessContainer': {'accessContainerId': account.gdap_group_id.strip(),
                                    'accessContainerType': 'securityGroup'},
                'accessDetails': {'unifiedRoles': [{'roleDefinitionId': r} for r in roles]},
            })
        status = item.get('status') or 'pending'
        self.sudo().write({'gdap_assignment_id': item.get('id'),
                           'gdap_assignment_status': status if status in dict(
                               ASSIGNMENT_STATES) else 'pending'})
        self._log('gdap', 'success', _(
            "GDAP roles (%(count)s) given to the security group %(group)s.",
            count=len(roles), group=account.gdap_group_id))
        return self.gdap_assignment_status

    def action_gdap_assign_group(self):
        self._check_admin()
        errors = []
        for tenant in self:
            try:
                tenant._refresh_gdap()
                tenant._ensure_gdap_assignment()
            except (M365GraphError, UserError) as exc:
                tenant.sudo().gdap_error = str(exc)[:250]
                tenant._log('gdap', 'failed', str(exc))
                errors.append("%s: %s" % (tenant.name, exc))
        if errors:
            return self._notify("\n".join(errors), 'danger')
        return self._notify(_("GDAP group access requested."))

    def action_refresh_gdap(self):
        self._check_admin()
        errors = []
        for tenant in self:
            try:
                tenant._refresh_gdap()
                tenant._log('gdap', 'info', _("GDAP status: %s.", dict(GDAP_STATES).get(
                    tenant.gdap_status, '-')))
            except (M365GraphError, UserError) as exc:
                tenant.sudo().gdap_error = str(exc)[:250]
                tenant._log('gdap', 'failed', str(exc))
                errors.append("%s: %s" % (tenant.name, exc))
        if errors:
            return self._notify("\n".join(errors), 'danger')
        return self._notify(_("GDAP status refreshed."))

    def action_request_gdap(self):
        """Create a GDAP relationship and send it for the customer's approval."""
        self._check_admin()
        self.ensure_one()
        if not self.tenant_id:
            raise UserError(_("The Directory (Tenant) ID of %s is not known yet.", self.name))
        if self.gdap_status in ('active', 'expiring', 'activating', 'approvalPending'):
            raise UserError(_("%(tenant)s already has a GDAP relationship (%(status)s).",
                              tenant=self.name,
                              status=dict(GDAP_STATES)[self.gdap_status]))
        account = self._gdap_account()
        roles = [r.strip() for r in (account.gdap_role_ids or '').split(',') if r.strip()]
        if not roles:
            raise UserError(_("Enter the GDAP roles on the Partner Center account."))
        days = min(max(account.gdap_duration_days or 730, 1), 730)
        body = {
            # Unique among all the relationships of the partner, 50 characters at most
            'displayName': ("Odoo %s %s" % (self.name, fields.Datetime.now().strftime(
                '%Y%m%d%H%M%S')))[-50:],
            'duration': 'P%sD' % days,
            'customer': {'tenantId': self.tenant_id.strip(), 'displayName': self.name},
            'accessDetails': {'unifiedRoles': [{'roleDefinitionId': r} for r in roles]},
            'autoExtendDuration': 'P180D' if account.gdap_auto_extend else 'PT0S',
        }
        try:
            item = account._partner_graph_request('POST', RELATIONSHIPS, json_body=body)
            account._partner_graph_request(
                'POST', '%s/%s/requests' % (RELATIONSHIPS, item['id']),
                json_body={'action': 'lockForApproval'})
        except M365GraphError as exc:
            # A notification, not an error: the audit log line is kept
            self._log('gdap', 'failed', _("GDAP relationship not requested: %s", exc))
            return self._notify(_("GDAP relationship not requested: %s", exc), 'danger')
        url = APPROVAL_URL % item['id']
        self.sudo().write({'gdap_status': 'approvalPending',
                           'gdap_relationship_id': item['id'],
                           'gdap_name': item.get('displayName'), 'gdap_role_count': len(roles),
                           'gdap_approval_url': url, 'gdap_error': False,
                           'gdap_checked': fields.Datetime.now()})
        self._log('gdap', 'success', _("GDAP relationship requested; approval link: %s", url))
        return self._notify(_("GDAP relationship requested: send the approval link of the "
                              "tenant to a Global Administrator of the customer."))

    @api.model
    def _cron_refresh_gdap(self):
        """Daily: GDAP status of the tenants of the Partner Center accounts."""
        tenants = self.search([('tenant_id', '!=', False)] + self._live_domain())
        for tenant in tenants.filtered(lambda t: t.sudo().partner_account_id.graph_client_id
                                       and t.sudo().partner_account_id.partner_tenant_id):
            try:
                tenant._refresh_gdap()
            except (M365GraphError, UserError) as exc:
                tenant.sudo().gdap_error = str(exc)[:250]
            if not config['test_enable']:
                self.env.cr.commit()
