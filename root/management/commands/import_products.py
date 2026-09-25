"""
Imports products and their variants from a normalised spreadsheet.

The xlsx is read with the standard library rather than openpyxl, so the command
adds no dependency to the project.
"""
import re
import zipfile
from xml.etree import ElementTree as ET

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import Inventory, InventoryItem
from root.models import (
    Business, Location, Product, ProductVariant, ProductVariantType, Unit,
)
from root.utils import generate_sku

SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

REQUIRED_COLUMNS = [
    'product_ref', 'product_name', 'product_code', 'product_desc', 'unit',
    'variant_name', 'is_active',
]

# The UI keys a variant's attributes by ProductVariantType.name
# (CreateProduct.tsx uses `[selectedVariantType.name]: value`), so an imported
# variant has to use the same names or it will not render as an attribute.
# Every attr_* column in the sheet needs an entry here, and the named type has
# to exist before the import runs.
ATTRIBUTE_TYPES = {
    'attr_base': 'Base',
    'attr_pack': 'Packaging',
    'attr_model': 'Model',
    'attr_color': 'Color',
    'attr_size': 'Size',
    'attr_power': 'Power',
    'attr_voltage': 'Voltage',
    'attr_capacity': 'Capacity',
    'attr_pv': 'PV Input',
    'attr_current': 'Current Type',
    'attr_rating': 'IP Rating',
    'attr_origin': 'Origin',
}


def _column_index(cell_ref):
    """'BC12' -> zero-based column index."""
    letters = re.match(r"([A-Z]+)", cell_ref).group(1)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def read_sheet(path, sheet_name):
    """Rows of one worksheet as lists of strings, blanks as None."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()

        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{SHEET_NS}si"):
                shared.append("".join(t.text or "" for t in item.iter(f"{SHEET_NS}t")))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {rel.get("Id"): rel.get("Target") for rel in relations}

        target = None
        for sheet in workbook.find(f"{SHEET_NS}sheets"):
            if sheet.get("name") == sheet_name:
                target = targets[sheet.get(f"{REL_NS}id")].lstrip("/")
                break
        if target is None:
            raise CommandError(f"The workbook has no sheet named {sheet_name!r}.")
        if not target.startswith("xl/"):
            target = "xl/" + target

        rows = []
        worksheet = ET.fromstring(archive.read(target))
        for row in worksheet.iter(f"{SHEET_NS}row"):
            cells = {}
            for cell in row.findall(f"{SHEET_NS}c"):
                index = _column_index(cell.get("r"))
                kind = cell.get("t")
                value_node = cell.find(f"{SHEET_NS}v")

                if kind == "s":
                    value = shared[int(value_node.text)] if value_node is not None else None
                elif kind == "inlineStr":
                    inline = cell.find(f"{SHEET_NS}is")
                    value = "".join(t.text or "" for t in inline.iter(f"{SHEET_NS}t")) if inline is not None else None
                else:
                    value = value_node.text if value_node is not None else None

                cells[index] = (value or "").strip() or None

            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(i) for i in range(width)])
        return rows


def as_int(value):
    """Sheet cell to int, or None when the cell is blank."""
    if value in (None, ""):
        return None
    return int(float(value))


def as_float(value):
    """Sheet cell to float, or None when the cell is blank."""
    if value in (None, ""):
        return None
    return float(value)


class _Rollback(Exception):
    """Raised to undo everything after a dry run."""


class Command(BaseCommand):

    help = (
        "Imports products and product variants from a normalised xlsx into one "
        "business.\n\n"
        "The sheet needs one row per variant, with the columns: product_ref, "
        "product_name, product_code, product_desc, unit, variant_name, "
        "attr_base, attr_pack, is_active. Rows are grouped into products by "
        "product_ref.\n\n"
        "Re-running is safe: a product is matched on (business, name) and a "
        "variant on (product, variant_name), so existing rows are left alone "
        "and only what is missing gets created. SKUs come from "
        "root.utils.generate_sku, which advances the business's sku_counter.\n\n"
        "  python manage.py import_products \"Urban Paint Products.xlsx\" "
        "--business-id 1 --dry-run"
    )

    def add_arguments(self, parser):
        parser.add_argument('path', help='Path to the .xlsx file.')
        parser.add_argument(
            '--business-id',
            type=int,
            help='The business the products belong to, by id.',
        )
        parser.add_argument(
            '--business-name',
            help=(
                'The business by name instead of id, for environments where '
                'the id is not known. Combine with --owner-email when more '
                'than one business shares the name.'
            ),
        )
        parser.add_argument(
            '--owner-email',
            help='Narrows --business-name to the business owned by this user.',
        )
        parser.add_argument(
            '--ensure-attribute-types',
            action='store_true',
            help=(
                'Create any ProductVariantType named in ATTRIBUTE_TYPES that '
                'does not exist yet, instead of aborting. Off by default so a '
                'typo cannot quietly add a lookup row.'
            ),
        )
        parser.add_argument(
            '--sheet',
            default='Products',
            help='Worksheet to read. Defaults to "Products".',
        )
        parser.add_argument(
            '--inventory-sheet',
            default='Inventory',
            help=(
                'Worksheet holding opening stock, matched to variants on '
                '(product_ref, variant_name). Skipped when the workbook has no '
                'such sheet. Pass "" to ignore it.'
            ),
        )
        parser.add_argument(
            '--location-id',
            type=int,
            help=(
                'Put every imported inventory item at this location. The sheet '
                'leaves location blank, so without this they have none.'
            ),
        )
        parser.add_argument(
            '--merge-duplicates',
            action='store_true',
            help=(
                'Allow repeated (product_ref, variant_name) rows. The first row '
                'wins and any field it leaves empty is filled from the later '
                'ones. Without this a repeat is an error, so nothing is dropped '
                'without being noticed.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would change, then roll everything back.',
        )

    def collapse_duplicates(self, records, label, merge_allowed):
        """One record per (product_ref, variant_name).

        A repeat is an error unless --merge-duplicates, because silently
        keeping the first and discarding the rest loses data the sheet meant
        to carry. When merging, the first row wins and its blanks are filled
        from the rows that follow, which is the convention the source sheets
        use for duplicates they merged themselves.
        """
        grouped = {}
        for record in records:
            grouped.setdefault(
                (record['product_ref'], record['variant_name']), []).append(record)

        repeats = {key: rows for key, rows in grouped.items() if len(rows) > 1}
        if repeats and not merge_allowed:
            listed = "; ".join(
                f"{ref} / {variant!r} x{len(rows)}" for (ref, variant), rows in repeats.items())
            raise CommandError(
                f"{label} has repeated (product_ref, variant_name) rows: {listed}."
                "\nResolve them in the sheet, or pass --merge-duplicates to keep "
                "the first row and fill its blanks from the others."
            )

        merged = []
        for key, rows in grouped.items():
            record = dict(rows[0])
            for other in rows[1:]:
                for field, value in other.items():
                    if not record.get(field) and value:
                        record[field] = value
            if len(rows) > 1:
                self.stdout.write(self.style.WARNING(
                    f"  merged {len(rows)} {label} rows for {key[0]} / {key[1]!r}"
                ))
            merged.append(record)
        return merged

    def resolve_attribute_types(self, header, ensure_missing):
        """Map each attr_* column to a ProductVariantType name.

        Called inside the import transaction so that a --dry-run which creates
        a missing type rolls that creation back with everything else.

        Order follows the sheet's columns, because the UI renders a variant as
        Object.values(attributes).join(" / ") and that has to match variant_name.
        """
        types = {t.name.lower(): t.name for t in ProductVariantType.objects.all()}
        attribute_map = {}

        for column in [name for name in header if name.startswith('attr_')]:
            expected = ATTRIBUTE_TYPES.get(column)
            if expected is None:
                raise CommandError(
                    f"Column {column!r} has no entry in ATTRIBUTE_TYPES, so there "
                    f"is no variant type to key it by."
                )

            if expected.lower() not in types:
                if not ensure_missing:
                    raise CommandError(
                        f"Column {column!r} maps to the variant type {expected!r}, "
                        f"which does not exist.\nKnown types: "
                        + ", ".join(sorted(types.values()))
                        + "\nRe-run with --ensure-attribute-types to create it."
                    )
                created = ProductVariantType.objects.create(name=expected)
                types[expected.lower()] = created.name
                self.stdout.write(self.style.SUCCESS(
                    f"created variant type {created.name!r} (id {created.id})"
                ))

            attribute_map[column] = types[expected.lower()]

        if attribute_map:
            self.stdout.write(
                "attributes: "
                + ", ".join(f"{c} -> {n}" for c, n in attribute_map.items())
            )
        return attribute_map

    def handle(self, *args, **options):
        path = options['path']
        dry_run = options['dry_run']

        if bool(options['business_id']) == bool(options['business_name']):
            raise CommandError("Give exactly one of --business-id or --business-name.")

        if options['business_id']:
            try:
                business = Business.objects.get(id=options['business_id'])
            except Business.DoesNotExist:
                raise CommandError(f"No business with id {options['business_id']}.")
        else:
            matches = Business.objects.filter(name=options['business_name'])
            if options['owner_email']:
                matches = matches.filter(owner__email__iexact=options['owner_email'])
            found = list(matches.select_related('owner')[:5])

            if not found:
                raise CommandError(
                    f"No business named {options['business_name']!r}"
                    + (f" owned by {options['owner_email']}" if options['owner_email'] else "")
                    + ".\nBusinesses present: "
                    + ", ".join(
                        f"{b.id}:{b.name!r} ({b.owner.email})"
                        for b in Business.objects.select_related('owner')[:20]
                    )
                )
            if len(found) > 1:
                raise CommandError(
                    "That name matches more than one business: "
                    + ", ".join(f"{b.id}:{b.owner.email}" for b in found)
                    + ".\nNarrow it with --owner-email."
                )
            business = found[0]
            self.stdout.write(
                f"resolved business {business.id} ({business.name}) "
                f"owned by {business.owner.email}"
            )

        rows = read_sheet(path, options['sheet'])
        if not rows:
            raise CommandError("The sheet is empty.")

        header = [(cell or "").strip() for cell in rows[0]]
        missing = [name for name in REQUIRED_COLUMNS if name not in header]
        if missing:
            raise CommandError("The sheet is missing columns: " + ", ".join(missing))

        records = []
        for number, row in enumerate(rows[1:], start=2):
            if not any(row):
                continue
            padded = list(row) + [None] * (len(header) - len(row))
            record = dict(zip(header, padded))
            record['_row'] = number
            records.append(record)

        # Units are a global lookup, so resolve every name up front and fail
        # before writing anything if one is unknown.
        unit_names = {(r['unit'] or "").lower() for r in records}
        units = {u.name.lower(): u for u in Unit.objects.all()}
        unknown = sorted(n for n in unit_names if n not in units)
        if unknown:
            raise CommandError(
                "These units are not in the Unit table: "
                + ", ".join(unknown)
                + ".\nKnown units: "
                + ", ".join(sorted(u.name for u in Unit.objects.all()))
            )

        records = self.collapse_duplicates(
            records, options['sheet'], options['merge_duplicates'])

        stock_rows = []
        if options['inventory_sheet']:
            try:
                stock_sheet = read_sheet(path, options['inventory_sheet'])
            except CommandError:
                stock_sheet = []
            if stock_sheet:
                stock_header = [(cell or "").strip() for cell in stock_sheet[0]]
                stock_rows = [
                    dict(zip(stock_header, list(row) + [None] * (len(stock_header) - len(row))))
                    for row in stock_sheet[1:] if any(row)
                ]
                stock_rows = self.collapse_duplicates(
                    stock_rows, options['inventory_sheet'], options['merge_duplicates'])

                known = {(r['product_ref'], r['variant_name']) for r in records}
                orphans = [
                    f"{r['product_ref']} / {r['variant_name']!r}"
                    for r in stock_rows
                    if (r['product_ref'], r['variant_name']) not in known
                ]
                if orphans:
                    raise CommandError(
                        f"{len(orphans)} inventory rows have no matching product row: "
                        + ", ".join(orphans[:5])
                        + ("..." if len(orphans) > 5 else "")
                    )

        location = None
        if options['location_id']:
            try:
                location = Location.objects.get(
                    id=options['location_id'], business=business)
            except Location.DoesNotExist:
                raise CommandError(
                    f"Business {business.id} has no location {options['location_id']}."
                    "\nIts locations: "
                    + ", ".join(
                        f"{loc.id}:{loc.name}"
                        for loc in Location.objects.filter(business=business)
                    )
                )

        inventory = None
        if stock_rows:
            inventory = Inventory.objects.filter(business=business).first()
            if inventory is None:
                raise CommandError(
                    f"Business {business.id} has no inventory row, so stock "
                    f"cannot be imported. It is normally created with the business."
                )

        grouped = {}
        for record in records:
            grouped.setdefault(record['product_ref'], []).append(record)

        self.stdout.write(
            f"{len(records)} variant rows across {len(grouped)} products "
            f"-> business {business.id} ({business.name})"
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("dry run - nothing will be kept"))

        created_products = created_variants = 0
        existing_products = existing_variants = 0
        created_stock = existing_stock = 0
        variant_by_key = {}
        report = []

        try:
            with transaction.atomic():
                attribute_map = self.resolve_attribute_types(
                    header, options['ensure_attribute_types'])

                for ref, variant_rows in grouped.items():
                    first = variant_rows[0]
                    name = first['product_name']

                    product, made = Product.objects.get_or_create(
                        business=business,
                        name=name,
                        defaults={
                            'code': first['product_code'],
                            'desc': first['product_desc'],
                            'unit': units[(first['unit'] or "").lower()],
                            'is_active': first['is_active'] not in ('0', 'false', 'False'),
                        },
                    )
                    if made:
                        created_products += 1
                    else:
                        existing_products += 1

                    made_here = skipped_here = 0
                    for record in variant_rows:
                        attributes = {
                            type_name: record[column]
                            for column, type_name in attribute_map.items()
                            if record[column]
                        }

                        # Deliberately not get_or_create: its `defaults` are
                        # built before the lookup runs, so generate_sku() would
                        # advance the business's sku_counter once per existing
                        # variant every time the import was re-run.
                        variant = ProductVariant.objects.filter(
                            base=product, name=record['variant_name']).first()

                        if variant is None:
                            variant = ProductVariant.objects.create(
                                base=product,
                                name=record['variant_name'],
                                sku=record.get('sku') or generate_sku(business.id),
                                attributes=attributes,
                                is_active=record['is_active'] not in ('0', 'false', 'False'),
                            )
                            made_here += 1
                            created_variants += 1
                        else:
                            skipped_here += 1
                            existing_variants += 1

                        variant_by_key[(ref, record['variant_name'])] = variant

                    report.append(
                        f"  ref {ref:>3}  {'new ' if made else 'kept'}  "
                        f"{name:<30} +{made_here} variants"
                        + (f", {skipped_here} already there" if skipped_here else "")
                    )

                for record in stock_rows:
                    variant = variant_by_key[
                        (record['product_ref'], record['variant_name'])]
                    _, stock_made = InventoryItem.objects.get_or_create(
                        inventory=inventory,
                        product=variant,
                        defaults={
                            'business': business,
                            'location': location,
                            'quantity': as_int(record.get('quantity')) or 0,
                            'quantity_on_hand': as_int(record.get('quantity_on_hand')) or 0,
                            'quantity_reserved': as_int(record.get('quantity_reserved')) or 0,
                            'unit_cost': as_float(record.get('unit_cost')),
                            'unit_price': as_float(record.get('unit_price')),
                            'reorder_level': as_int(record.get('reorder_level')),
                            'track_code': record.get('track_code'),
                            'notes': record.get('notes'),
                        },
                    )
                    if stock_made:
                        created_stock += 1
                    else:
                        existing_stock += 1

                if dry_run:
                    raise _Rollback

        except _Rollback:
            pass

        for line in report:
            self.stdout.write(line)

        self.stdout.write("")
        self.stdout.write(
            f"products:  {created_products} created, {existing_products} already existed"
        )
        self.stdout.write(
            f"variants:  {created_variants} created, {existing_variants} already existed"
        )
        if stock_rows:
            self.stdout.write(
                f"stock:     {created_stock} created, {existing_stock} already existed"
                + (f", at {location.name}" if location else ", no location")
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("rolled back - run again without --dry-run to keep it"))
        else:
            business.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(
                f"done - sku_counter now {business.sku_counter}"
            ))
