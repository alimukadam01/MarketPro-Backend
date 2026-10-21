"""
Global search: one box that finds anything in Market Pro.

Each app declares what it makes searchable in its own `search.py`:

    # loyalty/search.py
    from root.search import SearchType, Hit, link, register

    register(SearchType(
        key="reward", label="Rewards", model=Reward, module="loyalty",
        text_fields=["name", "customer__name"], number_fields=["id"],
        to_hit=lambda r: Hit(id=r.id, title=r.name,
                             link=link("/loyalty/update-reward", reward_id=r.id)),
    ))

RootConfig.ready() imports every app's `search.py` (Django's
autodiscover_modules), so a new module is searchable the moment it has one -
nothing here changes. The registry tests in root/tests_search.py run over
every registered type, so a new one is tested without new tests.

Root's own types live in root/search_types.py, because `root.search` is this
package.
"""
from .registry import register, registered
from .types import Hit, SearchType, link, status
from .access import admin_only

__all__ = ["Hit", "SearchType", "admin_only", "link", "register", "registered", "status"]
