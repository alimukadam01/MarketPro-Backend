"""What the accounts app makes searchable: transactions and money accounts.

Both are accounting data, so both follow has_accounting_access - the paid
add-on for admins, the accounting permission for employees.
"""
from root.search import Hit, SearchType, link, register, status
from root.search.labels import joined, pkr

from .models import MoneyAccount, Transaction
from .utils import has_accounting_access

TRANSACTION_STATUS = {
    "C": ("Cleared", "success"), "PEN": ("Pending", "warning"), "B": ("Bounced", "danger"),
}


def _accounting(request, business, access):
    return has_accounting_access(request, business)


def _party(txn):
    """Who the money was with: the party on an on-account payment, or the
    customer / supplier of the invoice a receipt was recorded on."""
    try:
        payment = txn.party_payment
    except Exception:
        payment = None
    if payment is not None:
        party = payment.customer or payment.supplier
        if party is not None:
            return party.name
    if txn.sales_receipt_id and txn.sales_receipt.sales_invoice.customer_id:
        return txn.sales_receipt.sales_invoice.customer.name
    if txn.purchase_receipt_id and txn.purchase_receipt.purchase_invoice.supplier_id:
        return txn.purchase_receipt.purchase_invoice.supplier.name
    return ""


def _transaction_hit(txn):
    where = f"Cheque {txn.cheque_number}" if txn.cheque_number else getattr(txn.account, "name", "")
    label, tone = TRANSACTION_STATUS.get(txn.status, (txn.status, "neutral"))
    return Hit(
        id=txn.id,
        title=joined(pkr(txn.amount), txn.get_type_display()),
        subtitle=joined(_party(txn), where),
        amount=txn.amount,
        date=txn.date,
        status=status(txn.status, label, tone),
        link=link("/accounting/update-transaction", transaction_id=txn.id),
    )


register(SearchType(
    key="transaction",
    label="Transactions",
    model=Transaction,
    access=_accounting,
    text_fields=[
        "reference", "notes", "cheque_number",
        "account__name", "transfer_account__name",
        "party_payment__customer__name", "party_payment__supplier__name",
        "sales_receipt__sales_invoice__invoice_number",
        "sales_receipt__sales_invoice__customer__name",
        "purchase_receipt__purchase_invoice__invoice_number",
        "purchase_receipt__purchase_invoice__supplier__name",
    ],
    number_fields=["id", "amount"],
    phone_fields=["party_payment__customer__phone", "party_payment__supplier__phone"],
    # "customer payment" finds customer receipts; "cheque" finds cheques;
    # "bounced" finds bounced ones.
    choice_fields={"type": None, "payment_method": None, "status": None},
    breakdown="type",
    select_related=[
        "account", "party_payment__customer", "party_payment__supplier",
        "sales_receipt__sales_invoice__customer",
        "purchase_receipt__purchase_invoice__supplier",
    ],
    order_by=["-date", "-created_at", "-id"],
    priority=180,
    to_hit=_transaction_hit,
))

register(SearchType(
    key="money_account",
    label="Money accounts",
    model=MoneyAccount,
    access=_accounting,
    text_fields=["name"],
    number_fields=["id"],
    choice_fields={"type": None},
    order_by=["name", "id"],
    priority=190,
    to_hit=lambda account: Hit(
        id=account.id,
        title=account.name,
        subtitle=account.get_type_display(),
        status=None if account.is_active else status("OFF", "Inactive", "neutral"),
        link=link("/accounting/accounts"),
    ),
))
