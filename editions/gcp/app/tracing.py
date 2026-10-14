"""``ToolTracePlugin`` and the process-wide trace recorder: an alias of ``editions.shared.tracing`` (one module
object, so both names share one recorder); the AMD edition uses the same plugin."""

import sys

from editions.shared import tracing as _shared

sys.modules[__name__] = _shared
