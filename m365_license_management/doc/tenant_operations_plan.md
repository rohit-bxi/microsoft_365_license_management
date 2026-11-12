# Tenant Operations: Implementation Plan

From a new customer to licensed users, from Odoo: create the tenant, connect it,
configure it, buy and assign its licenses, and remove MFA when needed.

Status: **all phases implemented in 19.0.1.7.0**, tested with Microsoft Graph, Partner
Center and Ingram Micro mocked (section 12). Not yet tried against a live tenant or the
Ingram Micro sandbox. Builds on the tenant creation of 19.0.1.3.0
(`doc/tenant_provisioning_plan.md`).

## 1. Decisions

| Question | Decision |
|---|---|
| Connection of the tenants | **One shared multi-tenant app**, consented once in each tenant |
| Buying licenses | **Through Ingram Micro** (Reseller API) |
| Tenant configuration | The list of section 6 *(assumed, to confirm)* |
| MFA operations | Reset MFA, per-user MFA off, Security Defaults off *(assumed, to confirm)* |

## 2. Goal

```
Create tenant --> Grant consent --> Apply configuration --> Buy licenses --> Create users + assign
(Partner Center)  (shared app)      (Graph)                 (Ingram)         (Graph)

MFA operations (Graph): at any time once the tenant is connected
```

Existing tenants can use every step after *Create tenant*.

## 3. Who does what

| Step | API | Why |
|---|---|---|
| Create the tenant | Partner Center `POST /v1/customers` (implemented) | Only Microsoft creates tenants |
| Consent, configuration, users, license assignment, MFA | Microsoft Graph | Only Graph manages the directory of a tenant |
| Buy licenses | Ingram Micro Reseller API | Ingram is the CSP provider the licenses are bought from |

The Ingram Reseller API has no tenant, user, license assignment or MFA endpoint: it covers
catalog, orders, quotes, invoices, renewals, deals, returns and freight only.

## 4. Prerequisites

Microsoft:

1. A **multi-tenant app registration** in the partner tenant (*Accounts in any
   organizational directory*) with a client secret, the redirect URI
   `https://<odoo>/m365/consent/callback` and the application permissions of section 10.
2. The existing Partner Center account (tenant creation).

Ingram Micro (ask the Ingram account team now, these take time):

1. API credentials (client ID and secret) with access to Catalog and Orders, sandbox and
   production.
2. **Net terms** on the Ingram account: the order API does not accept credit cards.
3. The Odoo callback URL `https://<odoo>/m365/ingram/webhook` registered for order
   events, and its **webhook secret key**.
4. The **vendor-required fields of Microsoft orders** (`vmfAdditionalAttributes`: tenant
   ID or domain, Customer Agreement acceptance, ...): names and formats. They are not in
   the public specification.
5. How a customer created by us in Partner Center is linked to Ingram as indirect
   provider (relationship invitation accepted by the customer, or created by Ingram).

## 5. Phase 1: Shared app and consent (implemented)

| File / model | Change |
|---|---|
| `models/m365_partner_account.py` | `graph_client_id`, `graph_client_secret` (administrators only): the shared app; `consent_redirect_uri` to register on it; `_shared_app_credentials()` |
| `models/m365_tenant.py` | `_get_app_credentials()`: the app the token is requested for (own app here) |
| `models/m365_tenant_consent.py` (`_inherit = 'm365.tenant'`) | `connection_mode` (*Own App Registration*, default / *Shared App*), consent fields, **Grant Consent** / **New Consent Link**, **Check Consent**, callback processing |
| `controllers/consent.py`, `views/m365_consent_templates.xml` | `GET /m365/consent/callback` (public) and its result page |
| `tests/test_m365_tenant_consent.py` | 13 tests |

Flow:

1. **Grant Consent** (administrators) stores a new nonce, an expiry of **7 days** and the
   requesting administrator, and opens
   `https://login.microsoftonline.com/{tenant_id}/v2.0/adminconsent?client_id={shared app}&scope={graph scope}&redirect_uri={callback}&state={signed state}`.
   The link is also shown on the *Entra App Registration* tab, to send to the customer:
   7 days instead of the one hour first planned, because the Global Administrator is
   often the customer.
2. A Global Administrator of the tenant signs in and accepts. For a new tenant: the
   initial admin user created by Odoo.
3. Microsoft redirects to the callback with `tenant`, `admin_consent=True` and `state`
   (or `error` and `error_description`).
4. The callback accepts only the last link of the tenant, unused and unexpired: the state
   is signed with `hash_sign()` (database secret, expiry) and its nonce must match the
   one stored. When the directory ID is a GUID, `tenant` must be that directory.
5. **The tenant switches to the shared app only once the app reads it** (`GET
   /organization` in a savepoint, 3 tries 5 seconds apart). A forged or replayed callback
   therefore changes nothing; an error leaves the tenant as it was and is logged. Then
   Test Connection runs. Chatter and audit log name the administrator who created the
   link.
6. Microsoft can take a few minutes to apply a consent: **Check Consent** tries again.
   It also connects a tenant that granted consent outside Odoo.

The result page tells the Global Administrator the outcome; Odoo administrators get a
link to the tenant. Admin consent grants every permission of the app at once. A
permission added to the app later needs **New Consent Link** in each tenant; Test
Connection reports the missing ones (`_probe_permissions()`).

Also fixed: changing a secret (tenant or Partner Center account) failed with an access
error for Microsoft 365 administrators who are not system administrators, because the
cleared token fields are restricted to system administrators. The tokens are now cleared
with superuser rights. Changing the shared app secret clears the tokens of the tenants
using it, without disconnecting them.

Not changed: the Bulk Import Tenant file and the portal still connect tenants with their
own app registration (a client ID is required there).

## 6. Phase 2: Tenant configuration (implemented)

`m365.config.profile` (*Configuration › Tenant Configuration Profiles*), one marked as
default for new tenants:

| Setting | Graph call |
|---|---|
| Security Defaults: keep / on / off | `PATCH /policies/identitySecurityDefaultsEnforcementPolicy` `{"isEnabled": ...}` |
| Passwords never expire | `PATCH /domains/{id}` `{"passwordValidityPeriodInDays": 2147483647}` |
| Default usage location | none: used for the users created in Phase 3 |
| Technical notification email | `PATCH /organization/{id}` `{"technicalNotificationMails": [...]}` |

Tenant fields: `config_profile_id`, `config_state` (Not Applied / Applied / Failed),
`config_date`, `config_error`, `security_defaults_enabled` (read by Test Connection and
the synchronisation).

**Apply Configuration** reads each current value first and writes only what differs, so
it can run again safely. One audit log line per change; the first failure stops the run
with state *Failed*.

Custom domains, `m365.tenant.domain` (name, verified, default, initial, DNS records to
send to the customer):

| Action | Graph call |
|---|---|
| Load the domains | `GET /domains` (synchronisation) |
| Add | `POST /domains` `{"id": "contoso.com"}` |
| DNS records | `GET /domains/{id}/verificationDnsRecords` (TXT), then `GET /domains/{id}/serviceConfigurationRecords` (MX, SPF, CNAME) |
| Verify | `POST /domains/{id}/verify` |
| Make default | `PATCH /domains/{id}` `{"isDefault": true}` |

Files: `models/m365_config_profile.py`, `models/m365_tenant_config.py`
(`_inherit = 'm365.tenant'`), `models/m365_tenant_domain.py`,
`views/m365_config_views.xml`; menus *Configuration › Tenants › Tenant Configuration
Profiles* and *Domains*; tenant tabs *Configuration* and *Domains*.

As built:

- *Security Defaults: Turn Off* in a profile is applied only to tenants with **Enable
  Security Operations** (Phase 5); on the others the configuration fails with that reason.
- The default profile is set on new tenants; changing the profile of a tenant sets its
  configuration back to *Not Applied*.
- A failed domain verification is a notification, not an error, so that its audit log
  line is kept.
- Test Connection also checks `Policy.Read.All` / `Domain.Read.All` when the profile or
  the tenant uses them, and reads the Security Defaults state.

## 7. Phase 3: Users and license assignment (implemented)

**Create users**: wizard (tenant form and user list) and bulk file.

- `POST /users` with `accountEnabled`, `displayName`, `mailNickname`,
  `userPrincipalName`, `usageLocation` (default from the profile) and `passwordProfile`
  (generated password, `forceChangePasswordNextSignIn: true`).
- The `m365.license.user` record is created from the answer, then one license request
  per license through the existing workflow (same approval rules).
- The domain of the user principal name must be a verified domain of the tenant.
- Passwords are shown once (wizard result, result file) and never stored or posted.
- Bulk file: reuse the parsing of `m365_license_bulk.py` with a new operation and its own
  template. Columns: Tenant\*, User Principal Name\*, Display Name\*, First Name, Last
  Name, Usage Location, License(s), Department, Job Title.
- REST API: `POST /m365/api/v1/tenants/{id}/users`.

License assignment does not change. When no license is left, the request offers
**Buy Licenses** (Phase 4) instead of only an error.

As built (`models/m365_user_create.py`, `views/m365_user_create_views.xml`): **Create
User** on the tenant, menu *Configuration › Tenants › Bulk Create Users*, REST API
`POST /m365/api/v1/tenants/{id}/users`. User creation needs *Enable License
Operations* on the tenant. The password stays in the wizard (field restricted to
administrators) until **Done**, or until Odoo deletes the old wizards; in bulk it is only in
the result file. Each row of a file is created on its own and committed at once: a row that
Microsoft refuses does not stop the others.

## 8. Phase 4: Buying licenses through Ingram Micro (implemented)

| Step | Ingram call | Result used |
|---|---|---|
| Token | `GET /oauth/oauth20/token` (client credentials) | Cached access token (`expires_in` is a string) |
| Catalog | `GET /resellers/v6/catalog?type=IM::subscription&vendor=Microsoft`, paged (max 100) | `planId`, `planName` (e.g. *Microsoft 365 Business Standard (NCE COM MTH)*), terms |
| Plan details | `GET /resellers/v6/catalog/details?planId=` | Billing period, `options` (resource ID, vendor part number, min / max units) |
| Price | `POST /resellers/v6/catalog/priceandavailability?includePricing=true`, `products[].planID` | Price, currency |
| Order | `POST /resellers/v7/orders` | `confirmationNumber`: accepted, processed in the background |
| Result | Ingram calls the Odoo webhook (`resellers/orders`, signed `x-hub-signature`) | `ingramOrderNumber`, line results, errors |
| Lookup | `GET /resellers/v6/orders/search?customerOrderNumber=` | The order of a call that got no answer |

Headers of every call: `IM-CustomerNumber`, `IM-CountryCode`, `IM-SenderID`,
`IM-CorrelationID` (new UUID per call). Errors:
`{"errors": [{"type", "message", "fields": [{"field", "message"}]}]}`.

| Model | Purpose |
|---|---|
| `m365.ingram.account` | Credentials, customer number, country, sender ID, API URL (sandbox / production), bill-to address ID, webhook secret, token cache, Test Connection, `_im_request()` client (like `_pc_request()`) |
| `m365.ingram.plan` | Catalog: plan ID, name, group, term, billing period, part numbers, price, currency, last refresh, **Catalogue Product** (`m365.product`) |
| `m365.ingram.order` and lines | One purchase for one tenant: plans, quantities, prices at order time, PO number, status, Ingram order number, errors |

Each plan is mapped to its catalogue product (Graph `skuPartNumber`) so that Odoo sees
when the licenses arrive. Odoo suggests the product by name; an administrator confirms.

```
Draft --Submit--> Waiting Approval --Approve--> Sending --accepted--> Sent --webhook OK--> Confirmed
                        |                          |                    |                     |
                        +--Reject--> Rejected      +--error--> Failed <-+--webhook error      |
                                                                 |                            |
                                                                 +--Retry (same PO)--> Sending|
                                                     licenses seen in Graph --> Delivered <---+
```

- **Approval**: always by an administrator (money is spent), never automatic.
- **No duplicate orders**: the PO number (`customerOrderNumber`, Odoo sequence) is saved
  and committed before the call. Retry first searches Ingram for that PO number and sends
  the order again only when it is not found. `allowDuplicateCustomerOrderNumber` is never
  sent, so Ingram refuses the same PO number twice (to confirm in the sandbox).
- **Order body**: `customerOrderNumber`, `endCustomerOrderNumber` (tenant reference),
  `endUserInfo` (tenant customer: company, contact, address, email), `lines[]` (plan,
  quantity), `vmfAdditionalAttributes` (Microsoft fields, prerequisite 4).
- **Webhook** `POST /m365/ingram/webhook` (public, no CSRF): checks `x-hub-signature`
  (secret key + `eventId`, algorithm of Ingram's webhook secret key article); a missing or
  wrong signature gets HTTP 401 and changes nothing. Event IDs are stored so a repeated
  event is ignored. The order is found by `customerOrderNumber`. The answer is immediate;
  the processing runs in a scheduled job, as for the Graph webhook.
- **Delivery**: once *Confirmed*, the tenant inventory is synchronised. When the total of
  the mapped product grew by at least the ordered quantity, the order is *Delivered*. Not
  delivered after 24 hours: an activity for the administrators. Ingram's order details do
  not return subscription information yet, hence the check through Graph.
- **Dashboard**: the low availability alert links to **Buy Licenses** with the tenant and
  product filled in.

Not in this phase: changing the seats of an existing subscription (not in the Reseller
API: an additional purchase is a new order). Cancelling an order, invoices and renewals:
see section 15.

As built (`models/m365_ingram_account.py`, `models/m365_ingram_plan.py`,
`models/m365_ingram_order.py`, `controllers/ingram.py`, `views/m365_ingram_views.xml`):

- The webhook URL is per account: `/m365/ingram/webhook/<account id>` (shown on the
  account). Signature: base64 of the HMAC-SHA512 of `eventId` with the webhook secret key,
  as in Ingram's webhook article; hex is accepted as well. Ingram's portal test messages
  have no signature and are refused (HTTP 401).
- Events are stored (`m365.ingram.event`, menu *Ingram Micro › Webhook Events*), each event
  ID once, and processed by *Microsoft 365: Process Ingram Micro Events*, triggered at once.
  *Microsoft 365: Check License Order Deliveries* runs every hour.
- **Vendor Fields** (tab of the account): the Microsoft fields of the orders, entered with
  the names Ingram gives, and the value Odoo sends (tenant directory ID, domain, contact,
  order date, plan ID, quantity or a fixed value).
- The token is requested as in Ingram's specification (GET with the credentials as query
  parameters). `IM-CorrelationID` is a 32 character hex UUID (maximum 32).
- Users of the *Microsoft 365 / User* group can read the orders of their tenants; only
  administrators create, approve and send them.
- **Buy Licenses**: tenant form, subscription form, license request without license left,
  and the dashboard alerts *No licenses left* / *Only n left* (administrators).

## 9. Phase 5: Removing MFA (implemented)

| Operation | Scope | Graph call |
|---|---|---|
| **Reset MFA** (the user registers again at the next sign-in) | User | `GET /users/{id}/authentication/methods`, then `DELETE /users/{id}/authentication/{type}Methods/{methodId}` for each method except the password |
| **Turn off per-user MFA** | User | `PATCH https://graph.microsoft.com/beta/users/{id}/authentication/requirements` `{"perUserMfaState": "disabled"}` (beta only) |
| **Turn off Security Defaults** | Tenant | `PATCH /policies/identitySecurityDefaultsEnforcementPolicy` `{"isEnabled": false}` |

Method types removed by Reset MFA: `microsoftAuthenticator`, `phone`, `softwareOath`,
`fido2`, `email`, `windowsHelloForBusiness`, `temporaryAccessPass`.

- `m365.license.request`: `request_type` gains *Reset MFA* and *Turn Off Per-user MFA*;
  the license (`sku_id`) is required for *Assign* / *Remove* only. Same approval queue,
  bulk file, REST API and audit log. The menu becomes *Requests*.
- MFA requests are never approved automatically, need a justification, and run only when
  the new tenant option **Enable Security Operations** (`allow_security_operations`,
  default off) is set.
- Reset MFA deletes the non-default methods first. Microsoft may refuse to delete the
  default method; the request then fails with the list of the methods left.
- Optional: after a reset, issue a Temporary Access Pass
  (`POST /users/{id}/authentication/temporaryAccessPassMethods`), shown once.
- User form: **MFA Methods** shows the methods and the per-user MFA state live (type,
  name, masked phone). Nothing is stored in Odoo.
- Tenant form: **Turn Off / Turn On Security Defaults** with a confirmation wizard and a
  reason, chatter message and audit log.
- The beta URL is built from `graph_url` (`/v1.0` → `/beta`); `_graph_request()` already
  accepts full URLs.

Not in this phase: excluding users from Conditional Access policies (needs Entra ID P1).

As built (`models/m365_tenant_security.py`, `models/m365_user_mfa.py`, changes in
`models/m365_license_request.py` and `models/m365_license_bulk.py`):

- **MFA Methods** on a user (administrators) reads the methods and the per-user MFA state
  live; the Temporary Access Pass is issued from there (administrators, Enable Security
  Operations, audit log without the code), not through a request.
- Users request *Reset MFA* / *Turn Off Per-user MFA* from the user form, the bulk file
  (*Bulk Assign / Remove Licenses*, empty License column, justification required) or the
  REST API (`type` `mfa_reset` / `mfa_disable`, no `sku_id`).
- Reset MFA deletes each method once more when Microsoft refused it the first time (the
  default method). Types Odoo does not know are reported, never deleted.
- The menu *License Requests* is now *Requests*.

## 10. Application permissions of the shared app (Microsoft Graph)

| Permission | Used by |
|---|---|
| Organization.Read.All | Inventory (implemented) |
| LicenseAssignment.ReadWrite.All | Assign / remove licenses (implemented) |
| User.ReadWrite.All | User synchronisation (implemented) and user creation |
| Domain.ReadWrite.All | Domains, password expiry |
| Organization.ReadWrite.All | Technical notification email |
| Policy.Read.All | Read Security Defaults |
| Policy.ReadWrite.ConditionalAccess | Turn Security Defaults on / off |
| UserAuthenticationMethod.ReadWrite.All | List and delete MFA methods, Temporary Access Pass |
| Policy.ReadWrite.AuthenticationMethod | Per-user MFA state (beta) |

## 11. Security and audit

- New buttons and wizards, the Ingram account, plans and orders: **Microsoft 365 /
  Administrator** only, multi-company rules. Orders and domains also follow the tenant
  rules of the mapped customers (users can read, not act).
- Secrets (shared app, Ingram, webhook) in administrator-only fields; tokens in system
  fields, as today.
- Passwords of new users and Temporary Access Passes: shown once, never stored, never in
  the chatter.
- New audit log operations: Consent, Configuration, Domain, Create User, Reset MFA,
  Turn Off Per-user MFA, Security Defaults, License Order.
- Portal: unchanged.

## 12. Testing

Automated, with Graph, Partner Center and Ingram mocked (test database only, never the
development database): `tests/test_m365_tenant_consent.py`,
`tests/test_m365_tenant_config.py`, `tests/test_m365_user_create.py`,
`tests/test_m365_mfa.py`, `tests/test_m365_ingram.py` (48 tests; 207 in the module, all
passing). They cover:

- Consent: valid state, changed or expired state, other tenant, error callback.
- Configuration: only changed settings written, second run changes nothing, stop on error.
- Domains: add, DNS records, verify, default.
- Users: creation with license request, unverified domain refused, bulk file.
- Ingram: token, headers, catalog paging, price, order body, retry after no answer
  (search before sending again), webhook signature good / bad, repeated event, delivery.
- MFA: every non-password method deleted, default method refused, beta URL for per-user
  MFA, Security Defaults on / off, no automatic approval, option off refused.
- Access: users and portal users can run none of it.

Manual acceptance with the Partner Center integration sandbox and the Ingram sandbox,
then one pilot customer:

1. Create a tenant, **Grant Consent** as its initial admin: Test Connection connects
   without an own app registration.
2. Apply the configuration; check Security Defaults and password expiry in the Entra
   admin center.
3. Add and verify a domain.
4. Buy one license: the order becomes *Confirmed* through the webhook, then *Delivered*.
5. Create two users with a license.
6. Reset the MFA of a test user: the user registers again at the next sign-in.

## 13. Next steps

| Step | Work |
|---|---|
| 1 | Register the shared app (section 10 permissions) and test Grant Consent on a test tenant |
| 2 | Run the manual acceptance of section 12 with the Partner Center integration sandbox |
| 3 | Get the Ingram prerequisites (section 4): credentials, net terms, webhook, vendor fields |
| 4 | Enter the vendor fields, synchronise the catalog, map the plans, then the Ingram sandbox acceptance |

## 14. Risks and open points

| Point | Effect / action |
|---|---|
| Microsoft requires MFA for the sign-in to its admin portals (Azure, Entra, Intune, Microsoft 365 admin center) whatever the tenant settings | Removing MFA does not change these sign-ins: tell the customers |
| The per-user MFA API is beta | May change; kept in one method |
| Security Defaults off leaves the tenant without MFA unless Conditional Access is set up | Approval, reason and audit; prefer Reset MFA |
| Consent needs one sign-in of a Global Admin per tenant; on a new tenant that admin registers MFA at this first sign-in (Security Defaults are on by default) | Grant consent before turning Security Defaults off. Later: Partner Center application consent API (App+User) to remove the sign-in |
| Microsoft fields of Ingram orders, link of the customers to Ingram, sandbox URL, webhook signature algorithm | From Ingram (section 4) |
| Seat changes and cancellations are not in the Reseller API | Out of scope; confirm with Ingram |
| Configuration list and MFA operations are assumptions | Confirm (section 1) |

## 15. Gap fixes (19.0.1.7.0)

Found by checking every call against Ingram's OpenAPI specification, the Microsoft Graph
reference and the Partner Center documentation. 225 tests, all passing.

| Gap | Fix |
|---|---|
| `endCustomerOrderNumber` limited to 18 characters | The customer reference (`ref`), else the first label of the domain, cut to 18 |
| `endUserInfo` limits (company 40, address 70, city 25, state 3, postal code 10) | Values trimmed; the state is sent as its code (found by name in the country), else not sent |
| PO number 18, 999 lines, one currency | Checked when the order is submitted |
| Catalog `type` (`IM::subscription` not in the listed values) | *Catalog Type* on the account (empty: not sent) |
| Default phone method cannot be deleted | Reset MFA first makes Microsoft Authenticator or a software token the default (beta `signInPreferences`), deletes the phones first (alternate before mobile) and explains what is left |
| External MFA and QR code + PIN methods | Deleted (`externalAuthenticationMethods/{id}/$ref`, beta `qrCodePinMethod`) |
| Order found on Retry trusted whatever its status | `orderStatus` read: cancelled orders are cancelled in Odoo, on hold / backordered tell the administrators |
| Status events read from the top-level event only | `resource[].eventType` and `lines[].lineStatus` read: cancelled, partly cancelled, hold, backorder, shipped |
| No fallback without webhook; order stuck in Sending | The hourly job looks the orders up by PO number (Sending > 15 min, Sent > 30 min, Confirmed > 2 h); alert after 24 h; Retry offered in Sending |
| Temporary Access Pass policy | Read in *MFA Methods* (state, lifetimes, one-time use), checked before issuing; *Enable Temporary Access Pass* |
| Ingram relationship of the customers | *Purchasing* tab of the tenant: confirmed by an administrator (Partner Center shows it only with App+User sign-in); required to submit an order (option of the account) |
| Microsoft Customer Agreement | *Purchasing* tab: who accepted it and when; required to submit an order (option); vendor field sources *Customer Agreement: ...* |
| Vendor fields typed by hand | *Import Vendor Fields* from the validation of an Ingram quote (`validatequote`) |
| Invoices (the Invoices API) | `m365.ingram.invoice`: search every day (first 90 days, then the last week again), lines read for the license orders; smart button on the order |
| Renewals | `m365.ingram.renewal`: read every day, matched by PO number, to-do 30 days before the expiration |
| Cancel at Ingram Micro | *Cancel at Ingram Micro* (`DELETE /resellers/v6/orders/{n}`), possible until the order is released; Reset to Draft takes a new PO number once one was used |
| `IM-ApplicationID` (invoice calls) | Sent with every call (the sender ID) |
| Header limits | Customer number 10, country 2 letters, sender ID 32: checked on the account |
| Setup guides listed 3 permissions | Full list on the tenant and Partner Center account forms |
| Bulk import and portal only with their own app | *Connection* column (Own App / Shared App) in Bulk Import Tenant; *Grant Consent* on the portal tenant page |
| REST API | `POST /tenants/{id}/consent`, `POST /tenants/{id}/configuration`, `GET/POST /tenants/{id}/domains`, `POST /domains/{id}/verify`, `GET/POST /orders`, `GET /orders/{id}`, `POST /orders/{id}/approve | reject | retry | cancel` |
| Failure lines lost when a button raised | Refresh from Microsoft 365, Set Usage Location, webhook subscription, Add Domain, Create User, Security Defaults and Temporary Access Pass log the failure and show it without rolling it back |
| Webhook events kept forever | Processed events purged weekly after `m365.audit_retention_days` |

Still not possible through the APIs: changing the seats of an existing subscription
(Reseller API), checking the Ingram relationship or recording the Customer Agreement in
Partner Center with app-only access (App+User only), and the Invoices v7 endpoints, which
are not in Ingram's public specification (v6 / v6.1 used).

## 16. End-to-end provisioning (6-day scope)

The 8 steps of the scope and where they are in the module (237 tests, all passing):

| Step | API | Module |
|---|---|---|
| 1. Upload customers (Excel/CSV) | - | *Bulk Import Tenant*, type New; column *Subscriptions*; each tenant is linked to the Odoo customer of the same name (created with address and contact) |
| 2. Authentication | Entra ID, Partner Center | Partner Center account (app-only) and shared app; Test Connection |
| 3. Create the tenant | Partner Center `POST /v1/customers` | Check / Create in Microsoft 365; customer ID and directory ID stored on the tenant |
| 4. GDAP | Graph (partner tenant) `delegatedAdminRelationships` | Status per tenant (daily refresh), *Request GDAP* with approval link (`lockForApproval`) |
| 5. Purchase subscriptions | Partner Center catalog, cart, checkout, orders | *Subscription Orders* (`m365.pc.order`): approval, cart saved before the idempotent checkout, order and subscription IDs, done when the licenses appear in Graph |
| 6. Create users | Graph `POST /users` | Create User, Bulk Create Users, REST API |
| 7. Assign licenses | Graph `/subscribedSkus`, `assignLicense` | License requests with approval |
| 8. Verify and report | Graph `/users/{id}`, `/licenseDetails` | Verification of every executed request (scheduled, *Verify*); *Provisioning* menu (status of each step, *Next Step*); Excel provisioning report |

*Buy Licenses Through* on the Partner Center account chooses Partner Center (direct bill
partners, indirect providers) or Ingram Micro (indirect resellers).

Setup in the partner tenant for GDAP: application permission
`DelegatedAdminRelationship.ReadWrite.All` on the shared app (admin consent in the
partner tenant) and the service principal of *Partner Customer Delegated Administration*:
`New-MgServicePrincipal -AppId 2832473f-ec63-45fb-976f-5d45a7d4bb91`.

The Microsoft Customer Agreement confirmation API needs App+User sign-in: the customer
accepts it in the Microsoft 365 admin center and the acceptance is recorded on the tenant
(*Purchasing* tab); Partner Center refuses the checkout without it.
