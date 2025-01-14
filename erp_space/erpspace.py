class ErpSpace:
    def __init__(self):
        self.functions = {}

    def register_function(self, name, func):
        """Register a function dynamically in the namespace."""
        self.functions[name] = func

    def call_function(self, name, *args, **kwargs):
        """Call a registered function by name."""
        if name in self.functions:
            return self.functions[name](*args, **kwargs)
        raise Exception(f"Function '{name}' is not registered in erpspace.")

# Create a global instance of ErpSpace
erpspace = ErpSpace()