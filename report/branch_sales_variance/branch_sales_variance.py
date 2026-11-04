# Branch Sales Variance — Lyons Liqour Lodge
# Rebuilds the "RUIRU Bank Reconciliation" workbook inside ERPNext.
#
# Views
#   Daily Money    : statement vs system sales per channel (Sheet1 of the workbook)
#   Stock Variance : stock reconciliation differences valued at selling price (Variance sheet)
#   Returns Check  : credit notes with risk flags
#
# Money logic per channel and day
#   Statement      = Bank Transaction deposits (Equity / NCBA / I&M / Co-op) or the
#                    manual figure in "Branch Daily Takings" (cash, Pochi, banks with no import)
#   Non-sales      = everything else posted into that account that day per the GL
#                    (debts collected, other branches, transfers, journals)  +  manual non-sales
#   Bank net       = Statement - Non-sales
#   System sales   = this branch's same-day sales collections in that channel
#   Variance       = Bank net - System sales   (negative = short)

import frappe
from frappe import _
from frappe.utils import flt, getdate, add_days, date_diff

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


def daily_money(f):
    days = {}  # date -> {"_": {...}, channel: {...}}

    def cell(d, ch):
        day = days.setdefault(getdate(d), {})
        return day.setdefault(ch, {"statement": 0.0, "non_sales": 0.0, "system": 0.0,
                                   "gl_in": 0.0, "manual_stmt": None, "manual_ns": 0.0,
                                   "shift_stmt": None})

    imported_channels = set()
    shift_stats = {"total": 0, "uncounted": 0, "multi_day": 0}

    def head(d):
        day = days.setdefault(getdate(d), {})
        return day.setdefault("_", {"gross": 0.0, "returns": 0.0, "collected": 0.0, "debts": 0.0})

    params = dict(f)

    # 1. sales and returns
    for r in frappe.db.sql(f"""
        SELECT si.posting_date d,
            SUM(CASE WHEN si.is_return = 0 THEN IF(si.rounded_total, si.rounded_total, si.grand_total) ELSE 0 END) gross,
            SUM(CASE WHEN si.is_return = 1 THEN IF(si.rounded_total, si.rounded_total, si.grand_total) ELSE 0 END) returns
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.posting_date""", params, as_dict=True):
        h = head(r.d)
        h["gross"] += flt(r.gross)
        h["returns"] += flt(r.returns)

    # 2. collections through Payment Entries (how POSNext records payments here)
    for r in frappe.db.sql(f"""
        SELECT pe.posting_date d, pe.mode_of_payment mop,
            SUM(CASE WHEN si.posting_date = pe.posting_date THEN per.allocated_amount ELSE 0 END) same_day,
            SUM(CASE WHEN si.posting_date <> pe.posting_date THEN per.allocated_amount ELSE 0 END) debts
        FROM `tabPayment Entry Reference` per
        JOIN `tabPayment Entry` pe ON pe.name = per.parent
        JOIN `tabSales Invoice` si ON si.name = per.reference_name
        WHERE pe.docstatus = 1 AND pe.payment_type = 'Receive' AND pe.company = %(company)s
          AND per.reference_doctype = 'Sales Invoice'
          AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY pe.posting_date, pe.mode_of_payment""", params, as_dict=True):
        cell(r.d, channel_of(r.mop))["system"] += flt(r.same_day)
        h = head(r.d)
        h["collected"] += flt(r.same_day)
        h["debts"] += flt(r.debts)

    # 3. collections inside the invoice itself (is_pos payments table), net of change
    for r in frappe.db.sql(f"""
        SELECT si.posting_date d, sip.mode_of_payment mop, SUM(sip.base_amount) amt
        FROM `tabSales Invoice Payment` sip
        JOIN `tabSales Invoice` si ON si.name = sip.parent
        WHERE si.docstatus = 1 AND si.is_pos = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.posting_date, sip.mode_of_payment""", params, as_dict=True):
        cell(r.d, channel_of(r.mop))["system"] += flt(r.amt)
        head(r.d)["collected"] += flt(r.amt)
    for r in frappe.db.sql(f"""
        SELECT si.posting_date d, SUM(si.base_change_amount) chg
        FROM `tabSales Invoice` si
        WHERE si.docstatus = 1 AND si.is_pos = 1 AND si.company = %(company)s
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s AND {BRANCH_COND}
        GROUP BY si.posting_date HAVING chg <> 0""", params, as_dict=True):
        cell(r.d, "cash")["system"] -= flt(r.chg)
        head(r.d)["collected"] -= flt(r.chg)

    # 4. bank statements (imported Bank Transactions) and everything booked into each bank
    bank_accounts = {c[0]: c[3] for c in CHANNELS if c[3]}
    if bank_accounts and f.get("use_bank_statements", 1):
        gl_account = dict(frappe.db.sql(
            "SELECT name, account FROM `tabBank Account` WHERE name IN %(n)s",
            {"n": tuple(bank_accounts.values())}))
        ba_channel = {v: k for k, v in bank_accounts.items()}
        # only banks whose statements are actually imported for this range
        imported = {r[0] for r in frappe.db.sql("""
            SELECT DISTINCT bank_account FROM `tabBank Transaction`
            WHERE docstatus = 1 AND bank_account IN %(ba)s
              AND date BETWEEN %(from_date)s AND %(to_date)s""",
            dict(params, ba=tuple(bank_accounts.values())))}
        acc_channel = {gl_account[v]: k for k, v in bank_accounts.items()
                       if gl_account.get(v) and v in imported}
        imported_channels = {ba_channel[b] for b in imported}

        for r in frappe.db.sql("""
            SELECT date d, bank_account ba, SUM(deposit) dep
            FROM `tabBank Transaction`
            WHERE docstatus = 1 AND bank_account IN %(ba)s
              AND date BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY date, bank_account""",
            dict(params, ba=tuple(bank_accounts.values())), as_dict=True):
            cell(r.d, ba_channel[r.ba])["statement"] += flt(r.dep)

        if acc_channel:
            for r in frappe.db.sql("""
                SELECT posting_date d, account acc, SUM(debit) dr
                FROM `tabGL Entry`
                WHERE is_cancelled = 0 AND company = %(company)s AND account IN %(acc)s
                  AND posting_date BETWEEN %(from_date)s AND %(to_date)s
                GROUP BY posting_date, account""",
                dict(params, acc=tuple(acc_channel.keys())), as_dict=True):
                cell(r.d, acc_channel[r.acc])["gl_in"] += flt(r.dr)

    # 5. manual takings (cash count, Pochi statement, I&M/Co-op if not imported)
    if frappe.db.table_exists("Branch Daily Takings"):
        for r in frappe.db.sql("""
            SELECT t.posting_date d, l.channel ch,
                SUM(l.statement_amount) stmt, SUM(l.non_sales_amount) ns,
                SUM(CASE WHEN l.statement_amount <> 0 THEN 1 ELSE 0 END) has_stmt
            FROM `tabBranch Takings Line` l
            JOIN `tabBranch Daily Takings` t ON t.name = l.parent
            WHERE t.docstatus = 1 AND t.pos_profile = %(pos_profile)s
              AND t.posting_date BETWEEN %(from_date)s AND %(to_date)s
            GROUP BY t.posting_date, l.channel""", params, as_dict=True):
            c = cell(r.d, LABEL_CHANNEL.get(r.ch, "other"))
            if r.has_stmt:
                c["manual_stmt"] = flt(r.stmt)
            c["manual_ns"] += flt(r.ns)

    # 6. POSNext closing shifts: the cashier's counted amount per mode of payment
    for shift in get_closing_shifts(f):
        shift_stats["total"] += 1
        if shift.multi_day:
            shift_stats["multi_day"] += 1
        if not shift.counted:
            shift_stats["uncounted"] += 1
            continue
        for mop, amt in shift.closing.items():
            c = cell(shift.posting_date, channel_of(mop))
            c["shift_stmt"] = (c["shift_stmt"] or 0.0) + amt

    # resolve each day/channel.  Priority: manual takings > imported bank statement > closing shift
    for d, day in days.items():
        for ch, c in day.items():
            if ch == "_":
                continue
            auto_ns = 0.0
            if c["manual_stmt"] is not None:
                c["statement"] = c["manual_stmt"]
                c["source"] = "Manual"
            elif ch in imported_channels:
                c["source"] = "Bank Statement"
                auto_ns = c["gl_in"] - c["system"]  # booked into the account but not this branch's same-day sales
            elif c["shift_stmt"] is not None:
                c["statement"] = c["shift_stmt"]
                c["source"] = "Closing Shift"
            else:
                c["statement"] = 0.0
                c["source"] = "No Statement" if c["system"] else ""
            c["non_sales"] = auto_ns + c["manual_ns"]

    return (get_money_columns(), build_money_rows(days, f), None, None,
            money_summary(days, shift_stats))


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


def build_money_rows(days, f):
    period = f.get("period") or "Day"
    agg = {}
    for d, day in days.items():
        p = agg.setdefault(period_key(d, period), {})
        for ch, c in day.items():
            t = p.setdefault(ch, {})
            for k, v in c.items():
                if isinstance(v, float):
                    t[k] = t.get(k, 0.0) + v
            if ch != "_":
                srcs = t.setdefault("sources", set())
                if c.get("source"):
                    srcs.add(c["source"])

    rows, order = [], [c[0] for c in CHANNELS]
    for p in sorted(agg):
        day = agg[p]
        h = day.get("_", {})
        net_sales = h.get("gross", 0) + h.get("returns", 0)
        parent = {
            "name": str(p), "label": frappe.format(p, {"fieldtype": "Date"}), "indent": 0,
            "gross_sales": h.get("gross", 0), "returns": h.get("returns", 0),
            "net_sales": net_sales, "credit_sales": net_sales - h.get("collected", 0),
            "debts_collected": h.get("debts", 0),
            "statement": 0, "non_sales": 0, "bank_net": 0, "system_sales": 0, "variance": 0,
        }
        children = []
        for ch in order:
            c = day.get(ch)
            if not c or not any(abs(c.get(k, 0)) > 0.005 for k in ("statement", "non_sales", "system")):
                continue
            bank_net = c["statement"] - c["non_sales"]
            row = {
                "name": f"{p}-{ch}", "parent_name": str(p), "label": CHANNEL_LABEL[ch], "indent": 1,
                "statement": c["statement"], "non_sales": c["non_sales"], "bank_net": bank_net,
                "system_sales": c["system"], "variance": bank_net - c["system"],
                "source": ", ".join(sorted(c.get("sources") or [])),
            }
            for k in ("statement", "non_sales", "bank_net", "system_sales", "variance"):
                parent[k] += row[k]
            children.append(row)
        rows.append(parent)
        rows.extend(children)

    if rows:
        total = {"label": _("Total"), "indent": 0, "bold": 1}
        for k in ("gross_sales", "returns", "net_sales", "credit_sales", "debts_collected",
                  "statement", "non_sales", "bank_net", "system_sales", "variance"):
            total[k] = sum(r.get(k, 0) for r in rows if r["indent"] == 0)
        rows.append(total)
    return rows


def money_summary(days, shift_stats):
    t = {"stmt": 0, "net": 0, "sys": 0, "missing": 0}
    for day in days.values():
        for ch, c in day.items():
            if ch == "_":
                continue
            t["stmt"] += c["statement"]
            t["net"] += c["statement"] - c["non_sales"]
            t["sys"] += c["system"]
            if c.get("source") == "No Statement":
                t["missing"] += c["system"]
    var = t["net"] - t["sys"]
    return [
        {"label": _("Statement Received"), "value": t["stmt"], "datatype": "Currency"},
        {"label": _("Bank Net Sales"), "value": t["net"], "datatype": "Currency"},
        {"label": _("System Sales Collected"), "value": t["sys"], "datatype": "Currency"},
        {"label": _("Net Variance"), "value": var, "datatype": "Currency",
         "indicator": "Red" if var < -0.5 else "Green"},
        {"label": _("System Sales With No Statement"), "value": t["missing"], "datatype": "Currency",
         "indicator": "Orange" if t["missing"] else "Green"},
        {"label": _("Shifts Closed Without Count"),
         "value": f"{shift_stats['uncounted']} / {shift_stats['total']}", "datatype": "Data",
         "indicator": "Orange" if shift_stats["uncounted"] else "Green"},
        {"label": _("Shifts Spanning Several Days"), "value": shift_stats["multi_day"], "datatype": "Int",
         "indicator": "Orange" if shift_stats["multi_day"] else "Green"},
    ]


def get_money_columns():
    cur = lambda fn, lb, w=130: {"fieldname": fn, "label": _(lb), "fieldtype": "Currency", "width": w}
    return [
        {"fieldname": "label", "label": _("Date / Channel"), "fieldtype": "Data", "width": 170},
        cur("statement", "Statement"),
        cur("non_sales", "Non-Sales / Debts"),
        cur("bank_net", "Bank Net Sales"),
        cur("system_sales", "System Sales"),
        cur("variance", "Variance"),
        cur("gross_sales", "Gross Sales"),
        cur("returns", "Returns"),
        cur("net_sales", "Net Sales"),
        cur("credit_sales", "Credit Sales"),
        cur("debts_collected", "Old Debts Collected"),
        {"fieldname": "source", "label": _("Statement Source"), "fieldtype": "Data", "width": 130},
    ]


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
