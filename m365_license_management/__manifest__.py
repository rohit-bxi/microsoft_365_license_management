# -*- coding: utf-8 -*-
{
    'name': 'Microsoft 365 License Management',
    'version': '19.0.1.10.1',
    'category': 'Administration',
    'summary': 'Multi-tenant Microsoft 365 license inventory, dashboard, '
               'approval based assignment, tenant setup, MFA operations and Ingram Micro '
               'ordering',
    'description': """
Microsoft 365 License Management
================================
Centralised license inventory dashboard and administrative portal for one or
more Microsoft 365 tenants, integrated through the Microsoft Graph API.

* Tenant masters (Entra app registration, OAuth 2.0 client credentials)
* Tenants of type New: created by Ingram Micro with their first order, one by one or
  in bulk from a CSV / Excel file, then connected like existing tenants (the creation
  through Microsoft Partner Center is disabled)
* Shared multi-tenant app of the partner: tenants connect after a one-time admin
  consent, without an app registration of their own
* Tenant configuration profiles (Security Defaults, password expiry, technical contact)
  and custom domains (add, DNS records, verify, default)
* Users created from Odoo, one by one or from a file, with their license requests
* MFA operations with approval: reset MFA, per-user MFA off, Security Defaults off,
  Temporary Access Pass
* End-to-end provisioning: customer file, tenant, GDAP, Partner Center subscriptions,
  users, licenses, verification (licenseDetails) and provisioning report
* Bulk provisioning without manual steps: partner user sign-in (App+User), Microsoft
  Customer Agreement and shared app installed through Partner Center, GDAP access of a
  partner security group, custom domains added and verified automatically, users of the
  customer file created when their tenant is ready, automatic run of every step
* Licenses bought through Ingram Micro in bulk: subscription periods, default plan,
  plans found by part number or name, Ingram orders in the automatic provisioning and
  in the customer file of existing tenants
* Licenses bought from Ingram Micro (catalog, prices, orders with approval, webhook,
  delivery check in the tenant)
* New tenants created by Ingram Micro with their first order (vendor required info,
  tenant_preference new), seen in Odoo once their domain exists in Microsoft 365
* Product catalogue master (SKU part number -> friendly product name)
* License inventory synchronisation (GET /subscribedSkus) with history snapshots
* User directory synchronisation (GET /users) with current licenses
* License assignment / removal requests with approval workflow
  (POST /users/{id}/assignLicense)
* REST API (/m365/api/v1, Odoo API-key bearer auth) and CSV export
* Per-user live license details, usage location update (PATCH /users/{id})
* Near real-time sync: Graph delta queries + change notification webhook
* Audit log of every successful and failed operation
* User customer mapping: users only see the tenant customers assigned to them, and the
  records of their tenants
* Customer tenants mapping: the tenants of each tenant customer
* Tenant customers: customer master (companies, contacts, address, tax ID) linked to
  the tenants, independent of the Odoo contacts, with portal access
* Scheduled synchronisation and low-availability notifications
* Dashboard: totals, utilisation by product, distribution, tenant comparison
* Portal self-service: portal users create new tenants and connect existing
  ones, one by one or from a CSV / Excel file, and see their own tenants only
    """,
    'author': 'BXI Technologies',
    'website': 'https://bxitech.com/',
    'depends': ['base', 'mail', 'web', 'portal'],
    'data': [
        'security/security.xml',
        'security/m365_portal_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/m365_product_data.xml',
        'data/ir_cron_data.xml',
        'data/m365_mail_template_data.xml',
        'data/m365_portal_data.xml',
        'views/m365_tenant_views.xml',
        'views/m365_tenant_import_views.xml',
        'views/m365_partner_account_views.xml',
        'views/m365_product_views.xml',
        'views/m365_sku_views.xml',
        'views/m365_license_user_views.xml',
        'views/m365_license_request_views.xml',
        'views/m365_license_bulk_views.xml',
        'views/m365_audit_log_views.xml',
        'views/m365_snapshot_views.xml',
        'views/m365_dashboard_views.xml',
        'views/m365_config_views.xml',
        'views/m365_user_create_views.xml',
        'views/m365_ingram_views.xml',
        'views/m365_provisioning_views.xml',
        'views/m365_bulk_provisioning_views.xml',
        'views/customer_customer_views.xml',
        'views/customer_portal_wizard_views.xml',
        'views/m365_user_customer_mapping_views.xml',
        'views/customer_tenant_mapping_views.xml',
        'views/menu.xml',
        'views/m365_tenant_portal_views.xml',
        'views/m365_portal_templates.xml',
        'views/m365_consent_templates.xml',
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
