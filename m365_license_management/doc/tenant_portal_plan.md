# Portal Tenant Self-Service: Implementation

Portal users create new Microsoft 365 tenants and connect existing ones from the portal
(*My Account › Microsoft 365 Tenants*, `/my/m365/tenants`), one by one or from a CSV /
Excel file, without an administrator.

Status: implemented in module version 19.0.1.5.0. The backend flow is unchanged: the
portal runs the methods of the backend buttons, and every existing test passes unchanged.

## 1. Processes

|                   | Single                                                     | Bulk (CSV / Excel)                                                    |
|-------------------|------------------------------------------------------------|-----------------------------------------------------------------------|
| **New tenant**    | Form › Save & Check › Create in Microsoft 365 › sign-in › Connect | `Customers` file › Validate › Import › checked in background › Create Selected |
| **Existing tenant** | Form › Save & Test Connection                            | Tenant file › Validate › Import › tested in background                |

New tenants are created with the Partner Center account of the backend (the first active
account of the user's company). Decisions: no approval step, each portal user sees only
the tenants they added, no limit of tenants per user.

## 2. Portal steps and backend methods

| Portal step                         | Backend method (unchanged)                                            |
|-------------------------------------|-----------------------------------------------------------------------|
| Save a tenant                       | `m365.tenant.create()` (type New or Existing, same constraints)       |
| Field checks of the forms           | `m365.tenant.import.wizard._convert()`, unique name / domain / Directory ID |
| Check (New)                         | `action_check`                                                        |
| Create in Microsoft 365, Retry      | `action_create`, then the scheduled action *Create Queued Tenants*    |
| Reset to To Check                   | `action_reset_creation`                                               |
| I have saved the password           | `action_clear_password`                                               |
| Connect, Test Connection            | write of the app registration, `action_test_connection`               |
| Bulk validate, templates            | `m365.tenant.import.wizard._parse()`, `action_download_template()`    |

`action_check`, `action_create` and `action_clear_password` check the Microsoft 365
Administrator group: the portal runs them as OdooBot (an administrator), on the tenants of
the user only, then sets `queued_by` to the portal user so that the audit log names them.
`action_test_connection` runs as sudo with the portal user, who is named in the audit log.

## 3. Files

| File | Content |
|---|---|
| `models/m365_tenant_portal.py` | Fields `portal_user_id`, `portal_job`; form checks, steps, background job |
| `controllers/portal.py` | Routes of `/my/m365/tenants` |
| `views/m365_portal_templates.xml` | Home card, list, tenant page, forms, bulk import (no JavaScript) |
| `views/m365_tenant_portal_views.xml` | Backend: *Portal User* field (read only), *Portal Tenants* filter |
| `security/m365_portal_security.xml` | Group and record rule |
| `data/m365_portal_data.xml` | Scheduled action, `m365.portal_import_max_rows`, grant / revoke actions |
| `tests/test_m365_portal.py` | 16 tests |

Existing files only got registration lines: manifest (version, `portal` dependency, data
files), the `__init__.py` files and one line of `ir.model.access.csv`.

## 4. Routes

| Route | Purpose |
|---|---|
| `GET /my/m365/tenants` | List: search, filters, row selection |
| `POST /my/m365/tenants/batch` | `check` / `test` (background) or `create` on the selected tenants |
| `GET/POST /my/m365/tenants/new?tenant_type=new\|existing` | Single form |
| `GET /my/m365/tenants/<id>` | Tenant page: status, errors, data, steps of its status |
| `GET/POST /my/m365/tenants/<id>/edit` | Customer data of a new tenant, then Check |
| `GET/POST /my/m365/tenants/<id>/connect` | App registration, then Test Connection |
| `POST /my/m365/tenants/<id>/check\|create\|reset\|test\|remove\|password` | Steps |
| `GET/POST /my/m365/tenants/import` | Upload and validate a file (nothing is added) |
| `POST /my/m365/tenants/import/submit` | Add all rows of the file, or none |
| `GET /my/m365/tenants/import/template?tenant_type=new\|existing` | Template (.xlsx) |

Background job: *Microsoft 365: Portal Background Jobs* (`ir_cron_m365_portal_jobs`),
triggered by the portal, 20 tenants per run, hourly to resume interrupted work. A page
with a tenant being checked, tested or created refreshes itself every 15 seconds.

## 5. Security

- Group **Microsoft 365: Portal Tenant Self-Service** (`group_m365_portal`), implies the
  portal; Odoo refuses it for internal users. Given or removed by Microsoft 365
  administrators: *Settings › Users*, select portal users, *Actions › Microsoft 365: Allow
  / Remove Portal Tenant Self-Service*.
- Record rule `[('portal_user_id', '=', user.id)]` for that group, read access only. The
  routes check the group, read the tenant as the user (others' tenants: 404), then write
  as sudo from a whitelist of fields. CSRF on every POST.
- Administrator fields stay hidden (`groups`): Partner Center account, client secret,
  admin user and password. The portal never shows the secret back ("Saved"). The initial
  admin password of a created tenant is shown to its owner until they press *I have saved
  the password*.
- The portal user is not subscribed to the tenant: the chatter stays internal.
- Files: .csv or .xlsx, 2 MB, at most `m365.portal_import_max_rows` rows (default 100,
  0 = no limit), only the columns of the template. Remove archives the tenant (new
  tenants before creation, existing tenants), so the audit log is kept.

## 6. Setup and user guide

1. Backend: *Tenants › Partner Center Account* configured and tested (for new tenants).
2. Give portal access to the customer contact (*Contacts › Action › Grant portal access*),
   then the self-service group (section 5).
3. The portal user opens *My Account › Microsoft 365 Tenants*:
   - **Add Existing Tenant**: name, domain, Directory ID, client ID and secret of an app
     registered in the tenant (application permissions `Organization.Read.All`,
     `User.Read.All`, admin consent) › *Save & Test Connection*.
   - **Create New Tenant**: company, contact, registered address › *Save & Check* › fix
     the errors › *Create in Microsoft 365* › copy the admin user and password ›
     *I have saved the password* › *Connect* with an app registered in the new tenant.
   - **Bulk Import**: choose the type, download the template, upload the file, fix the
     rows with errors, *Import*. New tenants: select the *Ready* ones › *Create in
     Microsoft 365*. Existing tenants without secret: *Connect* each one.
4. Connected tenants are synchronised by the backend like any tenant.

## 7. Testing

```
odoo-bin -c <config> -d <test database> -u m365_license_management \
         --test-tags /m365_license_management --stop-after-init
```

`tests/test_m365_portal.py`: access and record rule, group (exclusive with internal users,
grant / revoke actions), form checks of both types, New tenant flow to Connected with the
audit log user, failed creation, Existing tenant connection errors, background jobs and
batching, bulk files (columns, row limit, templates), and the HTTP routes of every step.

Manual acceptance with the Partner Center **integration sandbox**, as a portal user:
create and connect a new tenant, add an existing tenant, import 3 rows of each type with
one invalid row (nothing added), fix and import again, then check that a second portal
user does not see these tenants.
