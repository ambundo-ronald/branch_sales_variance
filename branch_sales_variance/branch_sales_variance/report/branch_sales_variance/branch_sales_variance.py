# Branch Sales Variance — Lyons Liqour Lodge
# Rebuilds the "RUIRU Bank Reconciliation" workbook inside ERPNext.
#
# Views
#   Daily Money    : statement vs system sales per channel (Sheet1 of the workbook)
#   Cashier Summary: per-cashier sales, returns, collections and close-of-shift difference
#   Stock Movement : opening + movements = closing per item, count coverage
#   Stock Variance : stock reconciliation differences valued at selling price (Variance sheet)
#   Returns Check  : credit notes with risk flags
#
#   Channel Detail : GL-style drill-down of one channel (click a channel row in Daily Money)
#
# Money logic per channel and day
#   Invoiced          = payments allocated to the day's invoices, whenever paid
#                       (ties to Sales Register by Mode of Payment; unpaid shows as Credit / Unpaid)
#   Expected receipts = Invoiced - paid later - paid in advance + old invoices paid today
#                       + advances received today + unallocated receipts
#   Statement         = manual takings > imported Bank Transactions > POS Closing Shift count
#   Other money in    = everything else booked into that bank account that day (GL)
#   Variance          = Statement - Other money in - Expected receipts   (negative = short)

import re

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, flt, formatdate, getdate

# key, label, modes of payment, bank account (Bank Account doctype) or None
CHANNELS = [
    ("equity", "Equity Bank", ["Equity Paybill"], "Equity Banlk - Equity Bank"),
    ("im", "I&M Bank", ["I&M Paybill"], "I&M Bank - I&M Bank"),
    ("ncba", "NCBA", ["Ncba Paybill"], "NCBA Bank - NCBA Bank"),
    ("coop", "Co-op Bank", ["Co-op Bank"], "Co-operative bank - Co-op Bank"),
    ("pochi", "Pochi", ["Pochi"], None),
    ("cash", "Cash", ["Cash"], None),
    ("other", "Other", [], None),  # any mode of payment not mapped above
]
# Statement lines whose description contains one of these are cash being banked. They count as the
# Cash channel's statement (when they name one of the branch's cashiers), not as bank receipts.
CASH_DEPOSIT_PATTERNS = ("CASH DEPOSIT",)

CHANNEL_LABEL = {c[0]: c[1] for c in CHANNELS}
LABEL_CHANNEL = {c[1]: c[0] for c in CHANNELS}
MOP_CHANNEL = {mop: c[0] for c in CHANNELS for mop in c[2]}

BRANCH_COND = """(si.pos_profile = %(pos_profile)s
    OR (IFNULL(si.pos_profile, '') = '' AND si.cost_center = %(cost_center)s))"""


def execute(filters=None):
    filters = frappe._dict(filters or {})
    validate(filters)
    view = filters.get("view") or "Daily Money"
    if view == "Stock Variance":
        return stock_variance(filters)
    if view == "Stock Movement":
        return stock_movement(filters)
    if view == "Cashier Summary":
        return cashier_summary(filters)
    if view == "Channel Detail":
        return channel_detail(filters)
    if view == "Returns Check":
        return returns_check(filters)
    return daily_money(filters)


def validate(f):
    if not f.get("pos_profile"):
        frappe.throw(_("Select a branch (POS Profile)"))
    if getdate(f.from_date) > getdate(f.to_date):
        frappe.throw(_("From Date must be before To Date"))
    prof = frappe.db.get_value(
        "POS Profile", f.pos_profile,
        ["company", "cost_center", "warehouse", "selling_price_list"], as_dict=True)
    f.company = f.get("company") or prof.company
    f.cost_center = prof.cost_center
    f.warehouse = prof.warehouse
    f.price_list = prof.selling_price_list or "Standard Selling"


# --------------------------------------------------------------------------- money
def period_key(d, period):
    d = getdate(d)
    if period == "Week":
        return add_days(d, -d.weekday())
    if period == "Month":
        return d.replace(day=1)
    return d


def channel_of(mop):
    return MOP_CHANNEL.get(mop, "other")


RECEIPT_KEYS = ("invoiced", "paid_later", "paid_earlier", "old_paid", "advance", "unallocated")
MONEY_KEYS = RECEIPT_KEYS + ("receipts", "statement", "other_in", "variance", "unchecked")


def mop_condition(ch, alias="pe"):
    """SQL condition + params selecting the modes of payment of one channel."""
    if ch == "other":
        return f"IFNULL({alias}.mode_of_payment, '') NOT IN %(mapped_mops)s", {
            "mapped_mops": tuple(MOP_CHANNEL) or ("",)}
    return f"{alias}.mode_of_payment IN %(ch_mops)s", {
        "ch_mops": tuple(dict((c[0], c[2]) for c in CHANNELS)[ch]) or ("",)}


def bank_channel(f):
    """Channel key for the Bank Account filter, or None when no bank is picked."""
    ba = f.get("bank_account")
    if not ba:
        return None
    for c in CHANNELS:
        if c[3] == ba:
            return c[0]
    frappe.throw(_("Bank Account {0} is not linked to a channel. Add it to CHANNELS in the report.").format(ba))


def branch_users(f):
    return [r[0] for r in frappe.db.sql(
        "SELECT user FROM `tabPOS Profile User` WHERE parent = %s", f.pos_profile)] or [""]


def bank_setup(f):
    """Every channel that has a Bank Account, with its GL account."""
    bank_accounts = {c[0]: c[3] for c in CHANNELS if c[3]}
    if not bank_accounts or not f.get("use_bank_statements", 1):
        return {}, {}
    gl_account = dict(frappe.db.sql(
        "SELECT name, account FROM `tabBank Account` WHERE name IN %(n)s",
        {"n": tuple(bank_accounts.values())}))
    ch_bank = {k: v for k, v in bank_accounts.items() if v in gl_account}
    ch_account = {k: gl_account[v] for k, v in ch_bank.items() if gl_account.get(v)}
    return ch_bank, ch_account


def last_import_dates(ch_bank):
    """Latest statement date imported for each bank channel."""
    if not ch_bank:
        return {}
    last = dict(frappe.db.sql("""
        SELECT bank_account, MAX(date) FROM `tabBank Transaction`
        WHERE docstatus = 1 AND bank_account IN %(ba)s GROUP BY bank_account""",
        {"ba": tuple(ch_bank.values())}))
    return {ch: last.get(ba) for ch, ba in ch_bank.items()}


def cash_condition(field="description"):
    conds = " OR ".join(f"UPPER(IFNULL({field}, '')) LIKE %(cdp{i})s" for i in range(len(CASH_DEPOSIT_PATTERNS)))
    return f"({conds or '0'})", {f"cdp{i}": f"%{x.upper()}%" for i, x in enumerate(CASH_DEPOSIT_PATTERNS)}


def cash_accounts(f):
    return [r[0] for r in frappe.db.sql(
        "SELECT name FROM `tabAccount` WHERE account_type = 'Cash' AND is_group = 0 AND company = %s",
        f.company)]


def against_cash_condition(f):
    accs = cash_accounts(f)
    if not accs:
        return "0", {}
    return ("(" + " OR ".join(f"IFNULL(against, '') LIKE %(cacc{i})s" for i in range(len(accs))) + ")",
            {f"cacc{i}": f"%{a}%" for i, a in enumerate(accs)})


def _words(text, minlen=3):
    return {w for w in re.split(r"[^A-Za-z0-9]+", (text or "").upper()) if len(w) >= minlen}


def branch_names(f):
    """Name words of the branch's POS users, used to tell whose cash deposit a statement line is."""
    names = set()
    for (full_name,) in frappe.db.sql("""
        SELECT u.full_name FROM `tabPOS Profile User` pu JOIN `tabUser` u ON u.name = pu.user
        WHERE pu.parent = %s""", f.pos_profile):
        names |= _words(full_name)
    return names


def is_branch_deposit(description, names):
    """True when the line names one of the branch's cashiers. Banks often run names together
    ("GLORYGAKII KIREA"), so names of 4+ letters match anywhere in the text."""
    text = (description or "").upper()
    return any((n in text) if len(n) >= 4 else (n in _words(text)) for n in names)


def daily_money(f):
    """Per day and channel:
        Invoiced (= Sales Register by Mode of Payment)
        - paid later - paid earlier + old invoices paid today + advances + unallocated
        = Expected receipts today
        Variance = Statement - Other money in the account - Expected receipts
    """
    days = {}
    from_d, to_d = getdate(f.from_date), getdate(f.to_date)
    in_range = lambda d: d is not None and from_d <= getdate(d) <= to_d

    def cell(d, ch):
        day = days.setdefault(getdate(d), {})
        c = day.get(ch)
        if c is None:
            c = day[ch] = {k: 0.0 for k in MONEY_KEYS}
            c.update(gl_in=0.0, gl_xfer=0.0, lines=0.0, banked=0.0,
                     manual_stmt=None, manual_ns=0.0, shift_stmt=None)
        return c

    def head(d):
        return days.setdefault(getdate(d), {}).setdefault("_", {"gross": 0.0, "returns": 0.0})

    p = dict(f)
    shift_stats = {"total": 0, "uncounted": 0, "multi_day": 0}

    # 1. sales and returns (ties to Sales Register)
    for r in frappe.db.sql(f"""
        SELECT si.posting_date d,
            SUM(CASE WHEN si.is_return = 0 THEN IF(si.rounded_total, si.rounded_total, si.grand_total) ELSE 0 END) gross,
            SUM(CASE WHEN si.is_return = 1 THEN IF(si.rounded_total, si.rounded_total, si.grand_total) ELSE 0 END) returns
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.posting_date""", p, as_dict=True):
        h = head(r.d)
        h["gross"] += flt(r.gross)
        h["returns"] += flt(r.returns)

    # 2. payments allocated to this period's invoices, whenever they were paid
    for r in frappe.db.sql(f"""
        SELECT si.posting_date sd, pe.posting_date pd, pe.mode_of_payment mop,
            SUM(per.allocated_amount) amt
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.company = %(company)s
          AND per.reference_doctype = 'Sales Invoice' AND si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.posting_date, pe.posting_date, pe.mode_of_payment""", p, as_dict=True):
        ch, amt, sd, pd = channel_of(r.mop), flt(r.amt), getdate(r.sd), getdate(r.pd)
        cell(sd, ch)["invoiced"] += amt
        if pd > sd:
            cell(sd, ch)["paid_later"] -= amt
            if in_range(pd):
                cell(pd, ch)["old_paid"] += amt
        elif pd < sd:
            cell(sd, ch)["paid_earlier"] -= amt
            if in_range(pd):
                cell(pd, ch)["advance"] += amt

    # 3. money received in the period for invoices dated outside it
    for r in frappe.db.sql(f"""
        SELECT pe.posting_date pd, pe.mode_of_payment mop,
            SUM(CASE WHEN si.posting_date < %(from_date)s THEN per.allocated_amount ELSE 0 END) older,
            SUM(CASE WHEN si.posting_date > %(to_date)s THEN per.allocated_amount ELSE 0 END) future
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.company = %(company)s
          AND per.reference_doctype = 'Sales Invoice' AND si.docstatus = 1
          AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND (si.posting_date < %(from_date)s OR si.posting_date > %(to_date)s) AND {BRANCH_COND}
        GROUP BY pe.posting_date, pe.mode_of_payment""", p, as_dict=True):
        c = cell(r.pd, channel_of(r.mop))
        c["old_paid"] += flt(r.older)
        c["advance"] += flt(r.future)

    # 4. receipts by this branch's cashiers not yet allocated to any invoice
    for r in frappe.db.sql("""
        SELECT pe.posting_date pd, pe.mode_of_payment mop, SUM(pe.unallocated_amount) amt
        FROM `tabPayment Entry` pe
        WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.party_type = 'Customer'
          AND pe.company = %(company)s AND pe.unallocated_amount > 0 AND pe.owner IN %(users)s
          AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY pe.posting_date, pe.mode_of_payment""",
        dict(p, users=tuple(branch_users(f))), as_dict=True):
        cell(r.pd, channel_of(r.mop))["unallocated"] += flt(r.amt)

    # 5. payments inside the invoice itself (is_pos payments table), net of change
    for r in frappe.db.sql(f"""
        SELECT si.posting_date d, sip.mode_of_payment mop, SUM(sip.base_amount) amt
        FROM `tabSales Invoice Payment` sip
        JOIN `tabSales Invoice` si ON si.name = sip.parent
        WHERE si.docstatus = 1 AND si.is_pos = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.posting_date, sip.mode_of_payment""", p, as_dict=True):
        cell(r.d, channel_of(r.mop))["invoiced"] += flt(r.amt)
    for r in frappe.db.sql(f"""
        SELECT si.posting_date d, SUM(si.base_change_amount) chg
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.is_pos = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.posting_date HAVING chg <> 0""", p, as_dict=True):
        cell(r.d, "cash")["invoiced"] -= flt(r.chg)

    # 6. bank statements and everything booked into each bank account
    ch_bank, ch_account = bank_setup(f)
    last_import = last_import_dates(ch_bank)
    if ch_bank:
        ba_channel = {v: k for k, v in ch_bank.items()}
        cash_sql, cash_p = cash_condition()
        bp = dict(p, ba=tuple(ch_bank.values()), **cash_p)
        for r in frappe.db.sql(f"""
            SELECT date d, bank_account ba, COUNT(*) n,
                SUM(CASE WHEN {cash_sql} THEN 0 ELSE deposit END) dep
            FROM `tabBank Transaction`
            WHERE docstatus = 1 AND bank_account IN %(ba)s
              AND date BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY date, bank_account""", bp, as_dict=True):
            c = cell(r.d, ba_channel[r.ba])
            c["statement"] += flt(r.dep)
            c["lines"] += flt(r.n)
        names = branch_names(f)
        for r in frappe.db.sql(f"""
            SELECT date d, deposit, description FROM `tabBank Transaction`
            WHERE docstatus = 1 AND bank_account IN %(ba)s AND deposit > 0 AND {cash_sql}
              AND date BETWEEN %(from_date)s AND %(to_date)s""", bp, as_dict=True):
            if is_branch_deposit(r.description, names):
                cell(r.d, "cash")["banked"] += flt(r.deposit)
    if ch_account:
        acc_channel = {v: k for k, v in ch_account.items()}
        xfer_sql, xfer_p = against_cash_condition(f)
        for r in frappe.db.sql(f"""
            SELECT posting_date d, account acc, SUM(debit) dr,
                SUM(CASE WHEN {xfer_sql} THEN debit ELSE 0 END) xfer
            FROM `tabGL Entry`
            WHERE is_cancelled = 0 AND company = %(company)s AND account IN %(acc)s
              AND posting_date BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY posting_date, account""", dict(p, acc=tuple(acc_channel), **xfer_p), as_dict=True):
            c = cell(r.d, acc_channel[r.acc])
            c["gl_in"] += flt(r.dr)
            c["gl_xfer"] += flt(r.xfer)  # cash moved into the bank: counted under Cash, not here

    # 7. manual takings
    if frappe.db.table_exists("Branch Daily Takings"):
        for r in frappe.db.sql("""
            SELECT t.posting_date d, l.channel ch,
                SUM(l.statement_amount) stmt, SUM(l.non_sales_amount) ns,
                SUM(CASE WHEN l.statement_amount <> 0 THEN 1 ELSE 0 END) has_stmt
            FROM `tabBranch Takings Line` l
            JOIN `tabBranch Daily Takings` t ON t.name = l.parent
            WHERE t.docstatus = 1 AND t.pos_profile = %(pos_profile)s
              AND t.posting_date BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY t.posting_date, l.channel""", p, as_dict=True):
            c = cell(r.d, LABEL_CHANNEL.get(r.ch, "other"))
            if r.has_stmt:
                c["manual_stmt"] = flt(r.stmt)
            c["manual_ns"] += flt(r.ns)

    # 8. POSNext closing shifts
    for shift in get_closing_shifts(f):
        shift_stats["total"] += 1
        shift_stats["multi_day"] += 1 if shift.multi_day else 0
        if not shift.counted:
            shift_stats["uncounted"] += 1
            continue
        for mop, amt in shift.closing.items():
            c = cell(shift.posting_date, channel_of(mop))
            c["shift_stmt"] = (c["shift_stmt"] or 0.0) + amt

    # resolve. Statement priority: manual takings > bank lines (cash: cash banked) > closing shift.
    # A channel/day with no statement at all is not compared: its receipts go to "Not Checked".
    for d, day in days.items():
        h = day.get("_", {"gross": 0.0, "returns": 0.0})
        channel_invoiced = 0.0
        for ch, c in list(day.items()):
            if ch == "_":
                continue
            c["receipts"] = sum(c[k] for k in RECEIPT_KEYS)
            other, checked = 0.0, True
            if c["manual_stmt"] is not None:
                c["statement"], c["source"] = c["manual_stmt"], "Manual"
            elif ch in ch_bank and c["lines"]:
                c["source"] = "Bank Statement"
                other = c["gl_in"] - c["gl_xfer"] - c["receipts"]  # in the account, not this branch's receipts
            elif ch == "cash" and c["banked"]:
                c["statement"], c["source"] = c["banked"], "Cash Banked"
            elif c["shift_stmt"] is not None:
                c["statement"], c["source"] = c["shift_stmt"], "Closing Shift"
            else:
                checked = False
                if ch in ch_bank:
                    last = last_import.get(ch)
                    c["source"] = (_("No lines this day (imported to {0})").format(formatdate(last))
                                   if last else _("Statement never imported"))
                else:
                    c["source"] = _("No Statement") if c["receipts"] else ""
            if checked:
                c["other_in"] = -(other + c["manual_ns"])
                c["variance"] = c["statement"] + c["other_in"] - c["receipts"]
            else:
                c["statement"] = c["other_in"] = c["variance"] = 0.0
                c["unchecked"] = c["receipts"]
            channel_invoiced += c["invoiced"]
        unpaid = h["gross"] + h["returns"] - channel_invoiced
        if abs(unpaid) > 0.005:
            c = cell(d, "credit")
            c["invoiced"] = unpaid
            c["paid_later"] = -unpaid  # will be collected later, or never
            c["source"] = "Unpaid / Credit"

    only = bank_channel(f)
    if only:
        days = {d: {only: day[only]} for d, day in days.items() if only in day}

    return (get_money_columns(), build_money_rows(days, f), None, None,
            money_summary(days, shift_stats, last_import, ch_bank))


def build_money_rows(days, f):
    period = f.get("period") or "Day"
    agg, ranges = {}, {}
    for d, day in days.items():
        k = period_key(d, period)
        lo, hi = ranges.get(k, (d, d))
        ranges[k] = (min(lo, d), max(hi, d))
        pday = agg.setdefault(k, {})
        for ch, c in day.items():
            t = pday.setdefault(ch, {})
            for key, v in c.items():
                if isinstance(v, float):
                    t[key] = t.get(key, 0.0) + v
            if ch != "_" and c.get("source"):
                t.setdefault("sources", set()).add(c["source"])

    rows, order = [], [c[0] for c in CHANNELS] + ["credit"]
    for k in sorted(agg):
        day = agg[k]
        h = day.get("_", {})
        lo, hi = ranges[k]
        if period == "Week":
            lo, hi = k, add_days(k, 6)
        elif period == "Month":
            lo, hi = k, add_days(add_days(k, 32).replace(day=1), -1)
        lo, hi = max(lo, getdate(f.from_date)), min(hi, getdate(f.to_date))
        parent = {"name": str(k), "label": formatdate(k), "indent": 0,
                  "gross_sales": h.get("gross", 0), "returns": h.get("returns", 0),
                  "period_from": str(lo), "period_to": str(hi)}
        for key in MONEY_KEYS:
            parent[key] = 0.0
        children = []
        for ch in order:
            c = day.get(ch)
            if not c or not any(abs(c.get(x, 0)) > 0.005 for x in MONEY_KEYS):
                continue
            row = {"name": f"{k}-{ch}", "parent_name": str(k), "indent": 1,
                   "label": CHANNEL_LABEL.get(ch, "Credit / Unpaid"), "channel": CHANNEL_LABEL.get(ch, "Credit / Unpaid"),
                   "period_from": str(lo), "period_to": str(hi),
                   "source": ", ".join(sorted(c.get("sources") or []))}
            for key in MONEY_KEYS:
                row[key] = c.get(key, 0.0)
                parent[key] += row[key]
            children.append(row)
        rows.append(parent)
        rows.extend(children)

    if rows:
        total = {"label": _("Total"), "indent": 0, "bold": 1}
        for key in MONEY_KEYS + ("gross_sales", "returns"):
            total[key] = sum(r.get(key, 0) for r in rows if r["indent"] == 0)
        rows.append(total)
    return rows


def money_summary(days, shift_stats, last_import=None, ch_bank=None):
    t = dict.fromkeys(("inv", "rec", "stmt", "var", "unchecked"), 0.0)
    for day in days.values():
        for ch, c in day.items():
            if ch == "_":
                continue
            t["inv"] += c["invoiced"]
            t["rec"] += c["receipts"]
            t["stmt"] += c["statement"]
            t["var"] += c["variance"]
            t["unchecked"] += c.get("unchecked", 0.0)
    out = [
        {"label": _("Invoiced Sales (= Sales Register)"), "value": t["inv"], "datatype": "Currency"},
        {"label": _("Expected Receipts"), "value": t["rec"], "datatype": "Currency"},
        {"label": _("Statement Received"), "value": t["stmt"], "datatype": "Currency"},
        {"label": _("Net Variance (checked days)"), "value": t["var"], "datatype": "Currency",
         "indicator": "Red" if t["var"] < -0.5 else "Green"},
        {"label": _("Not Checked (no statement)"), "value": t["unchecked"], "datatype": "Currency",
         "indicator": "Orange" if t["unchecked"] else "Green"},
        {"label": _("Shifts Closed Without Count"),
         "value": f"{shift_stats['uncounted']} / {shift_stats['total']}", "datatype": "Data",
         "indicator": "Orange" if shift_stats["uncounted"] else "Green"},
    ]
    for ch in (ch_bank or {}):
        last = (last_import or {}).get(ch)
        out.append({"label": _("{0} statement imported to").format(CHANNEL_LABEL[ch]),
                    "value": formatdate(last) if last else _("Never"), "datatype": "Data",
                    "indicator": "Green" if last else "Red"})
    return out


def get_money_columns():
    cur = lambda fn, lb, w=125: {"fieldname": fn, "label": _(lb), "fieldtype": "Currency", "width": w}
    return [
        {"fieldname": "label", "label": _("Date / Channel"), "fieldtype": "Data", "width": 170},
        cur("invoiced", "Invoiced Sales"),
        cur("paid_later", "Paid Later / Unpaid (−)", 150),
        cur("paid_earlier", "Paid in Advance (−)", 140),
        cur("old_paid", "Old Invoices Paid (+)", 145),
        cur("advance", "Advances Received (+)", 145),
        cur("unallocated", "Unallocated (+)"),
        cur("receipts", "Expected Receipts", 135),
        cur("statement", "Statement"),
        cur("other_in", "Other Money In (−)", 135),
        cur("variance", "Variance"),
        cur("unchecked", "Not Checked (no statement)", 170),
        {"fieldname": "source", "label": _("Statement Source"), "fieldtype": "Data", "width": 220},
        cur("gross_sales", "Gross Sales"),
        cur("returns", "Returns"),
    ]


# --------------------------------------------------------------------------- channel drill-down
def _tokens(*texts):
    out = set()
    for t in texts:
        for tok in re.split(r"[^A-Za-z0-9]+", (t or "").upper()):
            if len(tok) >= 6:
                out.add(tok)
    return out


def channel_detail(f):
    """GL-style drill-down for one channel: every invoice, payment, statement line and
    other entry that makes up that channel's line in Daily Money."""
    label = f.get("channel")
    if not label and bank_channel(f):
        label = CHANNEL_LABEL[bank_channel(f)]
    if not label:
        frappe.throw(_("Pick a Channel or a Bank Account, or click a channel row in Daily Money"))
    if label == "Credit / Unpaid":
        return credit_detail(f)
    ch = LABEL_CHANNEL[label]
    cond, cp = mop_condition(ch)
    p = dict(f, **cp)
    ch_bank, ch_account = bank_setup(f)
    bank_account, account = ch_bank.get(ch), ch_account.get(ch)
    cash_sql, cash_p = cash_condition()
    names = branch_names(f)

    # statement candidates per day; the one Daily Money uses is picked below
    cands = {}  # date -> {"manual": [...], "bank": [...], "banked": [...], "shift": [...]}

    def add(kind, row):
        cands.setdefault(getdate(row["posting_date"]), {}).setdefault(kind, []).append(row)

    bt_tokens, bt_matched_pe, line_days = set(), {}, set()
    if bank_account:
        line_days = {getdate(r[0]) for r in frappe.db.sql("""
            SELECT DISTINCT date FROM `tabBank Transaction`
            WHERE docstatus = 1 AND bank_account = %(ba)s AND date BETWEEN %(from_date)s AND %(to_date)s""",
            dict(p, ba=bank_account))}
        bts = frappe.db.sql(f"""
            SELECT name, date, deposit, reference_number, description, {cash_sql} is_cash
            FROM `tabBank Transaction`
            WHERE docstatus = 1 AND bank_account = %(ba)s AND deposit > 0
              AND date BETWEEN %(from_date)s AND %(to_date)s
            ORDER BY date, name""", dict(p, ba=bank_account, **cash_p), as_dict=True)
        links = {}
        if bts:
            for r in frappe.db.sql("""
                SELECT parent, payment_document, payment_entry FROM `tabBank Transaction Payments`
                WHERE parent IN %(n)s""", {"n": tuple(b.name for b in bts)}, as_dict=True):
                links.setdefault(r.parent, []).append(r)
                if r.payment_document == "Payment Entry":
                    bt_matched_pe[r.payment_entry] = r.parent
        for b in bts:
            bt_tokens |= _tokens(b.reference_number, b.description)
            linked = links.get(b.name, [])
            row = {"posting_date": b.date, "voucher_type": "Bank Transaction", "voucher_no": b.name,
                   "reference_no": b.reference_number, "party": (b.description or "")[:80],
                   "amount": flt(b.deposit),
                   "status": ("Matched: " + ", ".join(l.payment_entry for l in linked)) if linked else "Not matched"}
            if b.is_cash:
                whose = "counted under Cash" if is_branch_deposit(b.description, names) else "not this branch's staff"
                row.update(amount=0.0, status=f"Cash deposit {flt(b.deposit):,.2f} → {whose}")
            add("bank", row)

    if ch == "cash" and ch_bank:
        for b in frappe.db.sql(f"""
            SELECT name, date, deposit, bank_account, description FROM `tabBank Transaction`
            WHERE docstatus = 1 AND bank_account IN %(ba)s AND deposit > 0 AND {cash_sql}
              AND date BETWEEN %(from_date)s AND %(to_date)s ORDER BY date, name""",
            dict(p, ba=tuple(ch_bank.values()), **cash_p), as_dict=True):
            if is_branch_deposit(b.description, names):
                add("banked", {"posting_date": b.date, "voucher_type": "Bank Transaction", "voucher_no": b.name,
                               "party": (b.description or "")[:80], "amount": flt(b.deposit),
                               "status": f"Cash banked into {b.bank_account}"})

    for s_ in get_closing_shifts(f):
        amt = sum(v for m, v in s_.closing.items() if channel_of(m) == ch)
        exp = sum(v for m, v in s_.expected.items() if channel_of(m) == ch)
        if (amt or exp) and s_.counted:
            add("shift", {"posting_date": s_.posting_date, "voucher_type": "POS Closing Shift",
                          "voucher_no": s_.name, "party": s_.user, "amount": amt,
                          "status": f"Counted (expected {exp:,.2f})"})

    if frappe.db.table_exists("Branch Daily Takings"):
        for r in frappe.db.sql("""
            SELECT t.name, t.posting_date, l.statement_amount, l.non_sales_amount, l.remarks
            FROM `tabBranch Takings Line` l JOIN `tabBranch Daily Takings` t ON t.name = l.parent
            WHERE t.docstatus = 1 AND t.pos_profile = %(pos_profile)s AND l.channel = %(label)s
              AND t.posting_date BETWEEN %(from_date)s AND %(to_date)s AND l.statement_amount <> 0""",
            dict(p, label=label), as_dict=True):
            add("manual", {"posting_date": r.posting_date, "voucher_type": "Branch Daily Takings",
                           "voucher_no": r.name, "amount": flt(r.statement_amount), "party": r.remarks,
                           "status": "Manual"})

    # same priority as Daily Money: manual > bank lines (cash: cash banked) > closing shift
    stmt_rows, checked_days, used_kind = [], set(), {}
    for d in sorted(cands):
        c = cands[d]
        if "manual" in c:
            use = "manual"
        elif bank_account and d in line_days:
            use = "bank"
        elif ch == "cash" and "banked" in c:
            use = "banked"
        elif "shift" in c:
            use = "shift"
        else:
            use = None
        if use:
            checked_days.add(d)
            used_kind[d] = use
        for kind, items in c.items():
            for r in items:
                if kind != use:
                    r = dict(r, amount=0.0, status=r["status"] + " (not used: another source wins this day)")
                stmt_rows.append(r)
    # bank days with only withdrawals still count as checked
    if bank_account:
        checked_days |= {d for d in line_days if d not in used_kind or used_kind[d] == "bank"}

    def seen_in_bank(pe, ref):
        if not bank_account:
            return ""
        if pe in bt_matched_pe:
            return "In bank: " + bt_matched_pe[pe]
        if ref and ref.upper() in bt_tokens:
            return "Ref on statement (not matched)"
        return "Not on statement"

    # invoices of the period and how each was paid
    inv_rows, rec_rows = [], []
    from_d, to_d = getdate(f.from_date), getdate(f.to_date)
    for r in frappe.db.sql(f"""
        SELECT si.name invoice, si.posting_date sd, si.owner cashier, si.customer,
            pe.name pe, pe.posting_date pd, pe.reference_no, per.allocated_amount amt
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.company = %(company)s
          AND per.reference_doctype = 'Sales Invoice' AND si.docstatus = 1 AND {cond}
          AND (si.posting_date BETWEEN %(from_date)s AND %(to_date)s
               OR pe.posting_date BETWEEN %(from_date)s AND %(to_date)s) AND {BRANCH_COND}
        ORDER BY si.posting_date, si.name""", p, as_dict=True):
        sd, pd = getdate(r.sd), getdate(r.pd)
        base = {"voucher_type": "Payment Entry", "voucher_no": r.pe, "invoice": r.invoice,
                "invoice_date": sd, "payment_date": pd, "reference_no": r.reference_no,
                "party": r.cashier, "amount": flt(r.amt)}
        if from_d <= sd <= to_d:
            status = "Same day" if pd == sd else ("Paid later" if pd > sd else "Paid in advance")
            inv_rows.append(dict(base, posting_date=sd, status=status))
        if from_d <= pd <= to_d:
            kind = ("Same day" if pd == sd else "Old invoice" if sd < pd else "Advance")
            rec_rows.append(dict(base, posting_date=pd, status=kind + " · " + seen_in_bank(r.pe, r.reference_no)
                                 if bank_account else kind))

    for r in frappe.db.sql(f"""
        SELECT pe.name, pe.posting_date, pe.reference_no, pe.party, pe.owner, pe.unallocated_amount amt
        FROM `tabPayment Entry` pe
        WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.party_type = 'Customer'
          AND pe.company = %(company)s AND pe.unallocated_amount > 0 AND pe.owner IN %(users)s AND {cond}
          AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s""",
        dict(p, users=tuple(branch_users(f))), as_dict=True):
        rec_rows.append({"posting_date": r.posting_date, "voucher_type": "Payment Entry", "voucher_no": r.name,
                         "reference_no": r.reference_no, "party": f"{r.party} ({r.owner})", "amount": flt(r.amt),
                         "status": "Unallocated" + (" · " + seen_in_bank(r.name, r.reference_no) if bank_account else "")})

    # other money booked into the account
    other_rows = []
    branch_pes = {r["voucher_no"] for r in rec_rows}
    cash_accs = cash_accounts(f)
    if account:
        for r in frappe.db.sql("""
            SELECT posting_date, voucher_type, voucher_no, party, against, remarks, SUM(debit) amt
            FROM `tabGL Entry`
            WHERE is_cancelled = 0 AND company = %(company)s AND account = %(acc)s AND debit > 0
              AND posting_date BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY posting_date, voucher_type, voucher_no
            ORDER BY posting_date, voucher_no""", dict(p, acc=account), as_dict=True):
            amt = flt(r.amt)
            if any(a in (r.against or "") for a in cash_accs):
                continue  # cash moved into the bank: counted under Cash
            if r.voucher_type == "Payment Entry" and r.voucher_no in branch_pes:
                # part of the PE may belong to another branch's invoices
                amt -= sum(x["amount"] for x in rec_rows if x["voucher_no"] == r.voucher_no)
                if abs(amt) < 0.005:
                    continue
                status = "Same receipt, other branch's share"
            else:
                status = "Other branch / debt / transfer / journal"
            other_rows.append({"posting_date": r.posting_date, "voucher_type": r.voucher_type,
                               "voucher_no": r.voucher_no, "party": r.party or (r.against or "")[:60],
                               "amount": -amt, "status": status + (f" · {r.remarks[:60]}" if r.remarks else "")})
        other_rows = [r for r in other_rows if abs(r["amount"]) > 0.005]

    # days without any statement are not compared (same as Daily Money)
    def on_checked(r):
        return getdate(r["posting_date"]) in checked_days

    for r in rec_rows:
        if not on_checked(r):
            r["status"] = (r.get("status") or "") + " · day not checked (no statement)"
    for r in other_rows:
        d = getdate(r["posting_date"])
        if not (d in line_days and used_kind.get(d, "bank") == "bank"):
            r["status"] += " · not used (no bank lines this day)"
            r["amount"] = 0.0

    invoiced = sum(r["amount"] for r in inv_rows)
    receipts = sum(r["amount"] for r in rec_rows)
    unchecked = sum(r["amount"] for r in rec_rows if not on_checked(r))
    statement = sum(r["amount"] for r in stmt_rows)
    other = sum(r["amount"] for r in other_rows)
    variance = statement + other - (receipts - unchecked)

    rows = []

    def section(key, title, items, note=""):
        total = sum(r["amount"] for r in items)
        rows.append({"name": key, "label": f"{title} ({len(items)})", "indent": 0, "amount": total,
                     "status": note, "bold": 1})
        for i, r in enumerate(items):
            rows.append(dict(r, name=f"{key}-{i}", parent_name=key, indent=1,
                             label=r.get("invoice") or r.get("voucher_no")))

    section("inv", _("Invoiced Sales: invoices of the period, by payment"), inv_rows,
            _("Ties to Sales Register by Mode of Payment"))
    section("rec", _("Receipts in the period"), rec_rows, _("Money this branch received"))
    section("stmt", _("Statement") if bank_account else _("Counted / Declared"), stmt_rows,
            label + (" bank statement" if bank_account else ""))
    if account:
        section("oth", _("Other money into the account"), other_rows, account)
    if abs(unchecked) > 0.005:
        rows.append({"label": _("Not checked: receipts on days with no statement"), "indent": 0, "bold": 1,
                     "amount": unchecked, "status": _("Left out of the variance")})
    rows.append({"label": _("Variance = Statement + Other − Checked receipts"), "indent": 0, "bold": 1,
                 "amount": variance, "status": ""})

    unmatched_bt = sum(1 for r in stmt_rows if r.get("status") == "Not matched")
    not_on_stmt = sum(1 for r in rec_rows if "Not on statement" in (r.get("status") or ""))
    summary = [
        {"label": _("Invoiced ({0})").format(label), "value": invoiced, "datatype": "Currency"},
        {"label": _("Receipts"), "value": receipts, "datatype": "Currency"},
        {"label": _("Statement"), "value": statement, "datatype": "Currency"},
        {"label": _("Other Money In"), "value": other, "datatype": "Currency"},
        {"label": _("Not Checked"), "value": unchecked, "datatype": "Currency",
         "indicator": "Orange" if unchecked else "Green"},
        {"label": _("Variance"), "value": variance, "datatype": "Currency",
         "indicator": "Red" if variance < -0.5 else "Green"},
    ]
    if bank_account:
        summary += [
            {"label": _("Statement Lines Not Matched"), "value": unmatched_bt, "datatype": "Int",
             "indicator": "Orange" if unmatched_bt else "Green"},
            {"label": _("Receipts Not on Statement"), "value": not_on_stmt, "datatype": "Int",
             "indicator": "Red" if not_on_stmt else "Green"},
        ]
    return detail_columns(), rows, None, None, summary


def credit_detail(f):
    rows = frappe.db.sql(f"""
        SELECT si.name invoice, si.posting_date, si.customer, si.owner,
            IF(si.rounded_total, si.rounded_total, si.grand_total) total,
            IFNULL((SELECT SUM(per.allocated_amount) FROM `tabPayment Entry Reference` per
                    JOIN `tabPayment Entry` pe ON pe.name = per.parent
                    WHERE per.reference_doctype = 'Sales Invoice' AND per.reference_name = si.name
                      AND pe.docstatus = 1), 0) paid,
            IFNULL((SELECT SUM(sip.base_amount) FROM `tabSales Invoice Payment` sip
                    WHERE sip.parent = si.name), 0) - IFNULL(si.base_change_amount, 0) pos_paid,
            si.outstanding_amount, si.is_return, si.return_against
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}""", dict(f), as_dict=True)
    out = []
    for r in rows:
        unpaid = flt(r.total) - flt(r.paid) - flt(r.pos_paid)
        if abs(unpaid) < 0.005:
            continue
        out.append({"posting_date": r.posting_date, "voucher_type": "Sales Invoice", "voucher_no": r.invoice,
                    "invoice": r.invoice, "label": r.invoice, "invoice_date": r.posting_date,
                    "party": f"{r.customer} ({r.owner})", "amount": unpaid, "indent": 0,
                    "status": ("Return against " + r.return_against if r.is_return and r.return_against
                               else "Return" if r.is_return else f"Outstanding now {flt(r.outstanding_amount):,.2f}")})
    total = sum(r["amount"] for r in out)
    summary = [{"label": _("Unpaid at Invoice Date"), "value": total, "datatype": "Currency"},
               {"label": _("Invoices"), "value": len(out), "datatype": "Int"}]
    return detail_columns(), out, None, None, summary


def detail_columns():
    return [
        {"fieldname": "label", "label": _("Section / Document"), "fieldtype": "Data", "width": 260},
        {"fieldname": "posting_date", "label": _("Date"), "fieldtype": "Date", "width": 95},
        {"fieldname": "voucher_type", "label": _("Voucher Type"), "fieldtype": "Data", "width": 130},
        {"fieldname": "voucher_no", "label": _("Voucher"), "fieldtype": "Dynamic Link", "options": "voucher_type", "width": 170},
        {"fieldname": "invoice", "label": _("Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 165},
        {"fieldname": "invoice_date", "label": _("Invoice Date"), "fieldtype": "Date", "width": 95},
        {"fieldname": "payment_date", "label": _("Paid On"), "fieldtype": "Date", "width": 95},
        {"fieldname": "reference_no", "label": _("Reference"), "fieldtype": "Data", "width": 120},
        {"fieldname": "party", "label": _("Party / Cashier / Narration"), "fieldtype": "Data", "width": 200},
        {"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 280},
    ]


def get_closing_shifts(f):
    """Submitted POSNext closing shifts for the branch, one dict per shift."""
    if not frappe.db.table_exists("POS Closing Shift"):
        return []
    shifts = {}
    for r in frappe.db.sql("""
        SELECT cs.name, cs.posting_date, cs.user, cs.period_start_date, cs.period_end_date,
            csd.mode_of_payment mop, csd.expected_amount, csd.closing_amount
        FROM `tabPOS Closing Shift` cs
        JOIN `tabPOS Closing Shift Detail` csd ON csd.parent = cs.name
        WHERE cs.docstatus = 1 AND cs.pos_profile = %(pos_profile)s
          AND cs.posting_date BETWEEN %(from_date)s AND %(to_date)s""", dict(f), as_dict=True):
        s = shifts.setdefault(r.name, frappe._dict(
            name=r.name, posting_date=r.posting_date, user=r.user, expected={}, closing={},
            multi_day=date_diff(r.period_end_date, r.period_start_date) >= 1
            if r.period_start_date and r.period_end_date else False))
        s.expected[r.mop] = s.expected.get(r.mop, 0) + flt(r.expected_amount)
        s.closing[r.mop] = s.closing.get(r.mop, 0) + flt(r.closing_amount)
    for s in shifts.values():
        # a shift where every counted figure is zero was closed without counting
        s.counted = any(abs(v) > 0.005 for v in s.closing.values())
    return list(shifts.values())


# --------------------------------------------------------------------------- stock
def stock_variance(f):
    rows = frappe.db.sql("""
        SELECT sr.posting_date, sr.name voucher, sri.item_code, sri.item_name,
            sri.current_qty system_qty, sri.qty counted_qty, (sri.qty - sri.current_qty) diff_qty,
            sri.amount_difference value_at_cost
        FROM `tabStock Reconciliation Item` sri
        JOIN `tabStock Reconciliation` sr ON sr.name = sri.parent
        WHERE sr.docstatus = 1 AND sr.purpose = 'Stock Reconciliation' AND sr.company = %(company)s
          AND sri.warehouse = %(warehouse)s
          AND sr.posting_date BETWEEN %(from_date)s AND %(to_date)s
          AND ABS(sri.qty - sri.current_qty) > 0.0001
        ORDER BY (sri.qty - sri.current_qty) ASC""", dict(f), as_dict=True)

    prices = get_prices(f, {r.item_code for r in rows})

    short = excess = 0.0
    for r in rows:
        r.selling_price = prices.get(r.item_code, 0)
        r.value_at_sp = flt(r.diff_qty) * r.selling_price
        r.no_price = 1 if not r.selling_price else 0
        if r.value_at_sp < 0:
            short += r.value_at_sp
        else:
            excess += r.value_at_sp

    columns = [
        {"fieldname": "posting_date", "label": _("Count Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "voucher", "label": _("Stock Reconciliation"), "fieldtype": "Link", "options": "Stock Reconciliation", "width": 170},
        {"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
        {"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 200},
        {"fieldname": "system_qty", "label": _("System Qty"), "fieldtype": "Float", "width": 100},
        {"fieldname": "counted_qty", "label": _("Counted Qty"), "fieldtype": "Float", "width": 100},
        {"fieldname": "diff_qty", "label": _("Variance Qty"), "fieldtype": "Float", "width": 100},
        {"fieldname": "selling_price", "label": _("SP ({0})").format(f.price_list), "fieldtype": "Currency", "width": 110},
        {"fieldname": "value_at_sp", "label": _("Value at SP"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "value_at_cost", "label": _("Value at Cost"), "fieldtype": "Currency", "width": 130},
    ]
    summary = [
        {"label": _("Shortage at Selling Price"), "value": short, "datatype": "Currency", "indicator": "Red"},
        {"label": _("Excess at Selling Price"), "value": excess, "datatype": "Currency", "indicator": "Blue"},
        {"label": _("Net Stock Variance"), "value": short + excess, "datatype": "Currency",
         "indicator": "Red" if short + excess < 0 else "Green"},
        {"label": _("Items Without a Price"), "value": sum(r.no_price for r in rows), "datatype": "Int",
         "indicator": "Orange"},
    ]
    return columns, rows, None, None, summary


def get_prices(f, items):
    """Selling price per item on the branch price list (latest valid price wins)."""
    prices = {}
    if items:
        for p in frappe.db.sql("""
            SELECT item_code, price_list_rate FROM `tabItem Price`
            WHERE price_list = %(pl)s AND selling = 1 AND item_code IN %(items)s
              AND IFNULL(valid_from, '2000-01-01') <= %(to_date)s
            ORDER BY IFNULL(valid_from, '2000-01-01') ASC""",
            {"pl": f.price_list, "items": tuple(items), "to_date": f.to_date}, as_dict=True):
            prices[p.item_code] = flt(p.price_list_rate)
    return prices


SALES_VOUCHERS = ("Sales Invoice", "POS Invoice", "Delivery Note")
PURCHASE_VOUCHERS = ("Purchase Receipt", "Purchase Invoice")


def stock_movement(f):
    """Opening + movements = closing for every item that moved, plus count coverage."""
    p = dict(f)

    def balances(cond):
        return {r.item_code: flt(r.qty) for r in frappe.db.sql(f"""
            SELECT item_code, qty_after_transaction qty FROM (
                SELECT item_code, qty_after_transaction,
                    ROW_NUMBER() OVER (PARTITION BY item_code
                        ORDER BY posting_date DESC, posting_time DESC, creation DESC) rn
                FROM `tabStock Ledger Entry`
                WHERE is_cancelled = 0 AND warehouse = %(warehouse)s AND {cond}) x
            WHERE rn = 1""", p, as_dict=True)}

    opening = balances("posting_date < %(from_date)s")
    closing = balances("posting_date <= %(to_date)s")

    items = {}

    def item(code):
        return items.setdefault(code, frappe._dict(
            item_code=code, opening=opening.get(code, 0), received=0.0, transfer_in=0.0,
            sold=0.0, returned=0.0, transfer_out=0.0, other=0.0, min_balance=None))

    for r in frappe.db.sql("""
        SELECT item_code, voucher_type,
            SUM(CASE WHEN actual_qty > 0 THEN actual_qty ELSE 0 END) inq,
            SUM(CASE WHEN actual_qty < 0 THEN actual_qty ELSE 0 END) outq,
            MIN(qty_after_transaction) min_bal
        FROM `tabStock Ledger Entry`
        WHERE is_cancelled = 0 AND warehouse = %(warehouse)s
          AND posting_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY item_code, voucher_type""", p, as_dict=True):
        it = item(r.item_code)
        it.min_balance = flt(r.min_bal) if it.min_balance is None else min(it.min_balance, flt(r.min_bal))
        vt = r.voucher_type
        if vt == "Stock Reconciliation":
            continue  # derived below from opening/closing so it works on every ERPNext version
        if vt in SALES_VOUCHERS:
            it.sold += flt(r.outq)
            it.returned += flt(r.inq)
        elif vt in PURCHASE_VOUCHERS:
            it.received += flt(r.inq)
            it.other += flt(r.outq)
        elif vt == "Stock Entry":
            it.transfer_in += flt(r.inq)
            it.transfer_out += flt(r.outq)
        else:
            it.other += flt(r.inq) + flt(r.outq)

    counted = {r.item_code: r for r in frappe.db.sql("""
        SELECT sri.item_code, MAX(sr.posting_date) last_count,
            SUM(CASE WHEN sr.posting_date >= %(from_date)s THEN 1 ELSE 0 END) counts_in_period
        FROM `tabStock Reconciliation Item` sri
        JOIN `tabStock Reconciliation` sr ON sr.name = sri.parent
        WHERE sr.docstatus = 1 AND sr.purpose = 'Stock Reconciliation'
          AND sri.warehouse = %(warehouse)s AND sr.posting_date <= %(to_date)s
        GROUP BY sri.item_code""", p, as_dict=True)}

    names = dict(frappe.db.sql(
        "SELECT name, item_name FROM `tabItem` WHERE name IN %(i)s", {"i": tuple(items) or ("",)}))
    prices = get_prices(f, set(items))

    rows = []
    stats = {"moved": 0, "not_counted": 0, "negative": 0, "adj_value": 0.0, "not_counted_sales": 0.0}
    for code, it in items.items():
        it.item_name = names.get(code, code)
        it.closing = closing.get(code, 0)
        movements = it.received + it.transfer_in + it.sold + it.returned + it.transfer_out + it.other
        it.recon_adjustment = it.closing - it.opening - movements
        it.selling_price = prices.get(code, 0)
        it.adjustment_value = it.recon_adjustment * it.selling_price
        c = counted.get(code)
        it.last_count = c.last_count if c else None
        it.counted_in_period = 1 if c and c.counts_in_period else 0

        flags = []
        if not it.counted_in_period and abs(movements) > 0:
            flags.append("Not counted this period")
            stats["not_counted"] += 1
            stats["not_counted_sales"] += -it.sold * it.selling_price
        if it.min_balance is not None and it.min_balance < 0:
            flags.append(f"Went negative ({it.min_balance:g})")
            stats["negative"] += 1
        if abs(it.recon_adjustment) > 0.0001:
            flags.append("Count adjusted stock")
        it.flags = ", ".join(flags)

        stats["moved"] += 1
        stats["adj_value"] += it.adjustment_value
        rows.append(it)

    rows.sort(key=lambda r: (r.adjustment_value, -abs(r.sold)))

    flt_col = lambda fn, lb, w=95: {"fieldname": fn, "label": _(lb), "fieldtype": "Float", "width": w}
    columns = [
        {"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 140},
        {"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 190},
        flt_col("opening", "Opening"),
        flt_col("received", "Purchased"),
        flt_col("transfer_in", "Transfer In"),
        flt_col("sold", "Sold"),
        flt_col("returned", "Sales Returns"),
        flt_col("transfer_out", "Transfer Out"),
        flt_col("other", "Other"),
        flt_col("recon_adjustment", "Count Adjustment", 110),
        flt_col("closing", "Closing"),
        flt_col("min_balance", "Lowest Balance", 105),
        {"fieldname": "selling_price", "label": _("SP"), "fieldtype": "Currency", "width": 95},
        {"fieldname": "adjustment_value", "label": _("Adjustment at SP"), "fieldtype": "Currency", "width": 130},
        {"fieldname": "last_count", "label": _("Last Counted"), "fieldtype": "Date", "width": 100},
        {"fieldname": "flags", "label": _("Flags"), "fieldtype": "Data", "width": 260},
    ]
    summary = [
        {"label": _("Items Moved"), "value": stats["moved"], "datatype": "Int"},
        {"label": _("Moved but Not Counted"), "value": stats["not_counted"], "datatype": "Int",
         "indicator": "Orange" if stats["not_counted"] else "Green"},
        {"label": _("Sales of Uncounted Items"), "value": stats["not_counted_sales"], "datatype": "Currency",
         "indicator": "Orange" if stats["not_counted_sales"] else "Green"},
        {"label": _("Items That Went Negative"), "value": stats["negative"], "datatype": "Int",
         "indicator": "Red" if stats["negative"] else "Green"},
        {"label": _("Count Adjustments at SP"), "value": stats["adj_value"], "datatype": "Currency",
         "indicator": "Red" if stats["adj_value"] < 0 else "Green"},
    ]
    return columns, rows, None, None, summary


# --------------------------------------------------------------------------- cashiers
def cashier_summary(f):
    p = dict(f)
    users = {}

    def u(user):
        return users.setdefault(user, frappe._dict(
            cashier=user, invoices=0, gross_sales=0.0, returns=0.0, return_count=0, flagged_returns=0,
            cash_collected=0.0, other_collected=0.0, total_collected=0.0,
            shifts=0, uncounted_shifts=0, expected_at_close=0.0, counted_at_close=0.0))

    for r in frappe.db.sql(f"""
        SELECT si.owner,
            SUM(CASE WHEN si.is_return = 0 THEN 1 ELSE 0 END) invoices,
            SUM(CASE WHEN si.is_return = 0 THEN IF(si.rounded_total, si.rounded_total, si.grand_total) ELSE 0 END) gross,
            SUM(CASE WHEN si.is_return = 1 THEN IF(si.rounded_total, si.rounded_total, si.grand_total) ELSE 0 END) returns,
            SUM(CASE WHEN si.is_return = 1 THEN 1 ELSE 0 END) return_count
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.owner""", p, as_dict=True):
        x = u(r.owner)
        x.invoices, x.gross_sales, x.returns, x.return_count = r.invoices, flt(r.gross), flt(r.returns), r.return_count

    collections = frappe.db.sql(f"""
        SELECT pe.owner usr, pe.mode_of_payment mop, SUM(per.allocated_amount) amt
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.company = %(company)s
          AND per.reference_doctype = 'Sales Invoice'
          AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY pe.owner, pe.mode_of_payment""", p, as_dict=True)
    collections += frappe.db.sql(f"""
        SELECT si.owner usr, sip.mode_of_payment mop, SUM(sip.base_amount) amt
        FROM `tabSales Invoice Payment` sip
        JOIN `tabSales Invoice` si ON si.name = sip.parent
        WHERE si.docstatus = 1 AND si.is_pos = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.owner, sip.mode_of_payment""", p, as_dict=True)
    for r in collections:
        x = u(r.usr)
        if channel_of(r.mop) == "cash":
            x.cash_collected += flt(r.amt)
        else:
            x.other_collected += flt(r.amt)
        x.total_collected += flt(r.amt)

    for s in get_closing_shifts(f):
        x = u(s.user)
        x.shifts += 1
        x.expected_at_close += sum(s.expected.values())
        if s.counted:
            x.counted_at_close += sum(s.closing.values())
        else:
            x.uncounted_shifts += 1

    for r in get_returns(f):
        if r.flags:
            u(r.owner).flagged_returns += 1

    rows = []
    for x in users.values():
        if f.get("cashier") and x.cashier != f.cashier:
            continue
        x.net_sales = x.gross_sales + x.returns
        x.close_difference = x.counted_at_close - x.expected_at_close if x.shifts > x.uncounted_shifts else None
        x.return_rate = (abs(x.returns) / x.gross_sales * 100) if x.gross_sales else 0
        rows.append(x)
    rows.sort(key=lambda r: -r.gross_sales)

    cur = lambda fn, lb, w=120: {"fieldname": fn, "label": _(lb), "fieldtype": "Currency", "width": w}
    columns = [
        {"fieldname": "cashier", "label": _("Cashier"), "fieldtype": "Link", "options": "User", "width": 190},
        {"fieldname": "invoices", "label": _("Invoices"), "fieldtype": "Int", "width": 80},
        cur("gross_sales", "Gross Sales"),
        cur("returns", "Returns"),
        {"fieldname": "return_count", "label": _("Credit Notes"), "fieldtype": "Int", "width": 90},
        {"fieldname": "return_rate", "label": _("Return %"), "fieldtype": "Percent", "width": 85},
        {"fieldname": "flagged_returns", "label": _("Flagged Returns"), "fieldtype": "Int", "width": 105},
        cur("net_sales", "Net Sales"),
        cur("cash_collected", "Cash Collected"),
        cur("other_collected", "Mobile / Bank Collected", 150),
        cur("total_collected", "Total Collected"),
        {"fieldname": "shifts", "label": _("Shifts Closed"), "fieldtype": "Int", "width": 95},
        {"fieldname": "uncounted_shifts", "label": _("Closed Without Count"), "fieldtype": "Int", "width": 130},
        cur("expected_at_close", "Expected at Close", 130),
        cur("counted_at_close", "Counted at Close", 130),
        cur("close_difference", "Close Difference", 125),
    ]
    summary = [
        {"label": _("Cashiers"), "value": len(rows), "datatype": "Int"},
        {"label": _("Shifts Closed Without Count"), "value": sum(r.uncounted_shifts for r in rows),
         "datatype": "Int", "indicator": "Orange" if any(r.uncounted_shifts for r in rows) else "Green"},
        {"label": _("Close Difference"), "value": sum(flt(r.close_difference) for r in rows),
         "datatype": "Currency"},
        {"label": _("Flagged Returns"), "value": sum(r.flagged_returns for r in rows), "datatype": "Int",
         "indicator": "Red" if any(r.flagged_returns for r in rows) else "Green"},
    ]
    return columns, rows, None, None, summary


# --------------------------------------------------------------------------- returns
def get_returns(f):
    late_days = int(f.get("late_return_days") or 2)
    rows = frappe.db.sql(f"""
        SELECT si.name, si.posting_date, si.owner, si.customer, si.return_against,
            si.update_stock, IF(si.rounded_total, si.rounded_total, si.grand_total) amount,
            o.posting_date orig_date, o.pos_profile orig_profile
        FROM `tabSales Invoice` si
        LEFT JOIN `tabSales Invoice` o ON o.name = si.return_against
        WHERE si.docstatus = 1 AND si.is_return = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        ORDER BY si.posting_date""", dict(f), as_dict=True)

    for r in rows:
        flags = []
        if not r.return_against:
            flags.append("No original invoice")
        elif r.orig_profile and r.orig_profile != f.pos_profile:
            flags.append(f"Sold at {r.orig_profile}")
        if not r.update_stock:
            flags.append("Stock not returned")
        if r.orig_date:
            r.days_after = date_diff(r.posting_date, r.orig_date)
            if r.days_after > late_days:
                flags.append(f"Returned {r.days_after} days later")
        r.flags = ", ".join(flags)
    return rows


def returns_check(f):
    rows = [r for r in get_returns(f) if not f.get("cashier") or r.owner == f.cashier]
    flagged = sum(1 for r in rows if r.flags)

    columns = [
        {"fieldname": "name", "label": _("Credit Note"), "fieldtype": "Link", "options": "Sales Invoice", "width": 170},
        {"fieldname": "posting_date", "label": _("Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "return_against", "label": _("Original Invoice"), "fieldtype": "Link", "options": "Sales Invoice", "width": 170},
        {"fieldname": "orig_date", "label": _("Original Date"), "fieldtype": "Date", "width": 100},
        {"fieldname": "days_after", "label": _("Days After"), "fieldtype": "Int", "width": 80},
        {"fieldname": "amount", "label": _("Amount"), "fieldtype": "Currency", "width": 120},
        {"fieldname": "update_stock", "label": _("Stock Returned"), "fieldtype": "Check", "width": 90},
        {"fieldname": "owner", "label": _("Cashier"), "fieldtype": "Data", "width": 170},
        {"fieldname": "customer", "label": _("Customer"), "fieldtype": "Data", "width": 140},
        {"fieldname": "flags", "label": _("Flags"), "fieldtype": "Data", "width": 260},
    ]
    summary = [
        {"label": _("Returns Value"), "value": sum(flt(r.amount) for r in rows), "datatype": "Currency"},
        {"label": _("Credit Notes"), "value": len(rows), "datatype": "Int"},
        {"label": _("Flagged"), "value": flagged, "datatype": "Int", "indicator": "Red" if flagged else "Green"},
    ]
    return columns, rows, None, None, summary
