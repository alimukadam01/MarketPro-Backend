"""Small formatting helpers shared by every app's search types."""


def product_label(variant):
    """"Weather Coat 4L (White)": the base product, then the variant if named."""
    if variant is None:
        return "Product"
    base = getattr(variant, "base", None)
    base_name = getattr(base, "name", "") or ""
    name = variant.name or ""
    if base_name and name and name != base_name:
        return f"{base_name} ({name})"
    return base_name or name or "Product"


def person_name(user):
    if user is None:
        return ""
    full = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return full or user.email or ""


def pkr(amount):
    return f"PKR {float(amount or 0):,.0f}"


def joined(*parts, sep=" · "):
    """Non-empty parts joined: joined("INV-12", None, "Ali") -> "INV-12 · Ali"."""
    return sep.join(str(part) for part in parts if part not in (None, ""))
