# -*- coding: utf-8 -*-
"""REST API of the Microsoft 365 License Management module.

Base URL: ``/m365/api/v1``

Authentication: an Odoo API key sent as ``Authorization: Bearer <key>``
(Preferences > Account Security > New API Key). Every call runs with the
access rights of the key owner, so Odoo's ACLs and record rules apply.
Responses are JSON: ``{"ok": true, "data": ...}`` or
``{"ok": false, "error": "..."}`` with a matching HTTP status.
"""
import functools
import json
import logging

from odoo import _, http
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)

API = '/m365/api/v1'
ADMIN_GROUP = 'm365_license_management.group_m365_admin'
MAX_LIMIT = 200


def _response(payload, status=200):
    return request.make_response(
        json.dumps(payload, default=str),
        headers=[('Content-Type', 'application/json'), ('Cache-Control', 'no-store')],
        status=status,
    )


def api_endpoint(func):
    """Convert exceptions into JSON error responses."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            data = func(*args, **kwargs)
            return _response({'ok': True, 'data': data})
        except AccessError as exc:
            return _response({'ok': False, 'error': str(exc)}, 403)
        except MissingError as exc:
            return _response({'ok': False, 'error': str(exc)}, 404)
        except (UserError, ValidationError, ValueError) as exc:
            # M365GraphError is a UserError as well
            return _response({'ok': False, 'error': str(exc)}, 400)
        except Exception:
            _logger.exception("Microsoft 365 API error")
            return _response({'ok': False, 'error': _("Internal server error.")}, 500)
    return wrapper


def _body():
    try:
        data = request.get_json_data()
    except Exception:
        raise ValueError(_("The request body must be valid JSON."))
    if not isinstance(data, dict):
        raise ValueError(_("The request body must be a JSON object."))
    return data


def _require_admin():
    if not request.env.user.has_group(ADMIN_GROUP):
        raise AccessError(_("Only Microsoft 365 administrators can perform this action."))


def _int(value, name, default=None):
    if value in (None, ''):
        if default is None:
            raise ValueError(_("%s is required.", name))
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(_("%s must be an integer.", name))


def _page(params):
    limit = min(max(_int(params.get('limit'), 'limit', 50), 1), MAX_LIMIT)
    offset = max(_int(params.get('offset'), 'offset', 0), 0)
    return limit, offset


def _browse(model, rec_id):
    rec = request.env[model].browse(rec_id).exists()
    if not rec:
        raise MissingError(_("Record not found."))
    rec.check_access('read')
    return rec


# ----------------------------------------------------------------------
# Serialisers
# ----------------------------------------------------------------------
def _dt(value):
    return value.isoformat(sep=' ') if value else None


def _tenant(t):
    return {
        'id': t.id, 'name': t.name, 'domain': t.domain, 'tenant_type': t.tenant_type,
        'state': t.state,
        'allow_license_operations': t.allow_license_operations,
        'sync_enabled': t.sync_enabled,
        'last_sync': _dt(t.last_sync_date), 'last_user_sync': _dt(t.last_user_sync_date),
        'sku_count': t.sku_count, 'user_count': t.user_count,
        'total_licenses': t.total_licenses, 'assigned_licenses': t.assigned_licenses,
        'available_licenses': t.available_licenses,
    }


def _sku(s):
    return {
        'id': s.id, 'tenant_id': s.tenant_id.id, 'sku_id': s.sku_id,
        'sku_part_number': s.sku_part_number, 'name': s.name, 'status': s.status,
        'total': s.enabled_units, 'assigned': s.consumed_units,
        'available': s.available_units, 'suspended': s.suspended_units,
        'expiring': s.warning_units, 'utilization': s.utilization,
        'last_refresh': _dt(s.last_refresh),
    }


def _user(u):
    return {
        'id': u.id, 'tenant_id': u.tenant_id.id, 'graph_id': u.graph_id, 'name': u.name,
        'user_principal_name': u.user_principal_name, 'mail': u.mail,
        'department': u.department, 'job_title': u.job_title,
        'usage_location': u.usage_location, 'account_enabled': u.account_enabled,
        'active': u.active, 'last_sync': _dt(u.last_sync),
        'licenses': [{'id': s.id, 'sku_part_number': s.sku_part_number, 'name': s.name}
                     for s in u.sku_ids],
    }


def _req(r):
    return {
        'id': r.id, 'reference': r.name, 'tenant_id': r.tenant_id.id,
        'type': r.request_type, 'state': r.state,
        'user': {'id': r.user_id.id, 'name': r.user_id.name},
        'sku': {'id': r.sku_id.id, 'name': r.sku_id.name},
        'requested_by': r.requested_by.name, 'approver': r.approver_id.name or None,
        'reason': r.reason or None, 'reject_reason': r.reject_reason or None,
        'error': r.error_message or None, 'executed_date': _dt(r.executed_date),
        'created': _dt(r.create_date),
    }


def _log(l):
    return {
        'id': l.id, 'date': _dt(l.date), 'tenant_id': l.tenant_id.id, 'action': l.action,
        'status': l.status, 'message': l.message, 'user': l.user_id.name,
        'request_id': l.request_id.id or None,
    }


class M365ApiController(http.Controller):

    # ------------------------------------------------------------------
    # Session / health
    # ------------------------------------------------------------------
    @http.route(API + '/me', type='http', auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def me(self, **kw):
        user = request.env.user
        return {'id': user.id, 'name': user.name, 'login': user.login,
                'is_admin': user.has_group(ADMIN_GROUP),
                'is_m365_user': user.has_group('m365_license_management.group_m365_user')}

    # ------------------------------------------------------------------
    # Tenants
    # ------------------------------------------------------------------
    @http.route(API + '/tenants', type='http', auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def tenants(self, **kw):
        Tenant = request.env['m365.tenant']
        return [_tenant(t) for t in Tenant.search(Tenant._live_domain())]

    @http.route(API + '/tenants/<int:tenant_id>', type='http', auth='bearer',
                methods=['GET'], csrf=False)
    @api_endpoint
    def tenant(self, tenant_id, **kw):
        return _tenant(_browse('m365.tenant', tenant_id))

    @http.route(API + '/tenants/<int:tenant_id>/test', type='http', auth='bearer',
                methods=['POST'], csrf=False)
    @api_endpoint
    def tenant_test(self, tenant_id, **kw):
        _require_admin()
        tenant = _browse('m365.tenant', tenant_id)
        org = tenant.graph_get_organization()
        tenant.write({'state': 'connected', 'last_error': False})
        tenant._log('test_connection', 'success', _("Connected via API."))
        return {'organization': org.get('displayName'), 'tenant': _tenant(tenant)}

    @http.route(API + '/tenants/<int:tenant_id>/sync', type='http', auth='bearer',
                methods=['POST'], csrf=False)
    @api_endpoint
    def tenant_sync(self, tenant_id, **kw):
        _require_admin()
        tenant = _browse('m365.tenant', tenant_id)
        ok, messages = tenant._sync_all_safe()
        if not ok:
            raise UserError("\n".join(messages))
        return {'messages': messages, 'tenant': _tenant(tenant)}

    @http.route(API + '/tenants/<int:tenant_id>/service_plans', type='http',
                auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def tenant_service_plans(self, tenant_id, **kw):
        """Live service plans of every subscribed SKU (Microsoft Graph)."""
        _require_admin()
        tenant = _browse('m365.tenant', tenant_id)
        return [{
            'sku_id': s.get('skuId'), 'sku_part_number': s.get('skuPartNumber'),
            'service_plans': [{'id': p.get('servicePlanId'), 'name': p.get('servicePlanName'),
                               'applies_to': p.get('appliesTo')}
                              for p in s.get('servicePlans', [])],
        } for s in tenant.graph_get_subscribed_skus()]

    # ------------------------------------------------------------------
    # Subscriptions (SKUs)
    # ------------------------------------------------------------------
    @http.route(API + '/skus', type='http', auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def skus(self, **kw):
        domain = []
        if kw.get('tenant_id'):
            domain.append(('tenant_id', '=', _int(kw['tenant_id'], 'tenant_id')))
        if kw.get('available_only'):
            domain.append(('available_units', '>', 0))
        limit, offset = _page(kw)
        Sku = request.env['m365.sku']
        return {'total': Sku.search_count(domain),
                'items': [_sku(s) for s in Sku.search(domain, limit=limit, offset=offset)]}

    @http.route(API + '/skus/<int:sku_id>/history', type='http', auth='bearer',
                methods=['GET'], csrf=False)
    @api_endpoint
    def sku_history(self, sku_id, **kw):
        sku = _browse('m365.sku', sku_id)
        limit = min(_int(kw.get('limit'), 'limit', 90), 365)
        snaps = request.env['m365.inventory.snapshot'].search(
            [('sku_id', '=', sku.id)], order='snapshot_date desc', limit=limit)
        return {'sku': _sku(sku), 'history': [{
            'date': _dt(s.snapshot_date), 'total': s.enabled_units,
            'assigned': s.consumed_units, 'available': s.available_units,
        } for s in snaps]}

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    @http.route(API + '/users', type='http', auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def users(self, **kw):
        domain = []
        if kw.get('tenant_id'):
            domain.append(('tenant_id', '=', _int(kw['tenant_id'], 'tenant_id')))
        if kw.get('search'):
            domain += ['|', ('name', 'ilike', kw['search']),
                       ('user_principal_name', 'ilike', kw['search'])]
        if kw.get('sku_id'):
            domain.append(('sku_ids', 'in', _int(kw['sku_id'], 'sku_id')))
        if kw.get('licensed') in ('1', 'true'):
            domain.append(('license_count', '>', 0))
        elif kw.get('licensed') in ('0', 'false'):
            domain.append(('license_count', '=', 0))
        limit, offset = _page(kw)
        User = request.env['m365.license.user']
        return {'total': User.search_count(domain),
                'items': [_user(u) for u in User.search(domain, limit=limit, offset=offset)]}

    @http.route(API + '/users/<int:user_id>', type='http', auth='bearer',
                methods=['GET'], csrf=False)
    @api_endpoint
    def user(self, user_id, **kw):
        user = _browse('m365.license.user', user_id)
        data = _user(user)
        if kw.get('live') in ('1', 'true'):
            _require_admin()
            data['license_details'] = user.get_live_license_details()
        return data

    @http.route(API + '/users/<int:user_id>/refresh', type='http', auth='bearer',
                methods=['POST'], csrf=False)
    @api_endpoint
    def user_refresh(self, user_id, **kw):
        user = _browse('m365.license.user', user_id)
        user.action_refresh_from_graph()
        return _user(user)

    @http.route(API + '/users/<int:user_id>/usage_location', type='http', auth='bearer',
                methods=['POST'], csrf=False)
    @api_endpoint
    def user_usage_location(self, user_id, **kw):
        user = _browse('m365.license.user', user_id)
        user.set_usage_location(_body().get('country_code'))
        return _user(user)

    # ------------------------------------------------------------------
    # License requests
    # ------------------------------------------------------------------
    @http.route(API + '/requests', type='http', auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def requests_list(self, **kw):
        domain = []
        if kw.get('state'):
            domain.append(('state', '=', kw['state']))
        if kw.get('tenant_id'):
            domain.append(('tenant_id', '=', _int(kw['tenant_id'], 'tenant_id')))
        limit, offset = _page(kw)
        Req = request.env['m365.license.request']
        return {'total': Req.search_count(domain),
                'items': [_req(r) for r in Req.search(domain, limit=limit, offset=offset)]}

    @http.route(API + '/requests/<int:req_id>', type='http', auth='bearer',
                methods=['GET'], csrf=False)
    @api_endpoint
    def request_get(self, req_id, **kw):
        return _req(_browse('m365.license.request', req_id))

    @http.route(API + '/requests', type='http', auth='bearer', methods=['POST'], csrf=False)
    @api_endpoint
    def request_create(self, **kw):
        """Body: tenant_id, user_id, sku_id, type (assign|remove), reason."""
        body = _body()
        req_type = body.get('type', 'assign')
        if req_type not in ('assign', 'remove'):
            raise ValueError(_("type must be 'assign' or 'remove'."))
        rec = request.env['m365.license.request'].create({
            'tenant_id': _int(body.get('tenant_id'), 'tenant_id'),
            'user_id': _int(body.get('user_id'), 'user_id'),
            'sku_id': _int(body.get('sku_id'), 'sku_id'),
            'request_type': req_type,
            'reason': body.get('reason') or False,
        })
        rec.action_submit()
        return _req(rec)

    def _workflow(self, req_id, method, **write_vals):
        rec = _browse('m365.license.request', req_id)
        if write_vals:
            rec.write(write_vals)
        getattr(rec, method)()
        return _req(rec)

    @http.route(API + '/requests/<int:req_id>/approve', type='http', auth='bearer',
                methods=['POST'], csrf=False)
    @api_endpoint
    def request_approve(self, req_id, **kw):
        return self._workflow(req_id, 'action_approve')

    @http.route(API + '/requests/<int:req_id>/reject', type='http', auth='bearer',
                methods=['POST'], csrf=False)
    @api_endpoint
    def request_reject(self, req_id, **kw):
        _require_admin()
        return self._workflow(req_id, 'action_reject',
                              reject_reason=_body().get('reason') or False)

    @http.route(API + '/requests/<int:req_id>/retry', type='http', auth='bearer',
                methods=['POST'], csrf=False)
    @api_endpoint
    def request_retry(self, req_id, **kw):
        return self._workflow(req_id, 'action_retry')

    # ------------------------------------------------------------------
    # Dashboard / audit
    # ------------------------------------------------------------------
    @http.route(API + '/dashboard', type='http', auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def dashboard(self, **kw):
        tenant_ids = [_int(i, 'tenant_ids') for i in kw['tenant_ids'].split(',')] \
            if kw.get('tenant_ids') else None
        return request.env['m365.dashboard'].get_dashboard_data(
            tenant_ids=tenant_ids, date_range=_int(kw.get('days'), 'days', 30))

    @http.route(API + '/audit', type='http', auth='bearer', methods=['GET'], csrf=False)
    @api_endpoint
    def audit(self, **kw):
        _require_admin()
        domain = []
        if kw.get('tenant_id'):
            domain.append(('tenant_id', '=', _int(kw['tenant_id'], 'tenant_id')))
        if kw.get('status'):
            domain.append(('status', '=', kw['status']))
        limit, offset = _page(kw)
        Log = request.env['m365.audit.log']
        return {'total': Log.search_count(domain),
                'items': [_log(l) for l in Log.search(domain, limit=limit, offset=offset)]}
