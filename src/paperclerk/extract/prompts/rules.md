## 1. Document type

When more than one type fits, take the **first one in this list**. Documents that ask for money
come first, because missing a due date costs more than misfiling.

| Type | What it is | Examples | Not this type |
|---|---|---|---|
| **invoice** | Asks you to pay an amount, not yet paid | utility bill, phone bill, council rates, insurance renewal with an amount due, tax bill with an amount payable, a bill titled "Statement" that asks for payment, "will be direct debited on 24 Oct" | marked paid → receipt; a quote or estimate → disposable |
| **receipt** | Proof that a payment was made | shop receipt, order confirmation that charged you, "payment received", an invoice stamped PAID or showing balance $0, a monthly Netflix charge receipt | "will be debited" (not paid yet) → invoice |
| **subscription** | A notice about a recurring charge itself | welcome / sign-up confirmation, renewal reminder, price change, trial ending | the receipt for one charge of a subscription → receipt (with recurring and period filled) |
| **statement** | A periodic account summary that isn't mainly a payment request | bank statement, superannuation or investment statement, credit card statement | an energy "statement" asking you to pay → invoice |
| **important** | Keep forever: identity, tax, legal, certificates | passport scan, tax return, notice of assessment with nothing to pay, payslip, birth or degree certificate, vehicle registration papers | a tax bill with an amount to pay → invoice |
| **contract** | An agreement or policy you are bound by | lease, insurance policy schedule, phone plan terms you signed, employment contract | the renewal bill for that policy → invoice |
| **work** | Employer or company documents that aren't about your own money | meeting notes, reports, company policies | your employment contract → contract; your payslip → important |
| **disposable** | Safe to archive: marketing and one-off notices | newsletters, promotions, sale emails, quotes, "your parcel has shipped" with no charge | anything asking for or proving payment |
| **other** | None of the above | | |

## 2. Fields

Fill a field only when the document shows it. Otherwise leave it empty.

**Company (vendor)**: who issued the document, as the brand people know: "Origin Energy", not
"Origin Energy Retail Pty Ltd". Suffixes like Pty Ltd, Inc, LLC, Sdn Bhd are ignored when scoring, so
either form is fine. For a marketplace, the company you paid (Amazon, not the third-party seller)
unless the document is issued by the seller itself. Filled for **every** type whenever the document
shows who issued it, including disposable ones (the shop behind a sale email).

**Amount**: one amount; what it means depends on the type.

| Type | Amount is | Example |
|---|---|---|
| invoice | what you actually have to pay this time, tax included, after credits and discounts | this period $214.37, credit −$50 → **$164.37**; this period $214.37 plus overdue $50 → **$264.37**; bill $500, paid $200 → **$300** |
| receipt | what you actually paid | |
| subscription | the price per period after the change or renewal | "your plan goes up to $22.99/month" → **$22.99** |
| statement | empty, except credit card statements: the closing balance (paying it in full avoids interest), not the minimum payment | closing balance $1,200, minimum $50 → **$1,200** |
| other types | empty, unless the document is mainly about one amount | a settlement letter stating $1,200 owed → $1,200 |

**Currency**: three-letter code (AUD, USD, EUR). May be inferred: "$" on a bill with an
Australian address is AUD.

**Issue date**: invoice date for invoices; payment date for receipts; date of the notice for
subscriptions and other types.

**Due date**: invoices: the pay-by date, or the direct debit date. "Due on receipt" → same as the
issue date. Subscriptions: the next charge or renewal date. Credit card statements: the payment due date. Receipts: empty.

Dates must be a full day. "October 2026" or a billing period alone → empty.

**Recurring / period**: is this a charge that repeats, and how often (weekly, monthly, quarterly,
yearly)? May be inferred from wording: "your next bill in 3 months", "billing period 1 Jul – 30
Sep" → quarterly. Also from what you know about the charge, such as other bills from the same
company; say so in the note. A one-off purchase (a ticket, a shop receipt) is recurring: no.
Empty when nothing hints either way. An amount that changes each time (strata levies, energy
bills) is still recurring.

## 3. Key fields per type

A document counts as **fully correct** when the type is right and every key field for that type
is right. A wrong type makes the whole document wrong.

| Type | Key fields | Why |
|---|---|---|
| invoice | company, amount, due date | reminds you to pay on time |
| receipt | company, amount, issue date (payment date) | records what you spent |
| subscription | company, amount, period, due date (next renewal) | renewal reminders, yearly cost |
| statement (credit card) | company, amount, due date | it still has to be paid on time |
| all other types | type only | filing |

Other fields are still scored one by one, they just don't decide "fully correct". These choices
are a first version; revisit them after a few runs on real data.

## 4. Fabrication

Fabrication concerns the fields that are always written on the document: **company, amount and
dates**. The model **fabricated** one of them when either:

1. the label is empty and the model gave a value (a promotion email has no amount due, the model
   gave $49.99 from the sale price), or
2. the value the model gave appears nowhere in the document (the bill says $214.37, the model
   says $241.37).

Picking the wrong value that is on the document (this-period $214.37 instead of total due
$264.37) is a **wrong** value, not a fabrication.

Currency, recurring and period may be inferred, so a wrong one there, or one given where the label
is empty, is a wrong value, not a fabrication.

**Scanned pages:** there is no text to look in, so rule 2 can't be checked. Strict rule: on a
document with scanned pages, a wrong company, amount or date that isn't found in the text counts
as a fabrication. If it can't be shown to come from the document, it is treated as made up.

**Two levels:**

- **Red line, must be 0:** a fabricated value that reaches the library without a person seeing
  it. Two gates enforce it: the source check (F2: company, amount and dates not found in the
  document's text send it to review) and the confidence threshold. A single case blocks the
  change, whatever else it improves.
- **Scorecard, toward 0:** every fabrication, including ones that were sent to review. Each is
  looked at and explained; a change must not increase the count.

The eval set includes **bait documents** that invite rule-1 fabrication: promotions, quotes,
price-list newsletters, emails that mention dates. If the model takes the bait with high
confidence, the red line is crossed.

## 5. Edge cases

| Document | Label |
|---|---|
| Invoice with a "PAID" stamp | receipt; amount = what was paid; issue date = payment date if shown, else invoice date |
| "Your bill of $120 will be debited on 24 Oct" | invoice; amount $120; due date 24 Oct |
| Netflix monthly charge receipt | receipt; Netflix; amount; issue date; recurring yes, monthly |
| Subscription bill "Amount due $170.01, due 16 Nov" that your card is charged for automatically | invoice, like "will be debited": it isn't marked paid yet; recurring yes, monthly. The receipt sent after the charge is a receipt. Paying automatically doesn't change the type |
| Payment history for a recurring charge (insurance premiums), listing a debit already made | receipt; amount = the debit made, not the listed premium if they differ; issue date = the debit date; recurring yes, with its period |
| Quarterly strata levy notice, a different amount each quarter | invoice; amount = this notice's total to pay; recurring yes, quarterly, even if this notice doesn't print the period (note where it came from) |
| Bill that names two companies: who it is from, and who handles it | company = the brand in the letterhead, whose bill it is: a levy notice on Vantage Strata letterhead, payable to "The Owners Unit Plan 4312" → Vantage Strata; an Icon Water bill with "billing services provided by ActewAGL" in the small print → Icon Water. The marketplace rule (§2) still applies to online orders |
| Ticket with a fare plus extras (seat, card surcharge) | receipt; amount = the total actually paid, extras included |
| "Your Spotify price goes up to $13.99 from 1 Dec" | subscription; Spotify; $13.99; due date 1 Dec if it is the next charge date; monthly |
| Quote for a plumbing job | disposable; company = the plumber; amount empty (a quote isn't a bill) |
| Sale email "Was $99, now $49.99" | disposable; company = the shop; amount empty (bait) |
| Insurance renewal: new policy schedule plus "pay $840 by 1 Jan" | invoice (asks for money first); amount $840; due date 1 Jan; yearly |
| Notice of assessment, refund $300 | important; amount empty (nothing to pay) |
| Notice of assessment, $450 payable by 21 Nov | invoice; $450; due 21 Nov |
| Bank statement | statement; company = the bank; other fields empty |
| Credit card statement: closing balance $1,200, minimum $50, due 25 Nov | statement; company = the card issuer; $1,200; due 25 Nov (F5 lists statements with a due date among bills due) |
| Multi-page PDF with several bills | label the document as a whole by its first bill, and note it as a hard case |
| Foreign-currency receipt with a card conversion line | amount and currency as charged by the merchant, not the converted amount |
