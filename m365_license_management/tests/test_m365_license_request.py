# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from ..models.m365_tenant import M365GraphError
from .common import (ADMIN_GROUP, GRAPH_PATH, GUID_A, GUID_B, USER_GROUP, USER_ID, USER_ID_2,
                     M365Common, graph_stub)


@tagged('post_install', '-at_install')
class TestM365LicenseRequest(M365Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.requester = new_test_user(cls.env, 'm365_requester', groups=USER_GROUP)
        cls.approver = new_test_user(cls.env, 'm365_approver', groups=ADMIN_GROUP)

    def _request(self, user=None, request_type='assign', env=None, **vals):
        return (env or self.env)['m365.license.request'].create(dict({
            'tenant_id': self.tenant.id,
            'user_id': (user or self.adele).id,
            'sku_id': self.sku.id,
            'request_type': request_type,
        }, **vals))

    def test_user_request_approved(self):
        req = self._request(env=self.env(user=self.requester), reason='New joiner')
        self.assertRegex(req.name, r'^LIC/\d{4}/\d{5}$')
        self.assertEqual((req.state, req.requested_by), ('draft', self.requester))
        self.assertFalse(req.is_admin)
        req.action_submit()
        self.assertEqual(req.state, 'submitted')
        self.assertEqual(self._logs('request').message,
                         '%s submitted by %s.' % (req.name, self.requester.name))
        # The administrators are asked to approve it
        activities = req.sudo().activity_ids
        self.assertIn(self.approver, activities.user_id)
        self.assertEqual(set(activities.mapped('summary')),
                         {'Approve license request %s' % req.name})
        with self.assertRaises(AccessError):
            req.action_approve()
        req = req.with_user(self.approver)
        self.assertTrue(req.is_admin)
        with patch(GRAPH_PATH, side_effect=graph_stub) as graph:
            req.action_approve()
        self.assertEqual((req.state, req.approver_id, req.error_message),
                         ('done', self.approver, False))
        self.assertTrue(req.executed_date)
        self.assertFalse(req.sudo().activity_ids)
        self.assertEqual(self.adele.sku_ids, self.sku)
        # Assigned, then the inventory is synchronised
        assign, sync = graph.call_args_list
        self.assertEqual(assign.args, ('POST', '/users/%s/assignLicense' % USER_ID))
        self.assertEqual(assign.kwargs['json_body'], {
            'addLicenses': [{'skuId': GUID_A, 'disabledPlans': []}], 'removeLicenses': []})
        self.assertEqual(sync.args, ('GET', '/subscribedSkus'))
        log = self._logs('assign_license')
        self.assertEqual((log.status, log.request_id, log.user_id), ('success', req, self.approver))
        self.assertEqual(log.message, '%s - Adele Vance / Microsoft 365 E3 completed.' % req.name)

    def test_auto_approve_admins(self):
        admin_env = self.env(user=self.approver)
        # Disabled: the requests of administrators wait for approval as well
        req = self._request(env=admin_env)
        req.action_submit()
        self.assertEqual(req.state, 'submitted')
        self.tenant.auto_approve_admins = True
        removal = self._request(self.megan, 'remove', env=admin_env)
        with patch(GRAPH_PATH, side_effect=graph_stub) as graph:
            removal.action_submit()
        self.assertEqual((removal.state, removal.approver_id), ('done', self.approver))
        self.assertEqual(graph.call_args_list[0].args,
                         ('POST', '/users/%s/assignLicense' % USER_ID_2))
        self.assertEqual(graph.call_args_list[0].kwargs['json_body'],
                         {'addLicenses': [], 'removeLicenses': [GUID_A]})
        self.assertFalse(self.megan.sku_ids)
        self.assertEqual(self._logs('remove_license').status, 'success')
        # Never for users
        user_req = self._request(self.megan, env=self.env(user=self.requester))
        user_req.action_submit()
        self.assertEqual(user_req.state, 'submitted')

    def test_submit_checks(self):
        assign = self._request()
        with self.assertRaisesRegex(UserError, 'Adele Vance does not have Microsoft 365 E3.'):
            self._request(request_type='remove').action_submit()
        with self.assertRaisesRegex(UserError, 'Megan Bowen already has Microsoft 365 E3.'):
            self._request(self.megan).action_submit()
        # Users read from Microsoft 365 need a usage location
        self.adele.write({'last_sync': fields.Datetime.now(), 'usage_location': False})
        with self.assertRaisesRegex(UserError, 'Adele Vance has no usage location.'):
            assign.action_submit()
        self.sku.consumed_units = 10
        with self.assertRaisesRegex(UserError, 'No available Microsoft 365 E3 license left'):
            assign.action_submit()
        self.tenant.state = 'draft'
        with self.assertRaisesRegex(UserError, 'Test the connection of tenant Contoso first.'):
            assign.action_submit()
        self.tenant.allow_license_operations = False
        with self.assertRaisesRegex(UserError, 'License operations are disabled for tenant '
                                               'Contoso.'):
            assign.action_submit()
        self.assertEqual(assign.state, 'draft')

    def test_duplicate_pending_request(self):
        first = self._request()
        first.action_submit()
        with self.assertRaisesRegex(UserError, 'A similar request is already pending'):
            self._request().action_submit()
        # Possible again once the first one is rejected
        first.action_reject()
        second = self._request()
        second.action_submit()
        self.assertEqual(second.state, 'submitted')

    def test_reject_and_reset(self):
        req = self._request(env=self.env(user=self.requester))
        req.action_submit()
        with self.assertRaises(AccessError):
            req.action_reject()
        req = req.with_user(self.approver)
        req.reject_reason = 'Budget freeze'
        req.action_reject()
        self.assertEqual((req.state, req.approver_id), ('rejected', self.approver))
        self.assertFalse(req.sudo().activity_ids)
        self.assertEqual(self._logs('request')[0].message, '%s rejected by %s. Budget freeze'
                         % (req.name, self.approver.name))
        # Rejected requests are not approved, they are reset to draft first
        with patch(GRAPH_PATH, side_effect=graph_stub) as graph:
            req.action_approve()
        self.assertFalse(graph.called)
        self.assertEqual(req.state, 'rejected')
        req.with_user(self.requester).action_reset_draft()
        self.assertEqual((req.state, req.approver_id), ('draft', self.env['res.users']))
        # Requests in progress are not reset
        req.action_submit()
        req.action_reset_draft()
        self.assertEqual(req.state, 'submitted')

    def test_failure_and_retry(self):
        req = self._request()
        req.action_submit()
        error = 'HTTP 400 - Request_BadRequest: License assignment failed.'
        with patch(GRAPH_PATH, side_effect=M365GraphError(error)):
            req.action_approve()
        self.assertEqual((req.state, req.error_message), ('failed', error))
        self.assertFalse(self.adele.sku_ids)
        log = self._logs('assign_license')
        self.assertEqual((log.status, log.message),
                         ('failed', '%s - Adele Vance / Microsoft 365 E3: %s' % (req.name, error)))
        with self.assertRaises(AccessError):
            req.with_user(self.requester).action_retry()

        # The license is assigned even if the inventory cannot be synchronised afterwards
        def graph(method, path, **kw):
            if path == '/subscribedSkus':
                raise M365GraphError('HTTP 503 - ServiceUnavailable')
            return {}

        with patch(GRAPH_PATH, side_effect=graph):
            req.action_retry()
        self.assertEqual((req.state, req.error_message), ('done', False))
        self.assertEqual(self.adele.sku_ids, self.sku)

    def test_operations_disabled_after_approval(self):
        req = self._request()
        req.action_submit()
        self.tenant.allow_license_operations = False
        with patch(GRAPH_PATH) as graph:
            req.action_approve()
        self.assertFalse(graph.called)
        self.assertEqual((req.state, req.error_message),
                         ('failed', 'License operations are disabled for this tenant.'))

    def test_same_tenant(self):
        other = self.env['m365.tenant'].create({
            'name': 'Fabrikam', 'domain': 'fabrikam.com', 'tenant_id': 'tid-fabrikam',
            'client_id': 'cid'})
        other_sku = self.env['m365.sku'].create({
            'tenant_id': other.id, 'sku_id': GUID_B, 'sku_part_number': 'SPE_E5'})
        with self.assertRaisesRegex(ValidationError, 'must belong to the selected tenant'):
            self._request(sku_id=other_sku.id)
        req = self.env['m365.license.request'].new({
            'tenant_id': self.tenant.id, 'user_id': self.adele.id, 'sku_id': self.sku.id})
        req._onchange_tenant_id()
        self.assertEqual((req.user_id, req.sku_id), (self.adele, self.sku))
        req.tenant_id = other
        req._onchange_tenant_id()
        self.assertFalse(req.user_id or req.sku_id)

    def test_users_see_their_own_requests(self):
        own = self._request(env=self.env(user=self.requester))
        other = self._request(self.megan, 'remove', env=self.env(user=self.approver))
        Request = self.env['m365.license.request']
        domain = [('tenant_id', '=', self.tenant.id)]
        self.assertEqual(Request.with_user(self.requester).search(domain), own)
        self.assertEqual(Request.with_user(self.approver).search(domain), own | other)

    def test_create_from_dashboard(self):
        Request = self.env['m365.license.request'].with_user(self.requester)
        result = Request.create_from_dashboard(self.tenant.id, self.adele.id, self.sku.id,
                                               'assign', 'Migration project')
        req = Request.browse(result['id'])
        self.assertEqual(result, {'id': req.id, 'name': req.name, 'state': 'submitted',
                                  'error': ''})
        self.assertEqual((req.request_type, req.reason), ('assign', 'Migration project'))
