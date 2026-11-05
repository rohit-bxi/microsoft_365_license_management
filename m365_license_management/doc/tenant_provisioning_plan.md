# New Tenants through Partner Center: Implementation Plan

Creation of new Microsoft 365 customer tenants from Odoo, through Microsoft Partner
Center, based on the `Bulk-M365Customers.ps1` provisioning script.

Status: implemented in module version 19.0.1.2.0 and merged into the tenant master in
19.0.1.3.0 (sections 1 to 9). Section 10 lists the next phases.

**19.0.1.3.0:** the separate model `m365.tenant.provision` is removed. A tenant to create
is now an `m365.tenant` record of **Type = New**; *Existing* tenants work as before. The
upgrade moves the old records to `m365.tenant` (type New) and drops their tables.

## 1. Goal

From **Microsoft 365 › Configuration › Tenants**, a Microsoft 365 administrator can:

1. Add a tenant of type **New** (Single Tenant) or load many from a CSV / Excel file
   (Bulk Import Tenant, type New: the `Customers.csv` of the script).
2. Check each customer with Microsoft: the domain is free and the address is valid.
3. Create the tenants in Microsoft 365. Each one gets a new Microsoft Entra tenant, a
   reseller relationship with the partner and a first global administrator.
4. See the result of each customer (status, customer ID, error and timestamp, as in
   `ProvisioningResults.csv`), then connect the same record (client ID and secret, Test
   Connection) to manage its licenses.

## 2. From the PowerShell script to Odoo

The script is a template: its `New-PCCustomer` call is not written yet (every row ends as
`PENDING_IMPLEMENTATION`). Microsoft documents `New-PartnerCustomer` of the community
Partner Center PowerShell module for this, which calls the Partner Center REST API
`POST /v1/customers`. Odoo calls that REST API directly instead of running PowerShell:

- no PowerShell, Partner Center module or Windows paths needed on the Odoo server;
- the credentials stay in Odoo, restricted to Microsoft 365 administrators;
- every customer is a record with its own status, error, chatter and audit log entry;
- the creation is covered by automated tests.

| Script                                        | Odoo                                                     |
|-----------------------------------------------|----------------------------------------------------------|
| `C:\M365Provisioning\Customers.csv`           | Bulk Import Tenant, type New (CSV or .xlsx, same columns) |
| `foreach ($Customer in $Customers)`           | Background job, one customer at a time                   |
| `New-PCCustomer` (to be completed)            | `POST /v1/customers` with the documented request schema  |
| `try` / `catch` per customer                  | A failure only fails its own record (status *Failed*)    |
| `ProvisioningResults.csv`                     | List view: Status, Customer ID, Error, Created On        |
| `Write-Host` / `Write-Warning`                | Chatter message and audit log entry (*Tenant Creation*)  |

## 3. Prerequisites (Microsoft side)

1. A **Cloud Solution Provider (CSP)** partner account (direct bill partner or indirect
   provider), plus its **integration sandbox** account for testing.
2. A web application registered in **Partner Center › Account settings › App
   management**: note the Account ID (partner directory ID), the App ID and a key (client
   secret).
3. App-only authentication is used (client credentials, scope
   `https://api.partnercenter.microsoft.com/.default`). Microsoft supports it for every
   call below, so no user sign-in or MFA is involved.

## 4. Design

| File / model | Purpose |
|---|---|
| `m365.tenant` field **Type** (`tenant_type`: Existing / New) | Selects the workflow. *Existing* (default) requires the Directory and Client IDs; *New* gets its Directory ID from Microsoft when created |
| `models/m365_tenant_creation.py` (`_inherit = 'm365.tenant'`) | Customer data (contact, registered address, registration number, GDAP), **Creation Status** (`provision_state`), Check / Create / Retry, the scheduled creation and the result (customer ID, admin user and password) |
| `models/m365_partner_account.py`: `m365.partner.account` | Partner Center connection: credentials, token cache, `_pc_request()` client, Test Connection, defaults for new customers (culture, language, GDAP) |
| `m365.tenant.import.wizard` (Bulk Import Tenant) | Type Existing: tenants to connect. Type New: `Customers.csv`, with its own template |

Other parts: the Partner Center Account menu (*Configuration › Tenants*), access rights
(administrators only), multi-company record rule, the audit log operation *Tenant
Creation* (linked to the tenant) and the scheduled action
*Microsoft 365: Create Queued Tenants* (`ir_cron_m365_create_tenants`).

### Workflow of a tenant of type New

```
To Check --Check OK--> Ready --Create in Microsoft 365--> Creating --success--> Created
   ^  |                  |                                   |
   |  +--errors (stay)   +--data changed--> To Check         +--error--> Failed --Retry--> Creating
   +----------------------------------- Reset to Draft ---------------------+

Created --> connection status Not Tested --Test Connection--> Connected (as an existing tenant)
```

- The form shows the creation status bar and the *Tenant Creation* tab until the tenant
  is created, then the connection status bar and the usual tabs.
- Changing the data of a *Ready* or *Failed* tenant sends it back to *To Check*.
- The Partner Center data of *Creating* and *Created* tenants cannot be changed and they
  cannot be deleted (archive them instead); their name and domain stay editable.
- The type cannot change once the tenant is created or connected.
- Tenants not created yet are left out of Test Connection, synchronisation, the
  dashboard, the REST API tenant list and the license request tenants.

## 5. Partner Center calls

| Step                | Call                                   | Result used                                         |
|---------------------|----------------------------------------|-----------------------------------------------------|
| Test Connection     | `GET /v1/profiles/legalbusiness`       | Partner organisation name                           |
| Check: domain       | `HEAD /v1/domains/{domain}`            | 404 = available, 200 = already used                 |
| Check: address      | `POST /v1/validations/address`         | `Verified` / `VerifiedShippable`, else the suggested address is shown |
| Create              | `POST /v1/customers`                   | `id`, `companyProfile.tenantId`, `userCredentials`  |

Request body of `POST /v1/customers`:

```json
{
  "enableGDAPByDefault": true,
  "CompanyProfile": {"Domain": "contoso.onmicrosoft.com",
                     "OrganizationRegistrationNumber": "required in some countries"},
  "BillingProfile": {
    "Culture": "en-IN", "Email": "billing@contoso.com", "Language": "en",
    "CompanyName": "Contoso Ltd",
    "DefaultAddress": {"FirstName": "", "LastName": "", "AddressLine1": "", "City": "",
                       "State": "", "PostalCode": "", "Country": "IN", "PhoneNumber": ""}
  }
}
```

Checks done in Odoo before calling Microsoft: required fields, domain format, email,
culture (`en-US`) and language (`en`) format, and the organization registration number
for the countries where Microsoft requires it (India, Brazil, Poland, United Arab
Emirates, etc.).

## 6. Reliability

- **Background creation.** *Create in Microsoft 365* only queues the tenants and triggers
  the scheduled action. The job creates up to 20 tenants per run, then triggers itself
  again. A large file therefore never hits the HTTP request time limit. The job also runs
  every hour to resume interrupted work.
- **No duplicate tenants.** The `MS-RequestId` of each creation is saved (and committed)
  before the call. When Partner Center gives no answer (timeout, network error), the
  tenant stays *Failed* with that ID, and **Retry** sends the same ID. Microsoft then
  returns the tenant it already created instead of creating a second one. *Reset to
  Draft* clears the ID; the next check then reports the domain as already used.
- **Commit per tenant.** Each result is committed as soon as it is known, so a later error
  in the same batch cannot roll back the record of a tenant that exists in Microsoft 365.
- Expired tokens are renewed once. HTTP 429 / 503 are retried up to 3 times, honouring
  `Retry-After`.

## 7. Security

- Partner Center accounts, the creation data of the tenants (*Tenant Creation* tab) and
  the import wizard are restricted to the **Microsoft 365 / Administrator** group.
  Multi-company rules apply.
- The client secret and the token are hidden from other users (field groups), as for the
  tenants.
- The **initial admin password** returned by Microsoft is visible to administrators only
  and is never posted in the chatter. Hand it to the customer, then press **Remove
  Password from Odoo**.
- Each creation (success or failure) is written to the audit log with the administrator
  who requested it.

## 8. User guide

1. *Tenants › Partner Center Account*: enter the IDs and the secret, then press
   **Test Connection**.
2. *Tenants › Single Tenant*: create a record of type **New**, or use *Bulk Import Tenant*
   with type **New** to load a file (use **Download Template**). A domain without a dot,
   e.g. `contoso`, becomes `contoso.onmicrosoft.com`.
3. Select the tenants and use **Check** (*Check (New Tenants)* in the list). Fix the
   reported errors. If Microsoft cannot verify an address you know is correct, tick
   **Address Confirmed**.
4. Select the *Ready* tenants and use **Create in Microsoft 365**. Results appear within
   about a minute.
5. Open a *Created* tenant: copy the admin user and password for the customer from the
   *Tenant Creation* tab. Its directory ID is filled in: add the client ID and secret of
   an app registration consented in the new tenant (*Entra App Registration* tab), then
   use **Test Connection** as for any tenant.

`Customers.csv` columns (headers are matched ignoring case, spaces and underscores, so
the field labels can be used as well):

`CompanyName`*, `Domain`*, `FirstName`*, `LastName`*, `Email`*, `PhoneNumber`,
`AddressLine1`*, `AddressLine2`, `City`*, `State`, `PostalCode`*, `Country`* (ISO code or
name), `Culture`, `Language`, `OrganizationRegistrationNumber` (*required).

## 9. Testing

Automated: `tests/test_m365_tenant_creation.py` (13 tests, Partner Center mocked) and the
type New cases of `tests/test_m365_tenant_import.py`. They cover the type rules
(required IDs, type changes), field checks, domain and address checks, the request
body, success, failure, timeout with the same request ID, batching, the restriction to
administrators, tenants not created yet being left out of connection, synchronisation and
dashboard, the Partner Center client (token, headers, errors) and the CSV / Excel
import.

```
odoo-bin -c <config> -d <test database> -u m365_license_management \
         --test-tags /m365_license_management --stop-after-init
```

Manual acceptance, with the **integration sandbox** account first:

1. Test Connection shows the partner name.
2. Check a customer with a domain already in use: the error is shown.
3. Check a customer with a partial address: the suggested address is shown.
4. Create one tenant: status *Created*, customer ID and directory ID filled, and the
   customer visible in Partner Center › Customers.
5. Sign in to the new tenant with the admin user and password.
6. Load a file of 3 customers with one invalid row: nothing is added until it is fixed.

## 10. Next phases (not implemented)

| Item | Note |
|------|------|
| Microsoft Customer Agreement attestation (`POST /v1/customers/{id}/agreements`) | Needed before buying subscriptions for the customer. Microsoft supports only **App+User** authentication (MFA enforced since April 2026) for it, so it needs the secure application model (refresh token of a partner user). Until then, the customer accepts the agreement in the Microsoft 365 admin center, or the partner confirms it in Partner Center. |
| Ordering subscriptions / licenses for the new customer | Partner Center cart and order APIs (new commerce). |
| Automatic Odoo tenant setup | Grant consent to a multi-tenant app in the new tenant (GDAP or application consent), so that *Test Connection* works without a manual app registration. |
| Custom domains | Add and verify `contoso.com` in the new tenant (Microsoft Graph). |
| Notify the customer | Email the sign-in details (secure delivery, not the password in clear). |
| Indirect resellers | `AssociatedPartnerId` for indirect providers creating customers for a reseller. |
