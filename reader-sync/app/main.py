"""Compatibility import; implementation lives in BookHub."""
import sys
from bookhub.sync import api as implementation
sys.modules[__name__] = implementation
