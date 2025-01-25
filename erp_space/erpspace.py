import frappe
from frappe.utils.background_jobs import enqueue
from frappe.model.workflow import get_workflow_safe_globals
from frappe.model.workflow import get_workflow_name
from frappe.workflow.doctype.workflow_action.workflow_action import (
    get_next_possible_transitions,
    get_doc_workflow_state,
    send_workflow_action_email
)

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
    def send_email(email, doctype, docname):
        """Send an email notification."""
        frappe.sendmail(
            recipients=email,
            subject=f"{doctype} [{docname}]",
            message=f"""
            <p>The document <b>{docname}</b> requires your approval.</p>
            <p><a href="{frappe.utils.get_url_to_form(doctype, docname)}">View Document</a></p>
            """
        )

    @staticmethod
    def share_doc(doc):
        
        """Share a document with users based on workflow state."""
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

            

            if not formulas:
                frappe.log_error(f"No formulas found for workflow state: {doc.workflow_state} of {doc.doctype}", "Workflow Error")
                return

            # Evaluate the formula with restricted globals
            role = formulas[0].get("custom_role") or frappe.safe_eval(
                formulas[0].get("custom_role_formula", ""),
                get_workflow_safe_globals(), dict(doc=doc.as_dict())
            )

            if not role:
                frappe.log_error(f"Unable to determine role for workflow state: {doc.workflow_state} of {doc.doctype}", "Workflow Error")
                return

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

            # Share the document and send emails
            for email_entry in emails:
                email = email_entry.get("email")
                try:
                    frappe.share.add_docshare(
                        doc.doctype, doc.name, email, submit=1, flags={"ignore_share_permission": True}
                    )
                    #SErpSpace.send_email(email, doc.doctype, doc.name)
                except Exception as e:
                    frappe.log_error(str(e), f"Error sharing document: {doc.name}")

            # Process workflow actions
            
            ErpSpace.custom_process_workflow_actions(doc, doc.workflow_state)

    @staticmethod
    def custom_process_workflow_actions(doc, state):
        """Process custom workflow actions."""
        workflow = get_workflow_name(doc.get("doctype"))
        if not workflow:
            return
        
        next_possible_transitions = get_next_possible_transitions(workflow, get_doc_workflow_state(doc), doc)
        if not next_possible_transitions:
            return

        #frappe.throw(str(next_possible_transitions))
        enqueue(
            send_workflow_action_email,
            queue="short",
            doc=doc,
            transitions=next_possible_transitions,
            enqueue_after_commit=False,
            now=frappe.flags.in_test,
        )
        #send_workflow_action_email(doc, next_possible_transitions)


# Create a global instance of ErpSpace
erpspace = ErpSpace()
