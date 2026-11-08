## Branch Sales Variance

This app does branch sales, banking, stock and cashier variance reporting for ERPNext POS branches. It was built for Lyons Liqour Lodge and replaces the manual "RUIRU Bank Reconciliation" workbook.

**Requires:** Frappe v16, ERPNext v16 and Python 3.14. POSNext is optional; it supplies POS Closing Shift counts.

### Install

**Self-hosted bench**

```bash
cd ~/frappe-bench
bench get-app https://github.com/<you>/branch_sales_variance --branch version-16   # or: bench get-app /path/to/branch_sales_variance
bench --site <site> install-app branch_sales_variance
bench --site <site> migrate
bench restart
```

**Frappe Cloud:** push this folder to a GitHub repo with a `version-16` branch, add it under Apps on your v16 bench group, deploy, then install it on the site.

Open it at **/app/query-report/Branch Sales Variance**.

### What gets installed

| Type | Name | Purpose |
|---|---|---|
| Script Report | Branch Sales Variance | Five views: Daily Money, Cashier Summary, Stock Variance, Stock Movement, Returns Check |
| DocType (submittable) | Branch Daily Takings | Manual fallback / correction figures per channel per day |
| Child DocType | Branch Takings Line | Lines of the above |

Permissions go to Accounts Manager, Accounts User and System Manager.

### Views

| View | What it answers |
|---|---|
| Daily Money | Did each day's money reach the banks and the drawer? Click a channel row to drill down. |
| Channel Detail | GL-style drill-down for one channel: every invoice, Payment Entry, statement line and other entry behind its Daily Money line |
| Cashier Summary | Sales, returns, collections and close-of-shift difference per cashier |
| Stock Variance | What did each Stock Reconciliation count find, valued at the selling price? |
| Stock Movement | Opening + purchases + transfers − sales = closing for every item that moved; which items weren't counted; which went negative |
| Returns Check | Risky credit notes: no original invoice, stock not returned, sold at another branch, late |

### Where Daily Money's statement comes from

For each channel and day, the report uses the first of these that exists:

1. **Branch Daily Takings** (manual override)
2. **Imported Bank Transactions** for the channel's bank account
3. **POS Closing Shift** counted amounts (POSNext). Shifts closed with all-zero counts are ignored and counted in the summary.

For banks with an imported statement, the non-sales amount is everything else booked into that bank account that day per the GL (other branches, old debts, transfers, journals). That makes the variance a statement-vs-books check for the account.

**Invoiced Sales** = payments allocated to the day's invoices *whenever paid*, plus a Credit / Unpaid line. It ties to **Sales Register by Mode of Payment**, and the date total ties to the Sales Register.

It is then bridged to what should have hit the bank or drawer that day:

```
Invoiced Sales
 − Paid later / unpaid          (invoice today, money on a later date)
 − Paid in advance              (invoice today, money received earlier)
 + Old invoices paid today
 + Advances received today
 + Unallocated receipts         (branch cashiers' receipts not yet linked to an invoice)
 = Expected Receipts
Variance = Statement − Other money into the account − Expected Receipts
```

### Configuration

Channel ↔ Mode of Payment ↔ Bank Account mapping is the `CHANNELS` list at the top of
`branch_sales_variance/branch_sales_variance/report/branch_sales_variance/branch_sales_variance.py`.
Edit it if you add a bank or rename a mode of payment. Anything unmapped falls under "Other".

The stock value uses the branch POS Profile's selling price list (e.g. Retail-Ruiru).

### Known limits

- Equity and NCBA receive money from every branch. A branch is only blamed for a gap if its own sales were booked into that bank.
- Banking lags show up as +/- swings on neighbouring days. Use Group By = Week or Month to net them out.
- A cash count from the system needs cashiers to close one POS shift per day **with counted amounts**.
