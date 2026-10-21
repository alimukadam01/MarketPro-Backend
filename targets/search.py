"""What the targets app makes searchable: targets and manual data points.

Both follow has_targets_access - the paid add-on for admins, the targets
permission for employees.
"""
from root.search import Hit, SearchType, link, register
from root.search.labels import joined

from .catalogue import data_point_choices
from .models import ManualDataPoint, Target
from .utils import has_targets_access


def _targets(request, business, access):
    return has_targets_access(request, business)


def _data_point_label(code):
    return dict(data_point_choices()).get(code, code)


register(SearchType(
    key="target",
    label="Targets",
    model=Target,
    access=_targets,
    text_fields=["name", "notes", "outcome", "subject_label"],
    number_fields=["id"],
    # Data points have no model choices; their labels come from the catalogue.
    choice_fields={"data_point": data_point_choices, "period_type": None},
    order_by=["-created_at", "-id"],
    priority=230,
    to_hit=lambda target: Hit(
        id=target.id,
        title=target.name,
        subtitle=joined(_data_point_label(target.data_point), target.get_period_type_display()),
        amount=target.target_value,
        link=link("/targets/update-target", target_id=target.id),
    ),
))

register(SearchType(
    key="manual_data_point",
    label="Manual data points",
    model=ManualDataPoint,
    access=_targets,
    text_fields=["name", "notes"],
    number_fields=["id"],
    order_by=["-as_of_date", "-id"],
    priority=240,
    to_hit=lambda point: Hit(
        id=point.id,
        title=point.name,
        subtitle=joined("As of", point.as_of_date.strftime("%d/%m/%Y"), sep=" ") if point.as_of_date else "",
        amount=point.value if point.value_type == "amount" else None,
        date=point.as_of_date,
        link=link("/targets"),
    ),
))
