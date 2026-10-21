"""What the sales app makes searchable: invoices and their items, purchase
quotations and their items, returned items and invoice images."""
from django.db.models import Q

from root.search import Hit, SearchType, link, register, status
from root.search.labels import joined, product_label

from .models import (
    InvoiceImage, PurchaseInvoice, PurchaseInvoiceItem, PurchaseQuotation,
    PurchaseQuotationItem, ReturnedItem, SalesInvoice, SalesInvoiceItem,
)

CREATED_BY = ["created_by__first_name", "created_by__last_name", "created_by__email"]

SALES_STATUS = {
    "C": ("Completed", "success"), "D": ("Draft", "warning"),
    "O": ("Overdue", "danger"), "X": ("Cancelled", "danger"),
}
PURCHASE_STATUS = {
    "R": ("Received", "success"), "PR": ("Partially Received", "warning"),
    "D": ("Draft", "warning"), "O": ("Overdue", "danger"), "C": ("Cancelled", "danger"),
}
QUOTATION_STATUS = {
    "A": ("Accepted", "success"), "S": ("Sent", "warning"), "D": ("Draft", "warning"),
    "R": ("Rejected", "danger"), "X": ("Cancelled", "danger"),
}


def _status(table, code):
    label, tone = table.get(code, (code, "neutral"))
    return status(code, label, tone)


def _attach_settlement(invoices):
    # The derived status (Overdue) needs what the khaata settled onto each
    # invoice; one pass per party for the five hits.
    from accounts.settlement import attach_settlement
    attach_settlement(invoices)


# ── Sales invoices and their items ───────────────────────────────────────────

register(SearchType(
    key="sales_invoice",
    label="Sales invoices",
    model=SalesInvoice,
    module="sales",
    text_fields=["invoice_number", "customer__name", "notes", *CREATED_BY,
                 "projects__project__name"],
    number_fields=["id", "total"],
    phone_fields=["customer__phone"],
    select_related=["customer"],
    prefetch_related=["invoice_items", "payment_receipts__transaction_record"],
    order_by=["-date_issued", "-id"],
    distinct=True,
    prepare=_attach_settlement,
    priority=10,
    to_hit=lambda invoice: Hit(
        id=invoice.id,
        title=invoice.invoice_number or f"#{invoice.id}",
        subtitle=getattr(invoice.customer, "name", ""),
        amount=invoice.total,
        date=invoice.date_issued,
        status=_status(SALES_STATUS, invoice.display_status),
        link=link("/sales/update-invoice", invoice_id=invoice.id),
    ),
))

register(SearchType(
    key="sales_invoice_item",
    label="Sales invoice items",
    model=SalesInvoiceItem,
    module="sales",
    text_fields=["product__name", "product__base__name", "product__sku", "track_code",
                 "notes", "sales_invoice__invoice_number"],
    number_fields=["sales_invoice"],
    select_related=["product__base", "sales_invoice__customer"],
    order_by=["-sales_invoice__date_issued", "-id"],
    priority=20,
    to_hit=lambda item: Hit(
        id=item.id,
        title=product_label(item.product),
        subtitle=joined(item.sales_invoice.invoice_number or f"#{item.sales_invoice_id}",
                        getattr(item.sales_invoice.customer, "name", "")),
        amount=(item.quantity - (item.returned_quantity or 0)) * (item.unit_price or 0),
        date=item.sales_invoice.date_issued,
        link=link("/sales/update-invoice", invoice_id=item.sales_invoice_id),
    ),
))

# ── Purchase invoices and their items ────────────────────────────────────────

register(SearchType(
    key="purchase_invoice",
    label="Purchase invoices",
    model=PurchaseInvoice,
    module="purchases",
    text_fields=["invoice_number", "supplier__name", "supplier__business_name", "notes",
                 *CREATED_BY, "projects__project__name"],
    number_fields=["id", "total"],
    phone_fields=["supplier__phone"],
    select_related=["supplier"],
    order_by=["-date_issued", "-id"],
    distinct=True,
    priority=30,
    to_hit=lambda invoice: Hit(
        id=invoice.id,
        title=invoice.invoice_number or f"#{invoice.id}",
        subtitle=getattr(invoice.supplier, "name", ""),
        amount=invoice.total,
        date=invoice.date_issued,
        status=_status(PURCHASE_STATUS, invoice.status),
        link=link("/purchases/update-invoice", invoice_id=invoice.id),
    ),
))

register(SearchType(
    key="purchase_invoice_item",
    label="Purchase invoice items",
    model=PurchaseInvoiceItem,
    module="purchases",
    text_fields=["product__name", "product__base__name", "product__sku", "track_code",
                 "notes", "purchase_invoice__invoice_number"],
    number_fields=["purchase_invoice"],
    select_related=["product__base", "purchase_invoice__supplier"],
    order_by=["-purchase_invoice__date_issued", "-id"],
    priority=40,
    to_hit=lambda item: Hit(
        id=item.id,
        title=product_label(item.product),
        subtitle=joined(item.purchase_invoice.invoice_number or f"#{item.purchase_invoice_id}",
                        getattr(item.purchase_invoice.supplier, "name", "")),
        amount=(item.quantity or 0) * (item.unit_cost or 0),
        date=item.purchase_invoice.date_issued,
        link=link("/purchases/update-invoice", invoice_id=item.purchase_invoice_id),
    ),
))

# ── Purchase quotations and their items ──────────────────────────────────────

register(SearchType(
    key="purchase_quotation",
    label="Purchase quotations",
    model=PurchaseQuotation,
    module="quotations",
    text_fields=["quotation_no", "notes", *CREATED_BY, "projects__project__name"],
    number_fields=["id"],
    prefetch_related=["items"],
    order_by=["-created_at", "-id"],
    distinct=True,
    priority=50,
    to_hit=lambda quotation: Hit(
        id=quotation.id,
        title=quotation.quotation_no or f"#{quotation.id}",
        subtitle=f"{len(quotation.items.all())} items",
        date=quotation.created_at,
        status=_status(QUOTATION_STATUS, quotation.status),
        link=link("/purchase-quotations/update-purchase-quotation",
                  purchase_quotation_id=quotation.id),
    ),
))

register(SearchType(
    key="purchase_quotation_item",
    label="Purchase quotation items",
    model=PurchaseQuotationItem,
    module="quotations",
    text_fields=["product__name", "product__base__name", "product__sku", "supplier__name",
                 "track_code", "notes", "purchase_quotation__quotation_no"],
    number_fields=["purchase_quotation"],
    select_related=["product__base", "supplier", "purchase_quotation"],
    order_by=["-created_at", "-id"],
    priority=60,
    to_hit=lambda item: Hit(
        id=item.id,
        title=product_label(item.product),
        subtitle=joined(item.purchase_quotation.quotation_no or f"#{item.purchase_quotation_id}",
                        getattr(item.supplier, "name", "")),
        amount=(item.quantity or 0) * (item.unit_price or 0),
        date=item.created_at,
        status=status("F", "Fulfilled", "success") if item.is_fulfilled else None,
        link=link("/purchase-quotations/update-purchase-quotation",
                  purchase_quotation_id=item.purchase_quotation_id),
    ),
))

# ── Returned items ───────────────────────────────────────────────────────────

register(SearchType(
    key="returned_item",
    label="Returned items",
    model=ReturnedItem,
    module="returned_items",
    text_fields=["invoice_item__product__name", "invoice_item__product__base__name",
                 "reason", "invoice_item__sales_invoice__invoice_number"],
    number_fields=["invoice_item__sales_invoice"],
    choice_fields={"return_type": None},
    select_related=["invoice_item__product__base", "invoice_item__sales_invoice"],
    order_by=["-created_at", "-id"],
    priority=70,
    to_hit=lambda returned: Hit(
        id=returned.id,
        title=product_label(returned.invoice_item.product),
        subtitle=joined(
            returned.invoice_item.sales_invoice.invoice_number
            or f"#{returned.invoice_item.sales_invoice_id}",
            f"x {returned.quantity}",
            returned.get_return_type_display(),
        ),
        date=returned.created_at,
        status=status("DMG", "Damaged", "danger") if returned.is_damaged else None,
        link=link("/returned-items/update-returned-item", returned_item_id=returned.id),
    ),
))

# ── Invoice images (by caption) ──────────────────────────────────────────────


def _visible_images(request, business, access):
    # An image belongs to a sales or a purchase invoice; it is visible only to
    # someone who may see that kind of invoice.
    allowed = Q(pk__in=[])
    if access.can_view_module("sales"):
        allowed |= Q(sales_invoice__business=business)
    if access.can_view_module("purchases"):
        allowed |= Q(purchase_invoice__business=business)
    return InvoiceImage.objects.filter(allowed)


def _image_hit(image):
    invoice = image.sales_invoice or image.purchase_invoice
    number = (invoice.invoice_number or f"#{invoice.id}") if invoice else ""
    path = "/sales/update-invoice" if image.sales_invoice_id else "/purchases/update-invoice"
    return Hit(
        id=image.id,
        title=image.caption or "Image",
        subtitle=joined("Sales invoice" if image.sales_invoice_id else "Purchase invoice", number),
        date=image.uploaded_at,
        link=link(path, invoice_id=invoice.id if invoice else None),
    )


register(SearchType(
    key="invoice_image",
    label="Invoice images",
    model=InvoiceImage,
    queryset=_visible_images,
    text_fields=["caption"],
    select_related=["sales_invoice", "purchase_invoice"],
    order_by=["-uploaded_at", "-id"],
    priority=80,
    to_hit=_image_hit,
))
