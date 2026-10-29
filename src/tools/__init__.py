"""Tool implementation package, split by domain (slice 1, #4082/#4071).

Public tool functions live in domain modules. ``src.tool_implementations``
re-exports from here for backward compatibility.
"""
from src.tools._common import _parse_tool_args  # noqa: F401
from src.tools.system import (  # noqa: F401
    do_manage_skills, _skill_dump, do_manage_tasks,
    do_api_call, do_app_api,
)
from src.tools.search import do_search_chats  # noqa: F401
from src.tools.notes import do_manage_notes  # noqa: F401
from src.tools.calendar import do_manage_calendar  # noqa: F401
from src.tools.research import do_manage_research, do_trigger_research  # noqa: F401
from src.tools.contacts import do_resolve_contact, do_manage_contact  # noqa: F401
from src.tools.vault import (  # noqa: F401
    _load_vault_config, _run_bw,
    do_vault_search, do_vault_get, do_vault_unlock,
)
