__version__ = "0.0.1"

import frappe
from .erpspace import erpspace

# Register the namespace globally as "erpspace"
globals()['erpspace'] = erpspace

# Register `erpspace` under `frappe` namespace
frappe.erpspace = erpspace
