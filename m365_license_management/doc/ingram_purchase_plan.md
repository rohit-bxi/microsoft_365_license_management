# Buying Microsoft 365 Licenses through Ingram Micro: Implementation Plan

Buy *Microsoft 365 Business Basic (no Teams) (NCE COM MTH)* (vendor part number
`CFQ7TTC0LH18:000P`, billing 1 month, subscription period 1 month or 1 year) through the
Ingram Micro Reseller API, in bulk, from the customer file, as the indirect-reseller
alternative to Partner Center orders.

Status: **implemented in 19.0.1.9.0** (section 10), tested with Ingram Micro and Graph
mocked (277 tests, all passing). Not yet run against Ingram: the API key is still refused
and questions Q1 to Q5 are open. Builds on the Ingram ordering of 19.0.1.7.0
(`doc/tenant_operations_plan.md`, section 8) and the bulk provisioning of 19.0.1.8.0
(`doc/bulk_provisioning_gap_plan.md`).

## 1. What exists and what is missing

| Area | Today (19.0.1.8.0) | Missing |
|------|--------------------|---------|
| Connection | Token, IM-* headers, sandbox URL (token at the host root), clear error when the app has no API product | Working credentials (Ingram side) |
| Catalog | Plans, groups, part numbers, min/max, price per plan | Subscription periods as data; a default plan; *No Teams* filter |
| Order | `POST /resellers/v7/orders`, PO number, end user, vendor fields, approval, webhook, lookup, cancel, delivery check | **Subscription period** of each line; ordering without a mapped catalogue product |
| Provisioning | Subscriptions step, customer file, automatic run, report: **Partner Center only** | The same with Ingram orders |
| Tenants | New tenants only through Partner Center | Bulk license and user provisioning of **existing** tenants from a file; shared app without Partner Center credentials |

The Ingram Reseller API cannot create tenants: with Ingram, tenants exist already (created
by the customer, or by Ingram if its marketplace offers it: question Q5).

## 2. Target flow

```
Customer file (existing tenants + Subscriptions + Users)
  -> connect (shared app consent, or own app)                           (existing)
  -> confirm Ingram relationship + Customer Agreement (file columns)    (Phase 3)
  -> Ingram order: plan + term + quantity, vendor fields                (Phases 1, 2, 3)
  -> Ingram confirms (webhook / lookup) -> licenses seen in Graph       (existing)
  -> users created and licensed, verified, report                       (existing, Phase 3)
```

## 3. Phase 0: Prerequisites and questions to Ingram (first)

Ingram side:

1. Client ID / Secret of the **approved** app (today: "no API product" on every call), and
   whether it is the sandbox or the production app (API URL
   `https://api.ingrammicro.com:443/sandbox` or without `/sandbox`).
2. Net terms on account `63-BXTELL` (the order API takes no card).
3. Webhook registered with `https://<odoo>/m365/ingram/webhook/<account id>`, and its
   secret key.
4. A sandbox quote for the plan (to read the vendor fields with *Import Vendor Fields*).

Questions (answers change Phases 1 and 3):

| # | Question | Effect |
|---|----------|--------|
| Q1 | How does the order API choose the subscription period (1 month / 1 year): one plan or part number per period, or a line attribute (name, values)? | Phase 1, option A or B |
| Q2 | Which vendor fields (`vmfAdditionalAttributes`) are mandatory for Microsoft NCE orders, at order or line level, with their names and formats? | Vendor Fields of the account |
| Q3 | Does the price and availability API return a price per period? | Price per line |
| Q4 | How is a customer tenant linked to us as reseller (invitation accepted by the customer, or created by Ingram)? Can the API tell whether it is linked? | Phase 3 relationship check |
| Q5 | Can a Microsoft order (or another Ingram API) create a **new** tenant, e.g. with a domain prefix vendor field? | Tenant creation without Partner Center (out of this plan if no) |

## 4. Phase 1: Subscription period of each line (Q1)

| File / model | Change |
|---|---|
| `m365.ingram.plan.term` (new, in `models/m365_ingram_plan.py`) | One record per period of a plan from `subscriptionPeriodSummary`: period, unit (month/year), label (*1 Year*), code sent to Ingram |
| `m365.ingram.plan` | `term_ids`; `term` kept as the summary text |
| `m365.ingram.order.line` | `term_id` (required when the plan has several periods; default: the account's default period, else the only one) |
| `m365.ingram.account` | `default_term` (*1 Year* / *1 Month*) |
| Order body | **Option A** (one plan per period): the line's plan already is the period, `term_id` only filters the plans. **Option B** (line attribute): new vendor field source *Subscription Period (lines)*, so the attribute name given by Ingram is entered on the account, not coded |
| Prices | Read per period when Q3 says so; else the plan price |

Checks: an order line without a period on a multi-period plan is refused at Submit.

## 5. Phase 2: Choosing the plan without hardcoding

- **Default Plan** and **Default Period** on the Ingram account, chosen from the synced
  catalog, e.g. *Microsoft 365 Business Basic (no Teams) (NCE COM MTH)*, *1 Year*.
- Plan matching for the file and the license names: plan ID, vendor part number
  (`CFQ7TTC0LH18:000P`), Ingram part number, exact plan name or name without the
  `(NCE COM MTH)` suffix; several plans: error listing them (as for Partner Center offers).
- Plan list: filters *No Teams*, *Monthly Billing*, column vendor part number.
- **Catalogue product not needed to order**: today `_validate` refuses a plan without
  catalogue product, but the product of a new license appears only after its first
  delivery. New rule: the order can be sent; the delivery check takes the license whose
  units grew by the ordered quantity when the plan has no product, then maps the plan to
  that product (logged), so the next orders use it.

## 6. Phase 3: Ingram in the bulk provisioning

| Part | Change |
|---|---|
| Channel | *Buy Licenses Through* = Ingram Micro on the Partner Center account, or no Partner Center account at all: the Ingram account decides |
| Subscriptions of the tenant | `requested_subscriptions` parsed against the Ingram plans (`<plan> = <qty>`, `<plan> 1 Year = <qty>`, or `<qty>` for the default plan and period); creates an `m365.ingram.order`, submitted, approved when *Approve Orders Automatically* |
| Steps | *Subscriptions* step reads the Ingram orders: draft/submitted → action, sending/sent/confirmed → pending, delivered → done, failed/cancelled → failed. New step **Ingram Relationship** (done when confirmed on the tenant), needed by Subscriptions |
| Agreement step | With Ingram: done when the acceptance is recorded (no Partner Center attestation); the values are sent through the vendor fields |
| Automatic run | Handler of the Subscriptions step for Ingram: create, approve, send, retry a failed order (same PO number: lookup first), follow the delivery |
| Customer file, type **Existing** | New optional columns `Subscriptions`, `CustomDomains`, Agreement columns, `IngramRelationship` (yes/no); the **Users** sheet; options *Run Automatically*, *Approve Orders*, *Customers Accepted the Agreement*, *Customers Linked to Ingram Micro* |
| Users file licenses | `default` = the product of the default Ingram plan; an Ingram plan name or VPN is accepted |
| Report | Subscriptions sheet: the Ingram order lines too (PO number, Ingram order number, plan, period, quantity, status) |
| Relationship check (Q4) | If Ingram offers an API, a check before ordering; else the confirmation of the file / tenant |

## 7. Phase 4: Shared app without Partner Center

Today the shared app (one app consented in every tenant) is stored on the Partner Center
account, whose Partner Center fields are required. Make *Partner Directory ID*,
*Application ID* and *Secret* of Partner Center optional when the account only carries the
shared app (and GDAP is not used): Test Connection, catalog and tenant creation then say
that Partner Center is not configured. Existing tenants connect with *Grant Consent*
(Global Administrator of each customer, once).

## 8. Phase 5: Tests and acceptance

Automated (Ingram and Graph mocked, test database only): periods read from the catalog;
line period required; option A/B order bodies; vendor field source *Subscription Period*;
default plan and period; ambiguous plan; order without catalogue product and mapping on
delivery; Ingram Subscriptions step and automatic run (create, approve, webhook confirmed,
delivered); existing-tenant file with subscriptions and users; report lines; Partner
Center account without Partner Center credentials.

Sandbox acceptance (Ingram sandbox app):

1. Test Connection, Synchronise Catalog: the plan `CFQ7TTC0LH18:000P` with 1 Month and
   1 Year.
2. Import Vendor Fields from a sandbox quote.
3. One existing test tenant, linked to Ingram, agreement recorded: order 1 license,
   1 Year, approved automatically.
4. Webhook: *Confirmed*; the license appears: *Delivered*; the plan is mapped.
5. A user of the file is created and licensed; the report shows the order.

## 9. Order and effort (estimate)

| Phase | Work | Days |
|---|---|---|
| 0 | Ingram credentials, answers Q1 to Q5 | Ingram |
| 1 | Subscription period | 1.5 |
| 2 | Default plan, matching, order without product | 1 |
| 3 | Ingram in the bulk provisioning, existing-tenant file | 2.5 |
| 4 | Shared app without Partner Center | 0.5 |
| 5 | Tests, sandbox acceptance, documentation | 1.5 |
| | **Total** | **≈ 7** |

Phases 2 and 4 do not depend on the answers of Ingram and can start at once; Phase 1 needs
Q1, Phase 3 needs Q4 for the relationship check only.

## 10. As built (19.0.1.9.0)

| Phase | Files | Notes |
|---|---|---|
| 1 Periods | `models/m365_ingram_plan.py` (`m365.ingram.plan.term`), `models/m365_ingram_account.py`, `models/m365_ingram_order.py` | Periods read on every catalog sync (12 months = 1 year); *Value Sent* editable and kept on later syncs; line *Subscription Period* (default: the account's); required at Submit when the plan has several. Vendor field sources *Subscription Period* (catalog value), *in Months*, *ISO* (P1Y): option B of Q1 needs only the attribute name from Ingram |
| 2 Plans | `models/m365_ingram_account.py`, `models/m365_ingram_order.py` | *Default Plan* / *Default Subscription Period*; `_match_plan`: plan ID, vendor or Ingram part number, name, name without (NCE …), optional period at the end; one plan per period chosen by the period; otherwise an error listing the plans. A plan without product can be ordered: the SKUs of the tenant are saved when sending and the plan is mapped to the license that grew by the ordered quantity |
| 3 Provisioning | `models/m365_tenant_provisioning.py`, `models/m365_pending_user.py`, `models/m365_tenant_import.py`, `controllers/export.py` | Channel = *Buy Licenses Through*, or Ingram when there is no Partner Center account. New step *Ingram Relationship*; Agreement through Ingram = recorded only; Subscriptions step and automatic run with Ingram orders (create, auto-approve, retry, delivery). Existing-tenant file: Subscriptions, CustomDomains, Agreement*, IngramRelationship, Users sheet / file, all options, new option *Customers Accepted the Reseller Relationship of Ingram Micro*. Users: `default` = product of the default plan, plan names and part numbers accepted. Report: Ingram lines in *Subscriptions* |
| 4 Without Partner Center | `models/m365_partner_account.py`, `models/m365_tenant_gdap.py`, `models/m365_partner_signin.py` | Partner Center ID, app ID and secret optional; Test Connection explains it; GDAP and the partner sign-in ask for the partner directory ID |
| 5 Tests | `tests/test_m365_ingram_bulk.py` (11 tests), updates to `tests/test_m365_ingram.py` and `tests/test_m365_bulk_provisioning.py` | |

Migration: the periods of the plans already read are created from their *Term* text.

Not built: price per period (Q3), the relationship check through an Ingram API (Q4), tenant
creation through Ingram (Q5).

