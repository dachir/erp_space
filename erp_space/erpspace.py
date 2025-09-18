import frappe
from frappe.utils import nowdate
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
    def on_workflow_action_on_update(doc, method=None):
        # ferme les ToDos de l'ancien état si l'état a changé
        ErpSpace.close_previous_state_todos_on_state_change(doc, method)
        # ferme aussi si Rejected
        ErpSpace.close_todos_on_rejected(doc, method)

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
        if doc.workflow_state not in ["Draft", "Rejected", "Approved", None]:
            # Fetch custom role formula
            formulas = frappe.db.sql(
                """
                SELECT t.custom_role_formula, t.custom_role, t.allowed
                FROM `tabWorkflow Transition` t 
                WHERE t.parent = %s AND t.state = %s AND t.action IN ('Approve')
                """,
                (doc.doctype, doc.workflow_state),
                as_dict=True
            )

            role = None
            if not formulas:
                frappe.log_error(f"No formulas found for workflow state: {doc.workflow_state} of {doc.doctype}", "Workflow Error")
                return

            if len(formulas) >= 1:
                if not formulas[0].get("custom_role_formula"):
                    role = formulas[0].get("allowed") or formulas[0].get("custom_role")
                else:
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
                admin_emails = ""
                if email == "Administrator":
                    admin_emails = "divyesh@marsavco.com; mcoit@marsavco.com"
                try:
                    frappe.share.add_docshare(
                        doc.doctype, doc.name, email, submit=1, flags={"ignore_share_permission": True}
                    )

                    ErpSpace.upsert_single_todo_for_workflow_action(
                        email, doc.doctype, doc.name,
                        state=getattr(doc, "workflow_state", None),
                        action="Approve",                       # ou l'action courante si tu l'as
                        assigned_by=doc.owner
                    )

                    # ✅ correction: on utilise la méthode statique + doc.name
                    ErpSpace.send_email(email if email != "Administrator" else admin_emails, doc.doctype, doc.name)

                    # Si tu veux passer par la méta-fonction de notif complète :
                    # ErpSpace.notify_user_for_workflow(
                    #     user_email=email,
                    #     ref_dt=doc.doctype,
                    #     ref_dn=doc.name,
                    #     action="Approve",
                    #     state=getattr(doc, "workflow_state", None),
                    #     share=True,
                    #     via_in_app=True,
                    #     via_email=True,
                    #     upsert_todo=True,
                    #     assigned_by=doc.owner
                    # )
                except Exception as e:
                    frappe.log_error(str(e), f"Error sharing document: {doc.name}")

            # Process workflow actions
            ErpSpace.custom_process_workflow_actions(doc, doc.workflow_state)
            #ErpSpace.close_previous_state_todos_on_state_change(doc)

    @staticmethod
    def custom_process_workflow_actions(doc, state):
        """Process custom workflow actions."""
        workflow = get_workflow_name(doc.get("doctype"))
        if not workflow:
            return

        next_possible_transitions = get_next_possible_transitions(workflow, get_doc_workflow_state(doc), doc)
        if not next_possible_transitions:
            return

        enqueue(
            send_workflow_action_email,
            queue="short",
            doc=doc,
            transitions=next_possible_transitions,
            enqueue_after_commit=False,
            now=frappe.flags.in_test,
        )

    @staticmethod
    def upsert_single_todo_for_workflow_action(
        user_email: str,
        ref_dt: str,
        ref_dn: str,
        state: str | None = None,
        action: str | None = None,
        assigned_by: str | None = None,
    ):
        """Un seul ToDo par (document, utilisateur).
        - Ouvre/crée le ToDo si l'utilisateur doit agir (état courant)
        - Met à jour description et custom_workflow_state
        - Ferme les doublons éventuels
        """
        try:
            if not user_email:
                return

            # Récupère l'état courant si non fourni
            if state is None and frappe.db.has_column(ref_dt, "workflow_state"):
                state = frappe.db.get_value(ref_dt, ref_dn, "workflow_state")
            state = state or ""

            action = action or "Approve"
            desc = f"[{action}] {ref_dt} {ref_dn} awaits your approval (state: {state})"

            # 1) Existe-t-il déjà un ToDo EXACT pour (doc, user, état) ?
            same_key = frappe.get_all(
                "ToDo",
                filters={
                    "reference_type": ref_dt,
                    "reference_name": ref_dn,
                    "allocated_to": user_email,
                    "custom_workflow_state": state
                },
                fields=["name", "status"],
                order_by="creation asc",
            )
            if same_key:
                name = same_key[0]["name"]
                # si déjà ouvert, on rafraîchit la description
                if same_key[0]["status"] != "Closed":
                    frappe.db.set_value("ToDo", name, {
                        "status": "Open",
                        "description": desc
                    })
                # on ferme les autres ToDos du même user/doc (anciens états ou doublons)
                others = frappe.get_all(
                    "ToDo",
                    filters={
                        "reference_type": ref_dt,
                        "reference_name": ref_dn,
                        "allocated_to": user_email,
                        "name": ["!=", name],
                        "status": ["!=", "Closed"],
                    },
                    pluck="name",
                )
                for nm in others:
                    frappe.db.set_value("ToDo", nm, "status", "Closed")
                return

            # 2) Sinon, on regarde s'il y a des ToDos pour ce user/doc
            existing = frappe.get_all(
                "ToDo",
                filters={
                    "reference_type": ref_dt,
                    "reference_name": ref_dn,
                    "allocated_to": user_email,
                },
                fields=["name", "status", "custom_workflow_state"],
                order_by="creation asc",
            )

            if not existing:
                # créer un ToDo
                todo = frappe.get_doc({
                    "doctype": "ToDo",
                    "description": desc,
                    "allocated_to": user_email,
                    "reference_type": ref_dt,
                    "reference_name": ref_dn,
                    "priority": "Medium",
                    "date": nowdate(),
                    "assigned_by": assigned_by or frappe.session.user,
                    "status": "Open",
                })
                if frappe.db.has_column("ToDo", "custom_workflow_state"):
                    todo.custom_workflow_state = state
                todo.insert(ignore_permissions=True)
            else:
                # mettre à jour le plus ancien comme canon
                name = existing[0]["name"]
                updates = {"description": desc, "status": "Open"}
                if frappe.db.has_column("ToDo", "custom_workflow_state"):
                    updates["custom_workflow_state"] = state
                frappe.db.set_value("ToDo", name, updates)

                # fermer tous les autres
                for t in existing[1:]:
                    if t["status"] != "Closed":
                        frappe.db.set_value("ToDo", t["name"], "status", "Closed")

        except Exception as e:
            frappe.log_error(str(e), "upsert_single_todo_for_workflow_action")

    @staticmethod
    def close_todos_on_rejected(doc, method=None):
        """Ferme tous les ToDos liés dès que le document passe à Rejected.
        Gère soit doc.status == 'Rejected', soit workflow_state == 'Rejected'."""
        try:
            if doc.doctype == "ToDo":
                return

            prev = getattr(doc, "get_doc_before_save", lambda: None)()
            prev_status = getattr(prev, "status", None) if prev else None
            prev_state  = getattr(prev, "workflow_state", None) if prev else None

            new_status = getattr(doc, "status", None)
            new_state  = getattr(doc, "workflow_state", None)

            newly_rejected = (
                (new_status == "Rejected" and prev_status != "Rejected") or
                (new_state  == "Rejected" and prev_state  != "Rejected")
            )
            if not newly_rejected:
                return

            names = frappe.get_all(
                "ToDo",
                filters={
                    "reference_type": doc.doctype,
                    "reference_name": doc.name,
                    "status": ["!=", "Closed"],
                },
                pluck="name",
            )
            if names:
                updates = {"status": "Closed"}
                if frappe.db.has_column("ToDo", "custom_workflow_state"):
                    updates["custom_workflow_state"] = "Rejected"
                for nm in names:
                    frappe.db.set_value("ToDo", nm, updates)

        except Exception as e:
            frappe.log_error(str(e), "close_todos_on_rejected")

    @staticmethod
    def close_previous_state_todos_on_state_change(doc, method=None):
        # ignore ToDo lui-même
        if doc.doctype == "ToDo":
            return

        prev = getattr(doc, "get_doc_before_save", lambda: None)()
        if not prev:
            return

        prev_state = getattr(prev, "workflow_state", None)
        new_state = getattr(doc, "workflow_state", None)

        if prev_state is None or new_state is None or prev_state == new_state:
            return

        # Fermer tous les ToDos ouverts qui étaient sur l'ancien état
        todos = frappe.get_all(
            "ToDo",
            filters={
                "reference_type": doc.doctype,
                "reference_name": doc.name,
                "status": ["!=", "Closed"],
            },
            fields=["name", "custom_workflow_state"],
        )
        for t in todos:
            t_state = t.get("custom_workflow_state") or ""
            if t_state != (new_state or ""):
                frappe.db.set_value("ToDo", t["name"], "status", "Closed")

    @staticmethod
    def close_todos_on_submit(doc, method=None):
        if doc.doctype == "ToDo":
            return
        names = frappe.get_all(
            "ToDo",
            filters={"reference_type": doc.doctype, "reference_name": doc.name, "status": ["!=", "Closed"]},
            pluck="name",
        )
        if names:
            updates = {"status": "Closed"}
            if frappe.db.has_column("ToDo", "custom_workflow_state"):
                updates["custom_workflow_state"] = getattr(doc, "workflow_state", "Submitted")
            for nm in names:
                frappe.db.set_value("ToDo", nm, updates)

    @staticmethod
    def notify_user_for_workflow(
        user_email: str,
        ref_dt: str,
        ref_dn: str,
        *,
        action: str | None = "Approve",
        state: str | None = None,
        subject: str | None = None,
        message_html: str | None = None,
        share: bool = True,
        via_in_app: bool = True,
        via_email: bool = True,
        upsert_todo: bool = True,
        assigned_by: str | None = None,
    ):
        """Envoie une notification complète à un utilisateur pour un document workflow.
        - Partage le document (si share=True)
        - Crée une Notification Log (in-app) anti-doublon
        - Envoie un email (via_email=True)
        - Upsert le ToDo pour ce user (upsert_todo=True)
        """
        try:
            if not user_email:
                return

            # État courant si non fourni
            if state is None:
                try:
                    if frappe.db.has_column(ref_dt, "workflow_state"):
                        state = frappe.db.get_value(ref_dt, ref_dn, "workflow_state")
                except Exception:
                    state = None
            state = state or ""

            link = frappe.utils.get_url_to_form(ref_dt, ref_dn)
            subject = subject or f"[{action or 'Action'}] {ref_dt} {ref_dn} — action requise"
            message_html = message_html or f"""
                <p>Le document <b>{frappe.utils.escape_html(ref_dn)}</b> requiert votre action : <b>{frappe.utils.escape_html(action or 'Action')}</b>.</p>
                <p>État : <b>{frappe.utils.escape_html(state)}</b></p>
                <p><a href="{link}">Ouvrir le document</a></p>
            """

            # 1) Share idempotent
            if share:
                exists = frappe.db.exists("DocShare", {
                    "share_doctype": ref_dt,
                    "share_name": ref_dn,
                    "user": user_email
                })
                if not exists:
                    frappe.share.add_docshare(
                        ref_dt, ref_dn, user_email,
                        submit=1, flags={"ignore_share_permission": True}
                    )

            # 2) Notification in-app (anti-doublon sur 5 minutes)
            if via_in_app:
                dup = frappe.db.sql(
                    """
                    SELECT name FROM `tabNotification Log`
                    WHERE for_user=%s AND document_type=%s AND document_name=%s
                      AND subject=%s AND creation > (NOW() - INTERVAL 5 MINUTE)
                    LIMIT 1
                    """,
                    (user_email, ref_dt, ref_dn, subject),
                    as_dict=True
                )
                if not dup:
                    frappe.get_doc({
                        "doctype": "Notification Log",
                        "subject": subject,
                        "email_content": message_html,
                        "for_user": user_email,
                        "type": "Assignment",
                        "document_type": ref_dt,
                        "document_name": ref_dn,
                    }).insert(ignore_permissions=True)

            # 3) Email (asynchrone, post-commit)
            if via_email:
                frappe.sendmail(
                    recipients=[user_email],
                    subject=subject,
                    message=message_html,
                    delayed=True
                )

            # 4) Upsert du ToDo pour ce user/doc
            if upsert_todo:
                ErpSpace.upsert_single_todo_for_workflow_action(
                    user_email, ref_dt, ref_dn,
                    state=state, action=action, assigned_by=assigned_by or frappe.session.user
                )

        except Exception as e:
            frappe.log_error(str(e), "notify_user_for_workflow")


# Create a global instance of ErpSpace
erpspace = ErpSpace()
