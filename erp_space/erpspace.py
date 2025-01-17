import frappe

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

    @staticmethod
    def share_doc(doc):
        if doc.workflow_state not in ["Draft", "Rejected"]:
            # Fetch custom role formula
            formulas = frappe.db.sql(
                """
                SELECT t.custom_role_formula, t.custom_role
                FROM `tabWorkflow Transition` t 
                WHERE t.parent = %s AND t.state = %s AND t.action = 'Approve'
                """,
                (doc.doctype, doc.workflow_state),
                as_dict=True
            )

            # Evaluate the formula with restricted globals
            role = ""
            if formulas[0].get("custom_role"):
                role = formulas[0].get("custom_role")
            else:
                role = frappe.safe_eval(
                    formulas[0].get("custom_role_formula"),
                    get_workflow_safe_globals(), dict(doc=doc.as_dict())
                )

            # Fetch emails of users with the specified role
            emails = frappe.db.sql(
                """
                SELECT u.name AS email
                FROM `tabUser` u 
                INNER JOIN `tabHas Role` r ON r.parent = u.name
                WHERE r.role = %s
                """,
                (role,),
                as_dict=True
            )

            # Share the document with users
            for email_entry in emails:
                email = email_entry.get("email")
                frappe.share.add_docshare(
                    doc.doctype, doc.name, email, submit=1, flags={"ignore_share_permission": True}
                )

                send_email(email, doc.doctype, doc.name)
            

# Create a global instance of ErpSpace
erpspace = ErpSpace()

