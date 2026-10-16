from urllib.parse import quote

from django.db import transaction
from typing import Dict, Set, Tuple
from django.db.models import Q, QuerySet
from django.db.models import Sum
from rest_framework.exceptions import ValidationError
from .models import PurchaseInvoiceItem, PurchaseInvoiceItemRestock, SalesInvoice, SalesInvoiceItem, SalesInvoiceItemDeduction
from inventory.models import InventoryItem
from .models import PurchaseInvoice
from .models import PurchaseInvoiceItemRestock
from root.utils import generateTransactionId, local_date, whatsapp_number


def getRestockField(is_partial: bool) -> str:
        return 'quantity_received' if is_partial else 'quantity'

def spiltNewAndOldProducts(
        product_ids: Set[int], business_id: int
    ) -> Tuple[QuerySet['InventoryItem'], Set[int], Set[int]]:
        existing = InventoryItem.objects.filter(product_id__in=product_ids, business_id=business_id)
        existing_ids = {inv.product_id for inv in existing}
        new_ids = product_ids - existing_ids
        return existing, existing_ids, new_ids

def logRestockEvent(item, quantity_received: int, invoice):
        """
        Record one BaseRestock instance for this item.
        """
        if isinstance(item, PurchaseInvoiceItem):
            PurchaseInvoiceItemRestock.objects.create(
                purchase_invoice_item=item,
                purchase_invoice=invoice,
                quantity=quantity_received
            )
        elif isinstance(item, SalesInvoiceItem):
            SalesInvoiceItemDeduction.objects.create(
                sales_invoice_item=item,
                sales_invoice=invoice,
                quantity=quantity_received
            )

def createInventoryItemFromRestock(item, qty_field: str) -> 'InventoryItem':
        """
        Instantiate a new InventoryItem using this restock's full amount.
        """
        data = item.morph()
        qty = getattr(item, qty_field)
        return InventoryItem.objects.create(
            business_id = data['business_id'],
            inventory_id = data['inventory_id'],
            location_id = data['location_id'],
            product_id = data['product_id'],
            quantity = qty,
            quantity_on_hand = qty,
            track_code = data.get('track_code'),
            notes = data.get('notes'),
            unit_cost = data.get('unit_cost'),
            last_transaction = data['last_transaction']
        )

def update_inventory(instance, is_partially_received):
    updated_invoice_items = []
    restock_objs = []

    # Decide which field holds the actual restocked amount
    qty_field = 'quantity_received' if is_partially_received else 'quantity'
    invoice_items = instance.invoice_items.all()

    # Map product_id → item data
    invoice_items_dict = {
        item.product.id: item for item in invoice_items
    }

    product_ids = set(invoice_items_dict) ### all products in the invoice.

    existing_inv_items = InventoryItem.objects.filter(product_id__in=product_ids) # existing inventory items
    existing_product_ids = {inv.product.id for inv in existing_inv_items} # set of product_ids in existing inventory items
    new_product_ids = product_ids - existing_product_ids # new inventory products

    # 1) Update existing InventoryItems
    to_update = []
    for item in existing_inv_items:
        inv_item = invoice_items_dict[item.product.id]
        data = inv_item.morph()
        agg = inv_item.restocks.aggregate(
			quantity_received = Sum(qty_field)
		)['quantity_received']
        prev_restock = agg or 0
        delta = getattr(inv_item, qty_field) - prev_restock
        item.quantity += delta
        item.last_transaction_id = data['last_transaction_id']

        inv_item.is_restocked = True if inv_item.quantity == inv_item.quantity_received else False        
        inv_item.is_partiallY_restocked = False if inv_item.quantity >= inv_item.quantity_received else True      

        updated_invoice_items.append(inv_item)
        to_update.append(item)
        restock_objs.append(
            PurchaseInvoiceItemRestock(
                purchase_invoice_id = instance.id,
                purchase_invoice_item_id = inv_item.id,
                quantity_received = delta
            )
        )

    # 2) Create new InventoryItems for products we haven’t seen before
    to_create = []
    for product_id in new_product_ids:
        inv_item = invoice_items_dict[product_id]
        data = inv_item.morph()

        to_create.append(InventoryItem(
            business_id = data['business_id'],
            inventory_id = data['inventory_id'],
            location_id = data['location_id'],
            product_id = product_id,
            quantity = data[qty_field],
            track_code = data.get('track_code'),
            notes = data.get('notes'),
            unit_cost = data.get('unit_cost'),
            last_transaction_id = data['last_transaction_id']
        ))

        inv_item.is_restocked = True if inv_item.quantity == inv_item.quantity_received else False        
        inv_item.is_partiallY_restocked = False if inv_item.quantity >= inv_item.quantity_received else True      

        updated_invoice_items.append(inv_item)
        restock_objs.append(
            PurchaseInvoiceItemRestock(
                purchase_invoice_id = instance.id,
                purchase_invoice_item_id = inv_item.id,
                quantity_received = data[qty_field]
            )
        )

    try:
        with transaction.atomic():
            if to_update:
                InventoryItem.objects.bulk_update(
                    to_update,
                    ['quantity', 'quantity_on_hand', 'last_transaction_id']
                )

            if to_create:
                InventoryItem.objects.bulk_create(to_create)

            if updated_invoice_items:
                PurchaseInvoiceItem.objects.bulk_update(
                    updated_invoice_items,
                    ['is_partially_restocked', 'is_restocked']
                )

            if restock_objs:
                PurchaseInvoiceItemRestock.objects.bulk_create(restock_objs)

    except Exception as error:
        print(error)

def printObject(instance):
     
    for key, value in vars(instance).items():
        print(f"{key}: {value}")

def checkPurchaseInvoiceItemFields(attrs, context):
     
    if attrs.get('quantity_received') > attrs.get('quantity'):
        raise ValidationError("quantity_received cannot be higher than quantity")

    if PurchaseInvoice.objects.filter(id=context['purchase_invoice_id']).values_list('status', flat=True).first() == 'R':
        raise ValidationError("cannot add purchase invoice items when purchase invoice satus = received")
    
def checkPurchaseInvoiceCreateFields(attrs):
     
    if attrs.get('status') in ('R', 'PR'):
        raise ValidationError('''
            status cannot be 'PARTIALLY_RECEIVED' or 'RECEIVED' while Purchase Invoice creation
        ''')
    
def checkSalesInvoiceItemCreateFields(attrs):

    if attrs.get('quantity') <= 0:
        raise ValidationError("quantity must be greater than 0")

    if attrs.get('unit_price') <= 0:
        raise ValidationError("unit_price must be greater than 0")

    product_id = attrs.get('product').id
    if not InventoryItem.objects.filter(product_id=product_id, quantity_on_hand__gte=attrs.get('quantity')).exists():
        raise ValidationError(f"Insufficient stock.")
    
def reserveSalesInvoiceItem(item):
    pass

def updateInventoryOnSale(instance: SalesInvoice):
    """
    Move the sold goods out of stock.

    Gated on the invoice being COMPLETED, which is now derived from it having
    lines rather than chosen by hand - so completing an invoice deducts, and
    nothing else does. It used to run for 'C' or 'PC', and 'PC' was itself
    written by the deduction code, which made the rule circular.

    A cancelled invoice never deducts; restockOnCancel below puts back
    anything it already had.

    Guarded per line, not per invoice. A line added to an invoice that is
    already deducted leaves the invoice flag True, so an early return on it
    would mean the new line never left stock - Bug #001 again, one level up.
    """
    if instance.status in instance.CANCELLED_STATUSES:
        return
    if instance.stored_status != 'C':
        return

    invoice_items_dict = instance.items_by_product()
    if not invoice_items_dict:
        return

    # The old `quantity_field = getRestockField(...)` branch stood here. It was
    # the only thing that read the 'C' / 'PC' distinction, and nothing ever
    # read the value it computed - the delta always came from
    # compute_restock_delta(). Removed with the status overload.
    product_ids = set(invoice_items_dict)
    existing, _, new_ids = spiltNewAndOldProducts(product_ids, instance.business.id)

    for inventory_item in existing:
        # One product can be on several lines at different prices, and every
        # line's quantity has to leave stock.
        for item in invoice_items_dict[inventory_item.product_id]:
            # The flag, not the delta. Most migrated lines carry is_deducted
            # with an empty deduction ledger - deliberately, so importing them
            # moved no stock - and for those the delta is the full quantity.
            # Trusting the delta alone would deduct every one of them the
            # first time its invoice was edited.
            if item.is_deducted:
                continue
            delta = item.compute_restock_delta()
            if delta > 0:
                inventory_item.apply_restock_delta(True, delta, generateTransactionId(instance))
                logRestockEvent(item, delta, instance)
                item.update_restock_flags()

    instance.update_deduction_flags()
    # Narrowed from a bare save(): this instance may predate adjust_totals, so
    # writing every column could put a stale total back.
    instance.save(update_fields=['is_deducted'])


def restockOnCancel(instance: SalesInvoice):
    """
    Cancelling an invoice returns its goods to stock.

    The deduction rows are deleted rather than offset with a counter-entry,
    because compute_restock_delta() measures what is left to deduct as
    quantity minus the logged deductions. Clearing them makes the line look
    undeducted again, so un-cancelling deducts it exactly once more and stock
    cannot drift either way.

    Nothing to undo for an invoice that never deducted - in particular the
    migrated invoices, which carry is_deducted with an empty ledger precisely
    so that their stock is never touched.
    """
    items_by_product = instance.items_by_product()
    if not items_by_product:
        return

    stock_rows = InventoryItem.objects.filter(
        business_id=instance.business_id,
        product_id__in=set(items_by_product),
    )

    for inventory_item in stock_rows:
        returned = 0
        for item in items_by_product[inventory_item.product_id]:
            deducted = item.restocks.aggregate(
                total=Sum('quantity'))['total'] or 0
            if not deducted:
                continue
            returned += deducted
            item.restocks.all().delete()
            item.is_deducted = False
            item.save(update_fields=['is_deducted'])

        if returned:
            # Added directly rather than through apply_restock_delta(False, ...),
            # which also overwrites unit_cost - a cancellation says nothing
            # about what the stock cost.
            inventory_item.quantity += returned
            inventory_item.quantity_on_hand += returned
            inventory_item.last_transaction = generateTransactionId(instance)
            inventory_item.save(update_fields=[
                'quantity', 'quantity_on_hand', 'last_transaction'])

    instance.is_deducted = False
    instance.save(update_fields=['is_deducted'])


def build_whatsapp_payload(invoice):
    """
    The wa.me link and its message for one invoice, or None when there is no
    number to send it to.

    Never raises. It is called after a write has already committed, where an
    exception would be reported to the user as a failure the database does not
    agree with.
    """
    try:
        customer = invoice.customer
        number = whatsapp_number(customer.phone) if customer else ''
        if not number:
            return None

        total = round(invoice.total or 0)
        paid = round(invoice.amount_paid or 0)
        due = total - paid
        issued = local_date(invoice.date_issued or invoice.created_at)

        lines = [
            f"Assalam-o-Alaikum {customer.name},",
            "",
            f"Invoice {invoice.invoice_number or invoice.id} "
            f"from {invoice.business.name}",
            f"Date: {issued.strftime('%d/%m/%Y')}",
            "",
            f"Items: {invoice.invoice_items.count()}",
        ]

        # Itemised above the total, not folded into it. A customer who is being
        # asked for a delivery fee should be able to see that is what it is.
        for charge in (invoice.extra_charges or []):
            try:
                label = str(charge.get('label') or '').strip()
                amount = round(float(charge.get('amount') or 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if label and amount:
                lines.append(f"{label}: PKR {amount:,}")

        lines.append(f"Total: PKR {total:,}")

        # A customer who has cleared the bill should not be shown a balance
        # line at all - it reads like a demand.
        if paid:
            lines.append(f"Paid: PKR {paid:,}")
        if due > 0:
            lines.append(f"Balance due: PKR {due:,}")
        else:
            lines.append("Paid in full - thank you.")

        lines += ["", "Thank you for your business."]
        message = "\n".join(lines)

        return {
            'phone': number,
            'message': message,
            'whatsapp_url': f"https://wa.me/{number}?text={quote(message)}",
        }

    except Exception as error:
        print(error)
        return None


# ── Receipt / transaction money details ───────────────────────────────────────
# Shared by the receipt serializers here, the expense serializer in root/, and
# the transaction dispatcher in accounts/. Kept in one place so a payment
# recorded on an invoice screen and the same payment recorded on the
# transaction screen patch the mirrored transaction identically.

# A payment holds its share of the invoice while it is cleared or still
# pending. A bounced one never arrives, so it frees its share up again.
# Receipts with no transaction behind them predate the accounting module.
LIVE_RECEIPT = (
    Q(transaction_record__isnull=True) |
    Q(transaction_record__status__in=['C', 'PEN'])
)


def live_receipt_total(invoice):
    """
    What the invoice already has spoken for, counting pending cheques.
    """
    return (
        invoice.payment_receipts
        .filter(LIVE_RECEIPT)
        .aggregate(total=Sum('amount'))['total'] or 0
    )


def invoice_room(invoice):
    """
    How much more this invoice can still accept as a payment that names it.
    Never negative.

    Less what its own payments have claimed (pending cheques included) AND
    what the party's khaata has already settled onto it - otherwise an
    invoice the pool covers would still take a payment, and the party would
    pay the same debt twice. This cap is what keeps named payments and the
    pool from contradicting each other (credit-debit-notes plan, section 4).
    """
    return max(
        float(invoice.total or 0)
        - float(live_receipt_total(invoice))
        - float(invoice.pool_applied or 0),
        0.0,
    )


def apply_money_details(source, money_details):
    """
    Push the money details onto the transaction a signal mirrored from this
    source record. `source` is anything carrying a `transaction_record`
    reverse accessor: a SalesReceipt, a PurchaseReceipt or an Expense.

    Returns the patched Transaction, so a caller that needs the mirrored row
    does not have to go looking for it again.

    Deliberately does NOT swallow. This used to catch every exception and
    return, which turned "the mirror never ran" into a 201: the invoice was
    paid down while the money sat on the default account, on the wrong date,
    with no photo. Letting Transaction.DoesNotExist propagate rolls the source
    record back with it.

    The caller may pass anything settable on Transaction, not just
    MONEY_FIELDS - the transaction dispatcher also passes image, reference and
    created_by_id.
    """
    transaction_record = source.transaction_record

    if not money_details:
        return transaction_record

    # A cheque stays pending until it clears, so it moves no money yet.
    if money_details.get('payment_method') == 'cheque':
        money_details['status'] = 'PEN'

    for attr, value in money_details.items():
        setattr(transaction_record, attr, value)
    transaction_record.save(update_fields=list(money_details.keys()))

    return transaction_record


# ── Extra charges as expenses ─────────────────────────────────────────────────
#
# Every extra charge on a sales invoice is money the shop pays out again - the
# rider, the labourer - so each one is mirrored as an Expense in the
# "invoice_extra_charge" category, and through the Expense mirror as an
# expense Transaction. The customer pays the charge as part of the invoice
# total, which counts in revenue; the expense takes it back out, so a charge
# nets to nothing in profit.
#
# The expense goes out of the account the customer's payment came into. Until
# there is a payment there is no account to choose, so there is no expense.

# Which account wins when an invoice was paid into several: cash first, then
# bank, then a mobile wallet.
CHARGE_ACCOUNT_PRIORITY = ['cash', 'bank', 'wallet']

# The payment method a transaction on each kind of account carries.
ACCOUNT_PAYMENT_METHOD = {
    'cash': 'cash',
    'bank': 'bank_transfer',
    'wallet': 'wallet',
}


def deleting_with_invoice(origin):
    """
    True when a delete began at a sales invoice (or its whole business), so
    the line or payment being deleted is going with it.

    Django deletes an invoice's lines and payments before the invoice row, and
    their post_delete receivers would otherwise resync the invoice: making
    expenses that point at a row about to disappear, which PostgreSQL refuses.
    `origin` is what Django passes post_delete: the instance or queryset whose
    delete() started it all.
    """
    from root.models import Business

    if origin is None:
        return False
    model = origin.model if isinstance(origin, QuerySet) else type(origin)
    return model in (SalesInvoice, Business)


def paid_into_account(invoice):
    """
    The account an invoice's own payments came into - which its extra charges
    are paid out of, and an automatic refund goes back through - or None if
    no payment has come in yet. Works for a sales or a purchase invoice: both
    keep their receipts under payment_receipts.

    Of the accounts the invoice's own payments went into, the first type in
    CHARGE_ACCOUNT_PRIORITY wins. Two accounts of that type: the one that took
    more of the money, then the one paid into first.

    A bounced cheque never brought money into its account, so it is ignored. A
    pending cheque counts: it names the account the money is going to.
    Settlement from the khaata (credit notes, money on account) moves no money
    into any account, so it never decides this.
    """
    received = {}   # account id -> [account, total received, first receipt id]
    receipts = invoice.payment_receipts.select_related('transaction_record__account')
    for receipt in receipts:
        txn = getattr(receipt, 'transaction_record', None)
        if txn is None or txn.account_id is None or txn.status == 'B':
            continue
        entry = received.setdefault(txn.account_id, [txn.account, 0, receipt.id])
        entry[1] += txn.amount or 0
        entry[2] = min(entry[2], receipt.id)

    for account_type in CHARGE_ACCOUNT_PRIORITY:
        candidates = [e for e in received.values() if e[0].type == account_type]
        if candidates:
            candidates.sort(key=lambda e: (-e[1], e[2]))
            return candidates[0][0]
    return None


def sync_charge_expenses(invoice):
    """
    Make an invoice's charge expenses match its extra charges, now.

    Idempotent, so every write that could change the answer calls it rather
    than working out what changed: the charges, the lines (Draft has none), a
    cancellation, a payment arriving or going, a cheque bouncing. When nothing
    differs it writes nothing.

    One expense per charge, dated on the invoice - the day the delivery or the
    labour happened - and on the account paid_into_account() picks. None at all
    for a draft or a cancelled invoice, or while nothing has been paid.

    When anything differs the old expenses are deleted and new ones made,
    rather than patched one by one: a charge has no identity of its own to
    match an expense against, only a position in a JSON list.
    """
    # Function-level imports: accounts and root both sit above sales.
    from root.models import Expense

    invoice = SalesInvoice.objects.filter(pk=invoice.pk).first()
    if invoice is None:
        return

    desired = []
    account = None
    if (invoice.status not in invoice.CANCELLED_STATUSES
            and invoice.status not in invoice.DRAFT_STATUSES
            and invoice.extra_charges):
        account = paid_into_account(invoice)
        if account is not None:
            for charge in invoice.extra_charges:
                try:
                    label = str(charge.get('label') or '').strip()
                    amount = round(float(charge.get('amount') or 0), 2)
                except (AttributeError, TypeError, ValueError):
                    continue
                if label and amount > 0:
                    desired.append((label, amount))

    date = local_date(invoice.date_issued or invoice.created_at)
    method = ACCOUNT_PAYMENT_METHOD.get(account.type) if account else None

    existing = list(
        Expense.objects.filter(sales_invoice=invoice)
        .select_related('transaction_record').order_by('id'))

    def unchanged():
        if len(existing) != len(desired):
            return False
        for expense, (label, amount) in zip(existing, desired):
            txn = getattr(expense, 'transaction_record', None)
            if (expense.name != label
                    or round(expense.amount or 0, 2) != amount
                    or txn is None
                    or txn.account_id != account.id
                    or txn.date != date
                    or txn.payment_method != method):
                return False
        return True

    if unchanged():
        return

    with transaction.atomic():
        for expense in existing:
            expense.delete()   # its transaction goes with it (CASCADE)

        reference = invoice.invoice_number or invoice.id
        for label, amount in desired:
            expense = Expense.objects.create(
                business_id=invoice.business_id,
                sales_invoice=invoice,
                name=label,
                category=Expense.INVOICE_CHARGE_CATEGORY,
                amount=amount,
                desc=f"Extra charge on sales invoice {reference}",
            )
            # The Expense mirror has just made the transaction on the default
            # account, dated today; point it at the right account and day.
            apply_money_details(expense, {
                'account_id': account.id,
                'date': date,
                'payment_method': method,
            })


# ── Automatic refunds ─────────────────────────────────────────────────────────
#
# An Update that brings an invoice's billed amount under what has been paid on
# it - an item removed, the discount raised, a charge taken off - leaves the
# party paid for more than they were billed. The khaata already reads the
# excess as an "overpayment" credit; the system records a refund of it, which
# the khaata reads as a debt that uses the credit up. If the user deletes the
# refund, the excess simply stays on the khaata as credit.

# Refund types per invoice kind: money back to a customer goes out; money
# back from a supplier comes in.
REFUND_TYPE = {'sales': 'sales_return_refund', 'purchase': 'purchase_return_refund'}


def overpaid_by(invoice):
    """
    How much more has been paid on this invoice than it bills. Never negative.

    Matches the khaata (accounts/settlement.py): only cleared payments count,
    and a draft or cancelled invoice bills nothing, so everything paid on one
    is excess. Reads the invoice's receipts through payment_receipts.all(), so
    pass an instance fetched after the change - a prefetched one still holds
    the receipts from before.
    """
    excluded = invoice.CANCELLED_STATUSES + invoice.DRAFT_STATUSES
    billed = 0.0 if invoice.status in excluded else float(invoice.total or 0)
    return max(float(invoice.receipts_paid or 0) - billed, 0.0)


def refund_new_overpayment(invoice, before, user_id=None):
    """
    Refund the overpayment an Update has just created, if any. Returns the
    refund Transaction, or None.

    `before` is overpaid_by() taken before the Update. Only the increase is
    refunded: an invoice already overpaid before this edit - because a refund
    was deleted, say - is not refunded again for that, so deleting a refund
    stays deleted.

    Whole rupees, as every transaction is. Out of (or for a supplier, into)
    the account the invoice's payments used, by the same rule as its extra
    charges, falling back to the default account. Dated today: the day the
    money goes back.
    """
    # Function-level imports: accounts sits above sales.
    from accounts.models import MoneyAccount, PartyPayment, Transaction
    from django.utils import timezone

    is_sales = isinstance(invoice, SalesInvoice)
    model = SalesInvoice if is_sales else PurchaseInvoice
    invoice = model.objects.filter(pk=invoice.pk).first()
    if invoice is None:
        return None

    excess = round(overpaid_by(invoice) - (before or 0))
    if excess < 1:
        return None

    account = (paid_into_account(invoice)
               or MoneyAccount.objects.get_default_account(invoice.business_id))
    reference = invoice.invoice_number or invoice.id

    with transaction.atomic():
        refund = Transaction.objects.create(
            business_id=invoice.business_id,
            account=account,
            type=REFUND_TYPE['sales' if is_sales else 'purchase'],
            amount=excess,
            date=timezone.localdate(),
            payment_method=ACCOUNT_PAYMENT_METHOD.get(account.type, 'cash'),
            status='C',
            notes=(f"Refund of the overpayment on "
                   f"{'sales' if is_sales else 'purchase'} invoice {reference}"),
            created_by_id=user_id,
        )
        PartyPayment.objects.create(
            business_id=invoice.business_id,
            customer_id=invoice.customer_id if is_sales else None,
            supplier_id=None if is_sales else invoice.supplier_id,
            transaction=refund,
            sales_invoice=invoice if is_sales else None,
            purchase_invoice=None if is_sales else invoice,
        )
    return refund
