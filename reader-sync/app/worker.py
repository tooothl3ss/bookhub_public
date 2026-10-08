"""Compatibility import; implementation lives in BookHub."""
import sys
from bookhub.sync import jobs as implementation
sys.modules[__name__] = implementation
