# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import patch

import requests

from odoo import fields
from odoo.tests import tagged
from odoo.tools import mute_logger

from ..models.m365_tenant import M365GraphError
from .common import GRAPH_PATH, GUID_A, GUID_B, MODELS, USER_ID, M365Common, http_response

TOKEN_POST = MODELS + '.m365_tenant.requests.post'
HTTP_CALL = MODELS + '.m365_tenant.requests.request'
TIME = MODELS + '.m365_tenant.time'


@tagged('post_install', '-at_install')
class TestM365GraphClient(M365Common):

    def _set_token(self, token='tok', minutes=60):
        self.tenant.sudo().write({
            'access_token': token,
            'token_expiry': fields.Datetime.now() + timedelta(minutes=minutes),
        })

    # ------------------------------------------------------------------
    # OAuth 2.0 client credentials
    # ------------------------------------------------------------------
    def test_access_token_cached(self):
        token = http_response(200, {'access_token': 'tok-1', 'expires_in': 3600})
        with patch(TOKEN_POST, return_value=token) as post:
            self.assertEqual(self.tenant._get_access_token(), 'tok-1')
            self.assertEqual(self.tenant._get_access_token(), 'tok-1')
        post.assert_called_once()
        self.assertEqual(post.call_args.args,
                         ('https://login.microsoftonline.com/tid-contoso/oauth2/v2.0/token',))
        self.assertEqual(post.call_args.kwargs['data'], {
            'client_id': 'cid', 'client_secret': 'secret',
            'scope': 'https://graph.microsoft.com/.default', 'grant_type': 'client_credentials',
        })
        # A token expiring within 5 minutes is renewed, as asked
        self._set_token('tok-1', minutes=4)
        with patch(TOKEN_POST, return_value=http_response(200, {'access_token': 'tok-2'})):
            self.assertEqual(self.tenant._get_access_token(), 'tok-2')
        self._set_token('tok-2')
        with patch(TOKEN_POST, return_value=http_response(200, {'access_token': 'tok-3'})):
            self.assertEqual(self.tenant._get_access_token(force=True), 'tok-3')

    def test_access_token_errors(self):
        self.tenant.client_secret = False
        with self.assertRaisesRegex(M365GraphError, 'Client Secret are required'):
            self.tenant._get_access_token()
        self.tenant.client_secret = 'wrong'
        denied = http_response(401, {'error': 'invalid_client',
                                     'error_description': 'AADSTS7000215: Invalid secret.'})
        with patch(TOKEN_POST, return_value=denied), self.assertRaisesRegex(
                M365GraphError, 'Authentication failed. HTTP 401 - invalid_client: AADSTS7000215'):
            self.tenant._get_access_token()
        with patch(TOKEN_POST, side_effect=requests.ConnectionError('Name resolution failed')), \
                self.assertRaisesRegex(M365GraphError, 'Unable to reach .*Name resolution failed'):
            self.tenant._get_access_token()

    def test_credentials_change_resets_connection(self):
        self._set_token()
        self.tenant.write({'name': 'Contoso Ltd', 'sync_enabled': False})
        self.assertEqual((self.tenant.state, self.tenant.sudo().access_token), ('connected', 'tok'))
        self.tenant.client_secret = 'new-secret'
        self.assertEqual(self.tenant.state, 'draft')
        self.assertFalse(self.tenant.sudo().access_token)
        self.assertFalse(self.tenant.sudo().token_expiry)

    def test_format_error(self):
        Tenant = self.env['m365.tenant']
        graph = http_response(403, {'error': {'code': 'Authorization_RequestDenied',
                                              'message': 'Insufficient privileges.'}})
        self.assertEqual(Tenant._format_error(graph),
                         'HTTP 403 - Authorization_RequestDenied: Insufficient privileges.')
        identity = http_response(400, {'error': 'invalid_request'})
        self.assertEqual(Tenant._format_error(identity), 'HTTP 400 - invalid_request: ')
        gateway = http_response(502, b'<html>Bad gateway</html>')
        self.assertEqual(Tenant._format_error(gateway), 'HTTP 502: <html>Bad gateway</html>')

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    def test_graph_request_paging(self):
        self._set_token()
        pages = [
            http_response(200, {'value': [{'id': 1}], '@odata.nextLink':
                                'https://graph.microsoft.com/v1.0/users?$skiptoken=X'}),
            http_response(200, {'value': [{'id': 2}]}),
        ]
        with patch(HTTP_CALL, side_effect=pages) as call:
            result = self.tenant._graph_request('GET', '/users', params={'$top': '1'},
                                                paginate=True)
        self.assertEqual(result, [{'id': 1}, {'id': 2}])
        first, second = call.call_args_list
        self.assertEqual(first.args, ('GET', 'https://graph.microsoft.com/v1.0/users'))
        self.assertEqual(first.kwargs['params'], {'$top': '1'})
        self.assertEqual(first.kwargs['headers']['Authorization'], 'Bearer tok')
        # The next link already contains the query parameters
        self.assertEqual(second.args[1], 'https://graph.microsoft.com/v1.0/users?$skiptoken=X')
        self.assertIsNone(second.kwargs['params'])

    def test_graph_request_retries(self):
        self._set_token()
        responses = [
            http_response(429, headers={'Retry-After': '7'}),
            http_response(503, headers={'Retry-After': 'soon'}),
            http_response(504, headers={'Retry-After': '120'}),
            http_response(401),
            http_response(204),
        ]
        new_token = http_response(200, {'access_token': 'tok-new'})
        with patch(HTTP_CALL, side_effect=responses) as call, patch(TIME) as mocked_time, \
                patch(TOKEN_POST, return_value=new_token) as post:
            result = self.tenant._graph_request('PATCH', '/users/x', json_body={'a': 1})
        self.assertEqual(result, {})
        # Throttled: wait as asked, 2 seconds when unreadable, 30 seconds at most
        self.assertEqual([c.args[0] for c in mocked_time.sleep.call_args_list], [7, 2, 30])
        # Token rejected: renewed once and the call repeated
        post.assert_called_once()
        self.assertEqual(call.call_count, 5)
        self.assertEqual(call.call_args.kwargs['headers']['Authorization'], 'Bearer tok-new')
        self.assertEqual(call.call_args.kwargs['json'], {'a': 1})

    def test_graph_request_errors(self):
        self._set_token()
        expired = http_response(401, {'error': {'code': 'InvalidAuthenticationToken',
                                                'message': 'Token expired.'}})
        new_token = http_response(200, {'access_token': 'tok-new'})
        with patch(HTTP_CALL, return_value=expired) as call, \
                patch(TOKEN_POST, return_value=new_token), \
                self.assertRaisesRegex(M365GraphError, 'HTTP 401 - InvalidAuthenticationToken'):
            self.tenant._graph_request('GET', '/users')
        self.assertEqual(call.call_count, 2)
        # Throttling: gives up after 4 retries
        with patch(HTTP_CALL, return_value=http_response(429)) as call, patch(TIME), \
                self.assertRaisesRegex(M365GraphError, 'HTTP 429'):
            self.tenant._graph_request('GET', '/users')
        self.assertEqual(call.call_count, 5)
        with patch(HTTP_CALL, side_effect=requests.Timeout('Read timed out')), \
                self.assertRaisesRegex(M365GraphError, 'request failed: Read timed out'):
            self.tenant._graph_request('GET', '/users')

    # ------------------------------------------------------------------
    # Graph operations
    # ------------------------------------------------------------------
    def test_graph_operations(self):
        with patch(GRAPH_PATH, return_value={}) as graph:
            self.tenant.graph_assign_license('adele.vance@contoso.com', add_sku_ids=[GUID_A],
                                             remove_sku_ids=[GUID_B],
                                             disabled_plans={GUID_A: [GUID_B]})
            self.tenant.graph_update_user(' %s ' % USER_ID, {'usageLocation': 'IN'})
        assign, update = graph.call_args_list
        # User principal names are URL encoded
        self.assertEqual(assign.args, ('POST', '/users/adele.vance%40contoso.com/assignLicense'))
        self.assertEqual(assign.kwargs['json_body'], {
            'addLicenses': [{'skuId': GUID_A, 'disabledPlans': [GUID_B]}],
            'removeLicenses': [GUID_B],
        })
        self.assertEqual(update.args, ('PATCH', '/users/%s' % USER_ID))
        self.assertEqual(update.kwargs['json_body'], {'usageLocation': 'IN'})
        # _graph_key is a static method: the message is not translated
        with mute_logger('odoo.tools.translate'), \
                self.assertRaisesRegex(M365GraphError, 'user identifier is required'):
            self.tenant.graph_get_user('  ')
        self.tenant.allow_license_operations = False
        with self.assertRaisesRegex(M365GraphError,
                                    'License operations are disabled for tenant Contoso'):
            self.tenant.graph_update_user(USER_ID, {'usageLocation': 'IN'})

    def test_graph_organization(self):
        org = {'id': 'org', 'displayName': 'Contoso Ltd'}
        with patch(GRAPH_PATH, return_value=[org]) as graph:
            self.assertEqual(self.tenant.graph_get_organization(), org)
        self.assertEqual(graph.call_args.args, ('GET', '/organization'))
        with patch(GRAPH_PATH, return_value=[]):
            self.assertEqual(self.tenant.graph_get_organization(), {})
