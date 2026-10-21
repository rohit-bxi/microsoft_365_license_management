# -*- coding: utf-8 -*-
{
    'name': 'Microsoft 365 License Management',
    'version': '19.0.1.2.0',
    'category': 'Administration',
    'summary': 'Multi-tenant Microsoft 365 license inventory, dashboard, '
               'approval based assignment and audit via Microsoft Graph',
    'description': """
Microsoft 365 License Management
================================
Centralised license inventory dashboard and administrative portal for one or
more Microsoft 365 tenants, integrated through the Microsoft Graph API.

* Tenant masters (Entra app registration, OAuth 2.0 client credentials)
* Creation of new customer tenants through Microsoft Partner Center (CSP), one by
  one or in bulk from a CSV / Excel file
* Product catalogue master (SKU part number -> friendly product name)
* License inventory synchronisation (GET /subscribedSkus) with history snapshots
* User directory synchronisation (GET /users) with current licenses
* License assignment / removal requests with approval workflow
  (POST /users/{id}/assignLicense)
* REST API (/m365/api/v1, Odoo API-key bearer auth) and CSV export
* Per-user live license details, usage location update (PATCH /users/{id})
* Near real-time sync: Graph delta queries + change notification webhook
* Audit log of every successful and failed operation
* Scheduled synchronisation and low-availability notifications
* Dashboard: totals, utilisation by product, distribution, tenant comparison
    """,
    'author': 'BXI Technologies',
    'website': 'https://bxitech.com/',
    'depends': ['base', 'mail', 'web'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/m365_product_data.xml',
        'data/ir_cron_data.xml',
        'views/m365_tenant_views.xml',
        'views/m365_tenant_import_views.xml',
        'views/m365_tenant_provision_views.xml',
        'views/m365_product_views.xml',
        'views/m365_sku_views.xml',
        'views/m365_license_user_views.xml',
        'views/m365_license_request_views.xml',
        'views/m365_license_bulk_views.xml',
        'views/m365_audit_log_views.xml',
        'views/m365_snapshot_views.xml',
        'views/m365_dashboard_views.xml',
        'views/menu.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'm365_license_management/static/src/scss/m365_dashboard.scss',
            'm365_license_management/static/src/js/m365_dashboard.js',
            'm365_license_management/static/src/xml/m365_dashboard.xml',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
