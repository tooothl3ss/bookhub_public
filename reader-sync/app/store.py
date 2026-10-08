"""Compatibility import; implementation lives in BookHub."""
import sys
from bookhub.sync import store as implementation
sys.modules[__name__] = implementation
