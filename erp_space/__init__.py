__version__ = "0.0.1"

from .erpspace import erpspace

# Register the namespace globally as "erpspace"
globals()['erpspace'] = erpspace
