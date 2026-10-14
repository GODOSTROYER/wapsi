"""The offline scripted model (``DemoScriptedLlm``): an alias of ``editions.shared.scripted`` (one module object).
The AMD edition's fake OpenAI-compatible endpoint plans with the same functions."""

import sys

from editions.shared import scripted as _shared

sys.modules[__name__] = _shared
