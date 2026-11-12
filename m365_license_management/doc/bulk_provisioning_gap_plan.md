# Bulk Provisioning Without Manual Steps: Implementation Plan

Closes the gaps found when checking the module (19.0.1.7.0) against the client
requirement: *bulk Microsoft 365 tenant creation and license allocation from an
Excel/CSV file, Business Basic (no Teams) bought through Partner Center, users created and
licensed through Microsoft Graph, Excel report*.

Status: **implemented in 19.0.1.8.0** (section 18), tested with Partner Center and
Microsoft Graph mocked: 266 tests, all passing. Not yet run against the Partner Center
integration sandbox (Phase 0 spike still to do). Builds on
`doc/tenant_provisioning_plan.md` and `doc/tenant_operations_plan.md`.

## 1. Gaps to close

| # | Gap | Phase |
|---|-----|-------|
| G1 | The file has no column for the client's own domain names (`abc.com` is rejected) | 5 |
| G2 | Domains are added and verified by hand, one at a time; no re-check after the DNS change | 6 |
| G3 | Each new tenant needs a Global Admin of the customer to sign in and grant consent | 1, 3 |
| G4 | The Microsoft Customer Agreement acceptance is recorded by hand; checkout fails without it | 1, 2 |
| G5 | GDAP relationship without access assignment: no partner user gets admin rights | 4 |
| G6 | No automatic chaining: *Next Step* runs one step per click | 8 |
| G7 | The report has one row per tenant with counts; no users, licenses or domains | 10 |
| G8 | Offer chosen by exact title, first match taken silently; template shows the Teams version | 9 |
| G9 | One catalog country per Partner Center account | 9 |
| G10 | Users can only be loaded once the licenses are in the tenant (second file, later) | 5, 7 |
| G11 | Licenses unknown to the product master (e.g. no Teams) show the raw skuPartNumber | 9 |

## 2. Target flow

```
Excel: sheet Customers (+ optional sheet Users)
  |
  v  Check: initial domain free, address, offer found, agreement contact      (existing + 5)
Create tenant            PC  POST /v1/customers  (enableGDAPByDefault)       (existing)
GDAP access assignment   Graph (partner) .../accessAssignments               (4)
Accept agreement (MCA)   PC  POST /v1/customers/{id}/agreements    App+User  (2)
Install shared app       PC  POST /v1/customers/{id}/applicationconsents
                         + Graph appRoleAssignedTo in the customer  App+User  (3)
Connect + configuration  Graph (shared app, app-only)                        (existing)
Custom domains           Graph POST /domains, TXT records, verify (scheduled) (6)
Order subscriptions      PC  cart / checkout -> licenses seen in /subscribedSkus (existing)
Create users             Graph POST /users  (UPN on the verified domain)     (7)
Assign + verify          Graph assignLicense, licenseDetails                 (existing)
Report                   Excel: Tenants, Domains, Users, Licenses, Orders    (10)

All of it run by a scheduled job (8): nobody clicks unless a step needs a person.
```

## 3. Phase 0: Prerequisites and sandbox spike (do first)

Microsoft side, with the Partner Center **integration sandbox**:

1. A **dedicated partner service user** (e.g. `m365-automation@partner.com`) with the
   *Admin Agent* role in Partner Center, MFA registered, member of a security group
   `M365 Automation` in the partner tenant.
2. On the **shared app** (`graph_client_id`), add the delegated permissions:
   Partner Center `user_impersonation`; Microsoft Graph `AppRoleAssignment.ReadWrite.All`,
   `Application.Read.All`, `offline_access`. Redirect URI
   `https://<odoo>/m365/partner/signin/callback`.
3. Partner tenant, application permission (already planned):
   `DelegatedAdminRelationship.ReadWrite.All`.

Spike (1 day), to confirm before building Phases 2 and 3:

| Question | How |
|---|---|
| Does `applicationconsents` install the shared app in a new sandbox customer? | Postman, App+User token |
| Does it grant the **application** permissions, or only delegated ones? | Check the service principal's `appRoleAssignments` in the customer |
| Do the GDAP default roles of a customer created with `enableGDAPByDefault` include *Privileged Role Administrator* (needed for `appRoleAssignedTo` on Microsoft Graph)? | `GET .../delegatedAdminRelationships/{id}` |
| Agreement template ID and body accepted by `POST /agreements` | `GET /v1/agreements?agreementType=MicrosoftCustomerAgreement` |

The result decides the consent path of Phase 3 (A alone, or A + B).

## 4. Phase 1: Partner user sign-in (App+User token)

Needed by G3 and G4: Microsoft accepts only App+User (with MFA) for the agreement and the
application consent APIs.

| File / model | Change |
|---|---|
| `models/m365_partner_signin.py` (`_inherit = 'm365.partner.account'`) | `pc_refresh_token`, `pc_user_token`, `pc_user_token_expiry` (`base.group_system`), `pc_user_name`, `pc_user_signin_date`, `pc_user_state` (Not Signed In / Signed In / Expired) |
| | **Sign In as Partner User**: authorization code flow + PKCE on the shared app, scope `https://api.partnercenter.microsoft.com/user_impersonation offline_access`; state signed with `hash_sign()` as for the consent |
| | `_get_user_token(resource, tenant=None)`: refresh token exchange for Partner Center, or for Graph in a **customer** tenant (`login.microsoftonline.com/{customer}/oauth2/v2.0/token`) |
| | `_pc_request(..., as_user=True)`: same client, user token |
| `controllers/partner_signin.py` | `GET /m365/partner/signin/callback` |
| `data/ir_cron_data.xml` | *Keep Partner Sign-in Alive*: weekly token refresh (refresh tokens expire after 90 days unused); activity to the administrators 14 days before the sign-in must be renewed |

Rules:

- Only a **system administrator** can sign in; the refresh token is never shown, logged or
  copied. Changing the shared app credentials clears it.
- The sign-in must carry MFA (`amr` claim checked); otherwise refused.
- Every App+User call is written to the audit log with the signed-in partner user.

## 5. Phase 2: Microsoft Customer Agreement through the API (G4)

| Step | Call |
|---|---|
| Template | `GET /v1/agreements?agreementType=MicrosoftCustomerAgreement` (cached on the account) |
| Attest | `POST /v1/customers/{id}/agreements` `{primaryContact {firstName, lastName, email, phoneNumber}, templateId, dateAgreed, type: "MicrosoftCustomerAgreement"}` |
| Read back | `GET /v1/customers/{id}/agreements?agreementType=MicrosoftCustomerAgreement` |

- New tenant fields: `mca_contact_*` (default: the billing contact), `mca_date`,
  `mca_state` (Not Accepted / Accepted / Failed), `mca_error`. The existing manual record
  (*Purchasing* tab) stays as fallback and fills the same fields.
- File columns: `AgreementFirstName`, `AgreementLastName`, `AgreementEmail`,
  `AgreementPhone`, `AgreementDate` (optional; default billing contact and import date).
- The partner attests that the customer accepted: the import wizard requires the box
  **The customers of this file accepted the Microsoft Customer Agreement**, saved with
  the user and date in the audit log.
- `m365.pc.order._validate()` reads `mca_state`.

## 6. Phase 3: Install the shared app without customer sign-in (G3)

**A. Partner Center application consent** (App+User):

```
POST /v1/customers/{id}/applicationconsents
{"applicationId": "<shared app>",
 "applicationGrants": [{"enterpriseApplicationId": "00000003-0000-0000-c000-000000000000",
                        "scope": "User.ReadWrite.All,Domain.ReadWrite.All,..."}]}
```

**B. Application permissions** (if the spike shows A grants only delegated ones), Graph in
the customer tenant with the partner user's token (GDAP *Privileged Role Administrator*):

```
GET  /servicePrincipals?$filter=appId eq '<shared app>'                     -> our SP
GET  /servicePrincipals?$filter=appId eq '00000003-0000-0000-c000-000000000000' -> Graph SP
POST /servicePrincipals/{graphSp}/appRoleAssignedTo
     {"principalId": "<our SP>", "resourceId": "<graph SP>", "appRoleId": "<role>"}  (one per permission)
```

- Permission list: the one of `doc/tenant_operations_plan.md` section 10, kept in one
  constant; app role IDs read from the Graph service principal (`appRoles`), not
  hardcoded.
- Then the existing `_activate_shared_app()` (reads `/organization`, 3 tries) and Test
  Connection. Idempotent: an existing grant or assignment is skipped.
- New tenant action **Install Shared App** (administrators); *Grant Consent* (customer
  sign-in) stays for tenants without GDAP.
- Audit log: *Consent*, with the partner user.

## 7. Phase 4: GDAP access assignment (G5)

| Step | Call (partner tenant, app-only) |
|---|---|
| Assign the group | `POST /tenantRelationships/delegatedAdminRelationships/{id}/accessAssignments` `{"accessContainer": {"accessContainerId": "<group>", "accessContainerType": "securityGroup"}, "accessDetails": {"unifiedRoles": [...]}}` |
| Status | `GET .../accessAssignments` (`pending`, `active`, `error`) |

- Partner Center account: `gdap_group_id` (security group object ID),
  `gdap_assignment_role_ids` (empty: all the roles of the relationship). Add
  *Privileged Role Administrator* (`e8611ab8-c189-46e8-94e1-60213ab1f814`) to
  `DEFAULT_GDAP_ROLES` (needed by Phase 3 B).
- Tenant: `gdap_assignment_id`, `gdap_assignment_status`. Created as soon as the
  relationship is `active` (immediately for tenants we create with the default GDAP;
  after approval for the others). Roles sent = requested roles ∩ relationship roles.
- `_cron_refresh_gdap` also reads the assignment; the GDAP step is *done* only when both
  are active.

## 8. Phase 5: Customer file (G1, G10)

`m365.tenant.import.wizard`, type New:

| Column | Change |
|---|---|
| `Domain` | A full domain other than `.onmicrosoft.com` (`abc.com`) is taken as the custom domain; the initial name is derived (`abc`) |
| `InitialDomain` (new) | Optional `.onmicrosoft.com` name; default derived from the domain or company name |
| `CustomDomains` (new) | One or more (`;`), the first is the default domain and the UPN domain of the users |
| `Subscriptions` | `<offer> = <qty>`, or only `<qty>`: the default offer of the account (Phase 9) |
| `Agreement*` | Phase 2 |

- **Free initial name**: when the derived name is taken (`HEAD /v1/domains`), the check
  tries `abc1` … `abc9` and shows the chosen name; the tenant stays *Ready* for review
  only when the option *Pick a free name automatically* is off.
- **Users sheet** (optional, same workbook, sheet `Users`): `CompanyName` or `Domain`,
  `UserName` (alias `adele` or full UPN), `DisplayName`, `FirstName`, `LastName`,
  `UsageLocation`, `Licenses`, `Department`, `JobTitle`. Rows become
  `m365.tenant.pending.user` records (Phase 7); checked at import for format and
  duplicates only, the rest when the tenant is ready.
- Options of the wizard: *Run automatically* (sets `auto_provision`, Phase 8),
  *Approve the subscription orders of this file* (the uploading administrator approves;
  audit log), *Make the custom domain the default*.
- New template with both sheets and the no-Teams sample.

## 9. Phase 6: Domains, automatic (G2)

`m365.tenant.domain` gains `state`:

```
To Add --POST /domains--> Waiting for DNS --verify OK--> Verified --(option)--> Default
   |                         |  re-check: 15 min, 1 h, then every 4 h up to 72 h
   +--error--> Failed        +--72 h--> Action Needed (activity + email to the contact)
```

- Fields: `state`, `txt_record` (the `MS=ms…` value alone), `verify_attempts`,
  `next_check`, `error`. Custom domains of the file are created as *To Add* on the tenant.
- Scheduled action *Verify Pending Domains* (15 min): adds, reads the TXT record,
  verifies, makes default, loads the service records. Uses the existing methods of
  `m365_tenant_domain.py`.
- **Domain already in another tenant** (Graph error): *Failed* with that reason, never
  retried automatically.
- **DNS hand-off**: *Send DNS Records* emails the TXT record(s) to the customer contact
  (mail template), and the report has a *Domains* sheet with the record of each domain.
- **DNS provider API (optional, depends on question 1)**: `m365.dns.provider`
  (*Manual* default, *Cloudflare*: `POST /zones/{zone}/dns_records` TXT `@`). Other
  providers on request.

## 10. Phase 7: Users created when the tenant is ready (G10)

`m365.tenant.pending.user`: tenant, alias / UPN, names, usage location, licenses (text),
`state` (Waiting / Created / Licensed / Failed), `user_id`, `error`, `password`
(administrators only).

Created when all of these are true (else they wait, the reason is shown):

1. The tenant is connected.
2. The UPN domain is verified (alias: the default custom domain, or the `.onmicrosoft.com`
   name when the file has no custom domain).
3. Each license is in the tenant inventory with free units. The license text is matched
   on the SKU name, skuPartNumber, or **the Partner Center offer** (`provisioning_id` =
   Graph `skuId`): no SKU ID in the file or the code.

Then the existing `_create_user()` and `_request_licenses()`; the requests are raised in
the name of the administrator who imported the file, so *Skip Approval for
Administrators* applies. Verification: existing scheduled check.

**Passwords**: generated, kept in the admin-only field until **Download Credentials**
(Excel, one file per tenant or all); downloading clears them from Odoo, as the initial
admin password. Never in the chatter, the log or the report.

## 11. Phase 8: Automatic run (G6)

- Tenant: `auto_provision` (Boolean), `prov_attempts`, `prov_next_run`, `prov_waiting`
  (reason shown in the list: *Waiting for DNS of abc.com*, *Order waiting approval*…).
- Steps (`STEPS`) become: tenant, GDAP, agreement, connection (install app),
  configuration, domains, subscriptions, users, licenses, verification. Each step method
  returns *done / waiting / failed*; the job runs steps one after the other in the same
  run while they finish at once.
- Scheduled action *Run Automatic Provisioning* (5 min): tenants with `auto_provision`,
  not failed, `prov_next_run` past; 20 per run, commit per tenant, re-triggers itself
  when more are left (same pattern as `_cron_provision_tenants`).
- Retries with backoff for errors Microsoft may clear (throttling, 5xx, "not found yet"
  right after creation); after 5 tries the step is *Failed* and the administrators get an
  activity. Errors that need a person (domain in another tenant, address refused) stop at
  once.
- *Next Step* stays for manual use. List action **Run Automatically** / **Stop**.
- Partner Center order approval: automatic only when the import option says so (Phase
  5); otherwise the step waits and *Approve and Order* works on several orders from the
  list.

## 12. Phase 9: Offers, catalog countries, license names (G8, G9, G11)

- Partner Center account: `default_offer_id` (*Default Offer for Provisioning*, chosen
  from the synced catalog, e.g. *Microsoft 365 Business Basic (no Teams)*, monthly).
- Offer matching: catalog item ID, then exact title / SKU title; **several matches: an
  error listing them** (no silent first match); the preference for monthly terms only
  breaks ties between terms of the same SKU.
- `m365.pc.offer.country`: the sync reads the account country **and** each country of
  the tenants (`country_id`); the key and the order line domain include the country;
  the requested subscriptions are matched in the tenant's country. Post-migration: the
  existing offers get the account `catalog_country`.
- License names: when the inventory sync meets a skuPartNumber without catalogue
  product, it creates one named after the Partner Center offer with the same
  `provisioning_id`; otherwise the skuPartNumber, as today.
- Offer list: search filter *No Teams*, *Monthly*.

## 13. Phase 10: Report (G7)

`/m365/export/provisioning`, one workbook:

| Sheet | Columns |
|---|---|
| Summary | Existing row per tenant + initial admin user, custom domains, agreement, app installed, GDAP assignment |
| Domains | Tenant, tenant ID, domain, initial / custom, state, default, TXT record, last check, error |
| Users | Tenant, tenant ID, UPN, display name, enabled, usage location, created on, state, error |
| Licenses | Tenant, UPN, license, skuPartNumber, request, assignment state, verification, details, date |
| Subscriptions | Tenant, order, offer, quantity, subscription ID, Partner Center status, done on |

Optional: *Email Report* to a list of addresses, daily while tenants are in progress.
No passwords in the report.

## 14. Security

- Refresh token of the partner user: `base.group_system` only, cleared when the app
  changes, never logged. It can act on every customer: use the dedicated service user,
  not a person.
- Automatic orders spend money: only from a file whose uploader ticked the approval
  option; the audit log names that administrator.
- Agreement attestation, app installation, GDAP assignment, domain changes: audit log
  entries, as for the existing operations.
- New models: administrators only, multi-company rules; pending users readable by users
  of the tenant, passwords never.

## 15. Testing

Mocked Partner Center and Graph, new test files per phase (`test_m365_partner_signin.py`,
`test_m365_agreement.py`, `test_m365_app_install.py`, `test_m365_gdap_assignment.py`,
`test_m365_domain_auto.py`, `test_m365_pending_user.py`, `test_m365_auto_provision.py`,
`test_m365_report.py`) and updates to the import and offer tests. Run on the test
database only:

```
odoo-bin -c <config> -d m365_test_claude -u m365_license_management \
         --test-tags /m365_license_management --stop-after-init
```

Key cases: token refresh and MFA check; agreement body and template; app install
idempotent, roles read from Graph; assignment only when the relationship is active;
domain backoff, 72 h stop, domain in another tenant; user waits for the domain and the
licenses; credentials cleared after download; automatic run stops on errors needing a
person, retries the others; ambiguous offer refused; offers per country; every sheet of
the report.

Sandbox acceptance: a file of 3 customers (one with a domain already used, one with a
custom domain, one with users) runs from upload to report with no click except the DNS
record.

## 16. Order and effort (estimate)

| Phase | Work | Days |
|---|---|---|
| 0 | Prerequisites, sandbox spike | 1 |
| 1 | Partner user sign-in | 1.5 |
| 2 | Agreement API | 0.5 |
| 3 | Install shared app | 1.5 |
| 4 | GDAP access assignment | 0.5 |
| 5 | Customer file | 1.5 |
| 6 | Domains automatic (+1 day for Cloudflare) | 1.5 |
| 7 | Pending users and credentials | 1.5 |
| 8 | Automatic run | 1.5 |
| 9 | Offers, countries, names | 1 |
| 10 | Report | 1 |
| | Sandbox acceptance, docs | 1 |
| | **Total** | **≈ 14 (15 with Cloudflare)** |

Phases 9 and 10 do not depend on the App+User work and can start at once; 4, 5 and 6 can
run in parallel with 1–3.

## 17. Questions for the client

1. Who manages the DNS of the client domains? (Manual TXT hand-off, or an API: Cloudflare,
   GoDaddy, Azure DNS.)
2. Have the customers accepted the Microsoft Customer Agreement, and who is the contact
   per customer? The partner attests it to Microsoft.
3. Approve the orders of an uploaded file automatically, or keep one approval per order?
4. How are user passwords handed over? (Credentials file to the client, or email to each
   user's contact.)
5. Make the custom domain the default domain of each tenant?
6. Are we buying in Partner Center directly (direct bill / indirect provider)? If through
   Ingram Micro, the order step uses the existing Ingram flow instead.

## 18. As built (19.0.1.8.0)

| Phase | Files | Notes |
|---|---|---|
| 1 Partner user sign-in | `models/m365_partner_signin.py`, `controllers/partner_signin.py` | *Sign In as Partner User* (system administrators), PKCE, MFA claim checked, refresh token rotated, weekly *Keep the Partner User Signed In* with a reminder after 76 days. `_pc_request` now shares `_pc_send` with `_pc_user_request` |
| 2 Agreement | `models/m365_tenant_agreement.py` | Reads the customer's agreements first (idempotent), then attests with the cached template; *Customer Accepted the Agreement* required |
| 3 Shared app | `models/m365_tenant_app_install.py` | Partner Center consent (409 = already there), then the missing app roles only; permissions and delegated scopes editable on the account |
| 4 GDAP | `models/m365_tenant_gdap.py` | *GDAP Security Group* and *Roles of the Group* on the account; roles sent = wanted ∩ relationship; *Give Roles to Group* on the tenant. Privileged Role Administrator added to the default roles (migration updates accounts still on the former default) |
| 5 Customer file | `models/m365_tenant_import.py`, `models/m365_file_import.py` | Columns `CustomDomains`, `Agreement*`, alias `InitialDomain`; a full domain in `Domain` is split; sheet `Users` or a separate users file; options of section 8; template with both sheets |
| 6 Domains | `models/m365_tenant_domain.py`, `models/m365_tenant_config.py`, `data/m365_mail_template_data.xml` | Domain `state`, TXT record, retry delays and 72 h limit as planned; *Add and Verify Custom Domains* every 15 minutes; *Send DNS Records* (mail template); *Retry*. Synchronisation keeps the domains not added yet |
| 7 Users | `models/m365_pending_user.py` | Menu *Users to Create*, tenant tab; *Create Waiting Users* every 15 minutes; *Download Credentials* (`/m365/export/credentials`) clears the passwords |
| 8 Automatic run | `models/m365_tenant_provisioning.py` | Steps run when the steps they need are done (`STEP_NEEDS`), up to 3 passes per run; *Run Automatically* / *Stop* (form, list, action menu); *Run Automatic Provisioning* every 5 minutes |
| 9 Offers | `models/m365_pc_order.py`, `models/m365_tenant.py` | Offer `country`, *Default Offer*, ambiguity error, country check on orders, product names from the offers |
| 10 Report | `controllers/export.py` | Sheets Provisioning, Domains, Users, Licenses, Subscriptions |

Differences from the plan:

- Configuration is not a separate step: it is still applied when the tenant connects
  through the shared app (`_after_shared_app_connected`).
- The Users sheet is read from an Excel customer file; a CSV customer file takes its users
  in the separate *Users File*.
- Two scheduled actions can process the same user or domain (their own one and the
  automatic run): the row is locked (`FOR UPDATE SKIP LOCKED`) so Microsoft is called once.

Not built (optional, waiting for the answers of section 17):

- DNS provider API (Cloudflare): question 1.
- Daily email of the report.

Tests: `tests/test_m365_bulk_provisioning.py` (29 tests) and updates to
`tests/test_m365_provisioning.py` (step order, 6 GDAP roles).

