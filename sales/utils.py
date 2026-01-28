from django.db import transaction
from typing import Dict, Set, Tuple
from django.db.models import QuerySet
from django.db.models import Sum
from rest_framework.exceptions import ValidationError
from .models import PurchaseInvoiceItem, PurchaseInvoiceItemRestock, SalesInvoice, SalesInvoiceItem, SalesInvoiceItemDeduction
from inventory.models import InventoryItem
from .models import PurchaseInvoice
from .models import PurchaseInvoiceItemRestock
from root.utils import generateTransactionId


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
    if instance.is_deducted: return

    if instance.status in ('C', 'PC'):

        if instance.status == 'C' and not instance.is_deducted:
            quantity_field = getRestockField(False)
        else:
            quantity_field = getRestockField(True)

        invoice_items_dict = instance.map_to_products()
        product_ids = set(invoice_items_dict)
        existing, _, new_ids = spiltNewAndOldProducts(product_ids, instance.business.id)

        for inventory_item in existing:
            item = invoice_items_dict[inventory_item.product_id]
            delta = item.compute_restock_delta()
            if delta > 0:
                inventory_item.apply_restock_delta(True, delta, generateTransactionId(instance))
                logRestockEvent(item, delta, instance)
                item.update_restock_flags()

        instance.update_deduction_flags()
        instance.save()
