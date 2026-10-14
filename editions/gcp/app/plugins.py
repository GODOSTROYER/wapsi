"""ADK plugins (guard, audit log, quota notice, memory, voice uploads): an alias of ``editions.shared.plugins``.
The two names are one module object; the AMD edition uses the same plugins."""

import sys

from editions.shared import plugins as _shared

sys.modules[__name__] = _shared
