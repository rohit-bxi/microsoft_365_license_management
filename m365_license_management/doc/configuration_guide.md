# Microsoft 365 License Management: Configuration Guide

Everything to set up before the module (19.0.1.8.0) works end to end: the Odoo server, the
Microsoft side (Partner Center, Entra ID, GDAP), Ingram Micro, and each configuration
screen in Odoo. Section 1 is the checklist; the other sections give the details.

## 1. Checklist, in order

| # | What | Where | Needed for |
|---|------|-------|------------|
| 1 | Public HTTPS address of Odoo | Odoo system parameters | Consent, partner sign-in, Graph and Ingram webhooks |
| 2 | Outgoing mail server | Odoo Settings | DNS records email, notifications |
| 3 | Scheduled actions running (`max_cron_threads` ≥ 1) | Odoo server config | Everything automatic |
| 4 | Users in the Microsoft 365 groups | Odoo Settings › Users | Access |
| 5 | Partner Center app (app-only) | Partner Center › App management | Tenant creation, catalog, orders |
| 6 | Shared multi-tenant app | Entra admin center (partner tenant) | Connecting customer tenants, GDAP, App+User |
| 7 | GDAP prerequisites | Partner tenant | GDAP relationships and group access |
| 8 | Partner service user and security group | Partner Center + Entra | Bulk provisioning without customer sign-in |
| 9 | Partner Center Account | Odoo › Configuration › Tenants › Partner Center Account | Steps 5 to 8 entered in Odoo |
| 10 | Sign In as Partner User | Same form (system administrator) | Agreement, Install Shared App |
| 11 | Synchronise Catalog, choose the Default Offer | Same form | Buying Business Basic (no Teams) |
| 12 | Tenant Configuration Profile (default) | Configuration › Tenants | Usage location, Security Defaults, passwords |
| 13 | Ingram Micro Account (only if buying through Ingram) | Configuration › Ingram Micro | Ingram orders, invoices, renewals |
| 14 | Tenant customers and user mapping | Configuration | What non-admin users see |
| 15 | Customer file, then Run Automatically | Configuration › Tenants › Bulk Import Tenant | Bulk provisioning |

Use the **Partner Center integration sandbox** and an **Ingram sandbox app** first:
customers created in production are real tenants and cannot be deleted from Odoo.

## 2. Odoo server

| Item | Value | Why |
|------|-------|-----|
| `web.base.url` (system parameter) | Public HTTPS address, e.g. `https://odoo.example.com` | Builds the callback and webhook URLs |
| `m365.webhook_base_url` (optional) | Public address when `web.base.url` is internal | Takes precedence for Microsoft and Ingram callbacks |
| `m365.audit_retention_days` | Days of audit log and Ingram events kept (default 365, 0 = forever) | Weekly purge |
| `m365.portal_import_max_rows` | Rows of a portal import file (default 100, 0 = no limit) | Portal self-service |
| Outgoing mail server | SMTP configured | *Send DNS Records*, alerts, activities |
| Scheduled actions | `max_cron_threads` ≥ 1 (2 or more with many tenants) | See section 9 |
| Python | `openpyxl`, `requests` | Excel files, API calls |

The addresses Microsoft and Ingram call must be reachable from the internet over HTTPS:

| URL | Used by |
|-----|---------|
| `https://<odoo>/m365/consent/callback` | Admin consent of the shared app (customer Global Admin) |
| `https://<odoo>/m365/partner/signin/callback` | Sign in of the partner user (App+User) |
| `https://<odoo>/m365/webhook/<tenant id>` | Graph change notifications (live user sync) |
| `https://<odoo>/m365/ingram/webhook/<account id>` | Ingram order events |

## 3. Odoo users and access

| Group | Gets |
|-------|------|
| Microsoft 365 / **User** | Dashboard, inventory, users, own requests; only the tenants of the customers mapped to them |
| Microsoft 365 / **Administrator** | Everything: tenants, accounts, approvals, orders, provisioning, secrets |
| Microsoft 365: **Portal Tenant Self-Service** | Portal page *My Account › Microsoft 365 Tenants* (own tenants) |
| Settings / **Administration (system)** | Additionally needed for *Sign In as Partner User* |

- **User Customer Mapping** (Configuration): which tenant customers each Microsoft 365
  user sees. A user without mapping sees no tenant.
- **Tenant Customers**: each tenant is linked to a customer (created automatically by the
  customer file import).
- **REST API** (`/m365/api/v1`): an Odoo API key of the user (Preferences › Account
  Security), sent as `Authorization: Bearer <key>`; the user's rights apply.
- The administrator who imports a customer file with *Run Automatically* is the user the
  automatic run acts as: keep them in the Administrator group.

## 4. Microsoft side

### 4.1 Partner Center app (app-only)

Partner Center › Account settings › **App management** › Add new web app (or add an
existing Entra web app).

| Note down | Odoo field |
|-----------|------------|
| Account ID (partner directory ID) | Partner Directory (Tenant) ID |
| App ID | Application (Client) ID |
| Key (client secret) | Client Secret |

Requirements: a CSP account (direct bill partner or indirect provider). Buying in Partner
Center needs direct bill or indirect provider; an indirect reseller buys through Ingram
(section 6).

### 4.2 Shared multi-tenant app (Entra, partner tenant)

Entra admin center › App registrations › New registration, **Accounts in any
organizational directory**, platform **Web**. It can be the app of 4.1 if that one is
multi-tenant.

| Setting | Value |
|---------|-------|
| Redirect URIs | `https://<odoo>/m365/consent/callback` and `https://<odoo>/m365/partner/signin/callback` (shown on the Partner Center Account form) |
| Client secret | Create one; enter it in Odoo; note its expiry date |

**Application permissions, Microsoft Graph** (granted in each customer tenant by consent or
*Install Shared App*):

| Permission | Feature |
|------------|---------|
| `Organization.Read.All` | License inventory |
| `User.Read.All` / `User.ReadWrite.All` | User sync / user creation, usage location |
| `LicenseAssignment.ReadWrite.All` | Assign and remove licenses |
| `Domain.ReadWrite.All` | Custom domains, password expiry |
| `Organization.ReadWrite.All` | Technical notification email |
| `Policy.Read.All` | Read Security Defaults |
| `Policy.ReadWrite.ConditionalAccess` | Turn Security Defaults on / off |
| `UserAuthenticationMethod.ReadWrite.All` | MFA methods, Temporary Access Pass |
| `Policy.ReadWrite.AuthenticationMethod` | Per-user MFA, Temporary Access Pass policy |

The same list (comma separated) is in **Application Permissions** on the Partner Center
Account: Install Shared App grants exactly these. A permission added later needs a new
consent / installation in each tenant.

**Application permission in the partner tenant** (admin consent in the partner tenant):
`DelegatedAdminRelationship.ReadWrite.All` (GDAP).

**Delegated permissions** (partner user sign-in, App+User): Partner Center
`user_impersonation`; Microsoft Graph `Application.Read.All`,
`AppRoleAssignment.ReadWrite.All`, `offline_access`, `openid`, `profile`.

### 4.3 GDAP prerequisites (partner tenant)

- Service principal of *Partner Customer Delegated Administration*:
  `New-MgServicePrincipal -AppId 2832473f-ec63-45fb-976f-5d45a7d4bb91`.
- A **security group** for the GDAP access (e.g. `M365 Automation`): note its **object
  ID**. Members: the partner service user (4.4) and the administrators who manage the
  customers.
- Default roles requested by Odoo: User, License and Helpdesk Administrator, Directory
  Readers, Global Reader and **Privileged Role Administrator** (needed by Install Shared
  App).

### 4.4 Partner service user (App+User)

A dedicated account (not a person), e.g. `m365-automation@<partner>.onmicrosoft.com`:
Partner Center role **Admin Agent**, **MFA registered**, member of the GDAP group. A system
administrator signs it in from Odoo; sign in again when Odoo asks (about every 90 days).

### 4.5 Own app registration of a tenant (alternative to the shared app)

For an existing tenant connected without the shared app: register a single-tenant app in
that tenant with the permissions of 4.2, grant admin consent, and enter Directory (Tenant)
ID, Application (Client) ID and Client Secret on the tenant (*Entra App Registration*
tab).

## 5. Odoo: Partner Center Account

Configuration › Tenants › **Partner Center Account**.

| Section | Field | Value |
|---------|-------|-------|
| App Registration (App-only) | Partner Directory (Tenant) ID, Application (Client) ID, Client Secret | From 4.1 |
| New Customers | Default Culture, Default Language | e.g. `en-IN`, `en` |
| | Create GDAP Relationship | On: Microsoft creates the default GDAP with each new customer |
| Shared App | Application (Client) ID, Client Secret | From 4.2 |
| | Redirect URI / Partner Sign-in Redirect URI | Read only: copy them to the app (4.2) |
| Partner User (App+User) | **Sign In as Partner User** | System administrator signs in as the service user (4.4) |
| Customer Tenant Setup | Application Permissions | List of 4.2 (default filled in) |
| | Delegated Scopes | `Application.Read.All, AppRoleAssignment.ReadWrite.All` |
| | GDAP Security Group (Object ID) | From 4.3 |
| | Roles of the Group | Empty = all roles of the relationship |
| | Default Offer | After the catalog sync: e.g. *Microsoft 365 Business Basic (no Teams)*, monthly |
| Purchase | Buy Licenses Through | Partner Center or Ingram Micro |
| | Catalog Country | e.g. `IN` (the countries of the tenants are read too) |
| | Catalog Products | Words of the product titles to read (default: Microsoft 365, Office 365, …) |
| | Require Customer Agreement | On |
| GDAP | GDAP Roles, Duration (days, max 730), Extend Automatically | Defaults are fine |
| Endpoints | Authority URL, Partner Center API URL, Scope | Defaults (change for sovereign clouds) |

Then: **Test Connection** (shows the partner name), **Synchronise Catalog**, choose the
**Default Offer**.

## 6. Odoo: Ingram Micro Account (only when buying through Ingram)

Configuration › Ingram Micro › **Ingram Micro Accounts**.

Prerequisites at Ingram (developer.ingrammicro.com, *Xvantage Integrations API Portal*):

- An **app** whose API products include the Reseller APIs used: Product Catalog v6,
  Order Management v6/v7, Quote Management v6, Invoice Management v6/v7, Subscription
  Management v7. A *Sandbox* app has the *(Sandbox)* products; production needs a
  production app. Adding products may need a new **approval** by Ingram.
- **Net terms** on the account (the order API does not take cards).
- The **webhook** for order events registered with the Odoo URL, and its secret key.
- The **vendor fields** Microsoft needs on orders (names from Ingram).
- The customers linked to Ingram as indirect provider (relationship accepted).

| Field | Value |
|-------|-------|
| API URL | Production app: `https://api.ingrammicro.com:443`; Sandbox app: `https://api.ingrammicro.com:443/sandbox` (the token is always taken at the host root) |
| Client ID, Client Secret | Of **that** app (portal › Apps › the app › API Keys) |
| Customer Number | Ingram reseller number, e.g. `63-BXTELL` (header `IM-CustomerNumber`, max 10) |
| Country Code | e.g. `IN` |
| Sender ID | Name of the application (max 32), default *Odoo Microsoft 365* |
| Vendor, Catalog Type | `Microsoft`, `IM::subscription` (empty: not sent) |
| Bill-to Address ID | Only with several billing addresses |
| Webhook Secret Key | From the portal; Webhook URL is shown to register there |
| Require Ingram Micro Relationship / Customer Agreement | On |
| Vendor Fields tab | Microsoft order fields (or *Import Vendor Fields* from a quote) |

Then **Test Connection**, **Synchronise Catalog**, and map each plan to its catalogue
product. Error *"no API product for …"*: the Client ID does not belong to an approved app
with those products, or the API URL does not match the app (sandbox / production).

## 7. Odoo: tenant configuration

### 7.1 Tenant Configuration Profiles

Configuration › Tenants › **Tenant Configuration Profiles**; mark one **Default for New
Tenants**.

| Field | Value |
|-------|-------|
| Security Defaults | Keep / Turn On / Turn Off (Off only for tenants with security operations) |
| Passwords Never Expire | On or off |
| Default Usage Location | e.g. India: **required** to assign licenses to new users |
| Technical Notification Email | Your support mailbox |

Applied when a tenant connects through the shared app, or with *Apply Configuration*.

### 7.2 A tenant (Configuration › Tenants › Single Tenant)

| Tab | Fields |
|-----|--------|
| Main | Type (Existing / New), Primary Domain, Tenant Customer, Company |
| Tenant Creation (New) | Partner Center Account, contact, registered address, Culture, Language, Organization Registration Number (India, Brazil, …), Create GDAP Relationship, Custom Domains, Make the Custom Domain the Default, Pick a Free Initial Domain |
| Entra App Registration | Connection (Own App / Shared App), Directory ID, Client ID, Secret, endpoints; Grant Consent / Check Consent |
| Operations & Automation | Enable License Operations, Skip Approval for Administrators, Enable Security Operations, Scheduled Synchronisation, Synchronise Users, low availability thresholds (% / units) |
| Configuration | Configuration Profile |
| Purchasing | Customer Accepted the Agreement, agreement contact and date, *Accept in Partner Center*; Ingram relationship |
| Provisioning | Steps, Subscriptions to Buy, Run Automatically / Stop, Approve Orders Automatically, Install Shared App, GDAP, Users to Create |
| Domains | Status, TXT record; Add Domain, Refresh, Send DNS Records |
| Live Sync | Delta Synchronisation; webhook (needs the public URL) |

### 7.3 Other master data

- **Product Catalogue**: SKU part number → friendly name. Unknown licenses get the name of
  the Partner Center offer, else a generated one: rename if needed.
- **Partner Center Offers**: read only, filters *No Teams*, *Monthly Term*, country.

## 8. Customer file (Bulk Import Tenant, type New)

Download the template from the wizard. Sheet **Customers**:

| Column | Required | Notes |
|--------|----------|-------|
| CompanyName | Yes | |
| Domain | Yes | `contoso` (→ contoso.onmicrosoft.com) or the client's domain `contoso.com` (initial name derived) |
| FirstName, LastName, Email | Yes | Billing contact |
| PhoneNumber | | |
| AddressLine1, City, PostalCode, Country | Yes | Country: ISO code or name |
| AddressLine2, State | | State code, e.g. KA |
| Culture, Language | | Default: the Partner Center account |
| OrganizationRegistrationNumber | In some countries | India, Brazil, Poland, UAE, … |
| Subscriptions | | `Microsoft 365 Business Basic (no Teams) = 10`, or `10` for the default offer |
| CustomDomains | | `contoso.com; contoso.in` |
| AgreementFirstName, AgreementLastName, AgreementEmail, AgreementPhone, AgreementDate | | Who accepted the Microsoft Customer Agreement (default: the contact) |

Sheet **Users** (or a separate users file for a CSV customer file): CompanyName,
UserName (alias or full UPN), DisplayName, FirstName, LastName, UsageLocation, Licenses
(`default`, an offer name, a license name or SKU), Department, JobTitle.

Options of the wizard: **Run Automatically**, **Approve the Subscription Orders**, **The
Customers Accepted the Microsoft Customer Agreement**, **Make the Custom Domain the
Default**, **Pick a Free Initial Domain**.

## 9. Scheduled actions

Settings › Technical › Scheduled Actions (all active by default):

| Name | Every |
|------|-------|
| Run Automatic Provisioning | 5 min |
| Add and Verify Custom Domains | 15 min |
| Create Waiting Users | 15 min |
| Follow Partner Center Orders | 15 min |
| Verify Assigned Licenses | 15 min |
| Process Change Notifications | 15 min |
| Process Ingram Micro Events | 15 min |
| Synchronise Tenants | 30 min |
| Create Queued Tenants | 1 h (triggered at once) |
| Check License Order Deliveries | 1 h |
| Refresh GDAP Status | 1 day |
| Renew Webhook Subscriptions | 1 day |
| Synchronise Ingram Micro Invoices and Renewals | 1 day |
| Keep the Partner User Signed In | 1 week |
| Purge Old Inventory Snapshots / Audit Logs / Ingram Events | 1 week |

## 10. Acceptance test (sandbox)

1. Partner Center Account: Test Connection, Synchronise Catalog, Default Offer.
2. Sign In as Partner User: status *Signed In*.
3. Import a file of 1 customer with a custom domain and 2 users, *Run Automatically*.
4. Follow *Provisioning*: tenant created, GDAP active with group access, agreement
   recorded, shared app installed and connected, TXT record shown.
5. Add the TXT record at the DNS host (or *Send DNS Records*): the domain is verified.
6. Order done, users created and licensed, verification *Verified*.
7. *Download Credentials*, then the **Report**.

## 11. Known open points

| Point | Action |
|-------|--------|
| Ingram account answers "no API product" | Use the Client ID / Secret of the approved app, API URL matching it (sandbox or production) |
| Install Shared App never run against Microsoft yet | Test it in the Partner Center sandbox first (plan, Phase 0): Partner Center consent, then Odoo grants the application permissions through GDAP |
| DNS provider API (Cloudflare) | Not built: TXT records are sent to the customer |
| Client secrets expire | Note the expiry of the Partner Center, shared app and Ingram secrets |
