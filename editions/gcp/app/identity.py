"""Who is calling, and with which role: an alias of ``editions.shared.identity`` (edition-neutral since the AMD
edition runs the same agent). ``editions.gcp.app.identity`` and ``editions.shared.identity`` are one module object,
so the signing key configured through either name is the key the tools verify."""

import sys

from editions.shared import identity as _shared

sys.modules[__name__] = _shared
