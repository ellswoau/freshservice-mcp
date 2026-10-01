"""Offline sanity tests for the closure / classification logic.

These do not touch the network. They exercise the schema-driven validation and
proving that a ticket cannot be flipped to Resolved until every
``required_for_closure`` field is populated -- the behaviour that makes
``resolve_ticket`` succeed on the first try.

Run with:

    python -m unittest -v tests_sanity
"""
from __future__ import annotations

import unittest

from freshservice_mcp.config import FreshServiceConfig
from freshservice_mcp.tools import ticket_tools as tt
from freshservice_mcp.tools import conversation_tools as ct
from freshservice_mcp.tools import change_tools as cht


def _fields():
    """A trimmed but representative /ticket_form_fields payload."""
    return [
        {"name": "workspace_id", "required_for_closure": True,
         "choices": [{"id": 2, "value": "Information Technology"}]},
        {"name": "subject", "required_for_closure": True},
        {"name": "status", "required_for_closure": True,
         "choices": [{"id": 2, "value": "Open"}, {"id": 4, "value": "Resolved"}]},
        {"name": "urgency", "required_for_closure": True,
         "choices": [{"id": 1, "value": "Low"}, {"id": 2, "value": "Medium"},
                     {"id": 3, "value": "High"}]},
        {"name": "priority", "required_for_closure": True,
         "choices": [{"id": 1, "value": "Low"}, {"id": 4, "value": "Urgent"}]},
        {"name": "category", "required_for_closure": True,
         "choices": [
             {"id": 100, "value": "User Account", "nested_options": [
                 {"id": 101, "value": "Reset Password", "nested_options": []},
                 {"id": 102, "value": "Account Unlocking", "nested_options": []}]},
             {"id": 200, "value": "Hardware", "nested_options": [
                 {"id": 201, "value": "Laptop", "nested_options": []}]},
         ]},
        {"name": "group", "required_for_closure": True,
         "choices": [{"id": 7, "value": "Help Desk Team"}]},
        {"name": "agent", "required_for_closure": True,
         "choices": [{"id": 42, "value": "Axel W"}]},
        {"name": "impact", "required_for_closure": False,
         "choices": [{"id": 1, "value": "Low"}, {"id": 3, "value": "High"}]},
        {"name": "department", "required_for_closure": False,
         "choices": [{"id": 900, "value": "Machine Shop"}]},
        {"name": "msf_store", "required_for_closure": True,
         "choices": [{"id": 53, "value": "8-Atlanta"}, {"id": 86, "value": "40-ECM"}]},
        {"name": "resolution", "required_for_closure": True, "choices": []},
    ]


def _config():
    return FreshServiceConfig(domain="acme", api_key="k",
                              default_agent_id=42, default_group_id=7,
                              default_workspace_id=2)


class ChoiceValidationTests(unittest.TestCase):
    def test_choice_id_by_name_and_number(self):
        fields = {f["name"]: f for f in _fields()}
        self.assertEqual(tt._choice_id(fields["group"], "Help Desk Team", "group"), 7)
        self.assertEqual(tt._choice_id(fields["group"], "7", "group"), 7)

    def test_choice_id_invalid_lists_valid_values(self):
        fields = {f["name"]: f for f in _fields()}
        with self.assertRaises(ValueError) as ctx:
            tt._choice_id(fields["group"], "Nope Team", "group")
        self.assertIn("Help Desk Team", str(ctx.exception))


class CategoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.fields = _fields()

    def test_valid_category_and_sub_category(self):
        tt._validate_category(self.fields, "User Account", "Reset Password")

    def test_invalid_category(self):
        with self.assertRaises(ValueError):
            tt._validate_category(self.fields, "Nonsense")

    def test_invalid_sub_category_for_category(self):
        with self.assertRaises(ValueError) as ctx:
            tt._validate_category(self.fields, "Hardware", "Reset Password")
        self.assertIn("Reset Password", str(ctx.exception))


class ClosureGapTests(unittest.TestCase):
    def test_missing_fields_reported(self):
        ticket = {"status": 4, "subject": "x", "workspace_id": 2,
                  "urgency": 1, "priority": 1, "category": "User Account",
                  "group_id": None, "responder_id": 42,
                  "custom_fields": {"msf_store": ["8-Atlanta"]}}
        missing = tt._missing_closure_fields(ticket, _fields())
        self.assertEqual(missing, ["group", "resolution"])

    def test_nothing_missing(self):
        ticket = {"status": 4, "subject": "x", "workspace_id": 2,
                  "urgency": 1, "priority": 1, "category": "User Account",
                  "group_id": 7, "responder_id": 42,
                  "custom_fields": {"msf_store": ["8-Atlanta"], "resolution": "done"}}
        self.assertEqual(tt._missing_closure_fields(ticket, _fields()), [])


class ClassificationBodyTests(unittest.TestCase):
    def test_defaults_fill_group_agent_workspace(self):
        body = tt._classification_body(None, _config(), _fields(),
                                       category="User Account",
                                       sub_category="Reset Password",
                                       store=["8-Atlanta"])
        self.assertEqual(body["category"], "User Account")
        self.assertEqual(body["sub_category"], "Reset Password")
        self.assertEqual(body["group_id"], 7)
        self.assertEqual(body["responder_id"], 42)
        self.assertEqual(body["workspace_id"], 2)
        self.assertEqual(body["custom_fields"]["msf_store"], ["8-Atlanta"])

    def test_explicit_values_win_and_are_validated(self):
        body = tt._classification_body(None, _config(), _fields(),
                                       group="Help Desk Team", agent="42",
                                       urgency="high", impact="3")
        self.assertEqual(body["group_id"], 7)
        self.assertEqual(body["responder_id"], 42)
        self.assertEqual(body["urgency"], 3)
        self.assertEqual(body["impact"], 3)

    def test_invalid_store_raises(self):
        with self.assertRaises(ValueError):
            tt._classification_body(None, _config(), _fields(), store=["99-Nowhere"])

    def test_invalid_sub_category_raises(self):
        with self.assertRaises(ValueError):
            tt._classification_body(None, _config(), _fields(),
                                    category="Hardware", sub_category="Reset Password")


class _FakeResp:
    def __init__(self, status=200, payload=b'{"ok": true}'):
        self.status_code = status
        self.content = payload
        self.headers = {"Content-Type": "application/json"}
        self.text = payload.decode() if isinstance(payload, bytes) else str(payload)

    def json(self):
        import json
        return json.loads(self.content.decode() if isinstance(self.content, bytes)
                          else self.content)


class _FakeSession:
    """Captures the multipart/files kwargs a real requests.Session would get."""
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResp()


def _client():
    from freshservice_mcp.client import FreshServiceClient
    c = FreshServiceClient(FreshServiceConfig(domain="acme", api_key="k"))
    c._session = _FakeSession()
    return c


class MultipartUploadTests(unittest.TestCase):
    def test_post_form_files_encodes_file_and_fields(self):
        c = _client()
        files = [("attachments[]", ("a.txt", b"hi", "text/plain"))]
        c.post_form_files("/tickets/1/reply", {"body": "see attached"}, files)
        call = c._session.calls[0]
        sent = call["files"]
        # form field present as a (None, value) tuple
        self.assertIn(("body", (None, "see attached")), sent)
        # upload present as (name, bytes, ctype), never (None, v)
        self.assertIn(("attachments[]", ("a.txt", b"hi", "text/plain")), sent)
        # multipart sets its own content type
        self.assertEqual(call["headers"].get("Content-Type"), None)

    def test_list_form_value_repeats_field(self):
        c = _client()
        c.post_form_files("/tickets/1/reply",
                          {"body": "x", "to_emails[]": ["a@b.c", "d@e.f"]}, [])
        sent = c._session.calls[0]["files"]
        self.assertEqual(sent.count(("to_emails[]", (None, "a@b.c"))), 1)
        self.assertEqual(sent.count(("to_emails[]", (None, "d@e.f"))), 1)

    def test_prep_uploads_reads_local_file(self):
        import os as _os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("hello")
            path = fh.name
        try:
            files, summary = ct._prep_uploads([path])
        finally:
            _os.unlink(path)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0][0], "attachments[]")
        self.assertEqual(files[0][1][1], b"hello")
        self.assertEqual(files[0][1][2], "text/plain")
        self.assertEqual(summary[0]["size"], 5)

    def test_prep_uploads_missing_file_raises(self):
        with self.assertRaises(ValueError):
            ct._prep_uploads(["/no/such/file-xyz.bin"])

    def test_prep_uploads_too_many_raises(self):
        too_many = [f"/tmp/f{i}.txt" for i in range(ct.MAX_UPLOADS_PER_CALL + 1)]
        with self.assertRaises(ValueError):
            ct._prep_uploads(too_many)

    def test_prep_uploads_empty_list_is_noop(self):
        self.assertEqual(ct._prep_uploads(None), ([], []))
        self.assertEqual(ct._prep_uploads([]), ([], []))


class TicketAttachmentTests(unittest.TestCase):
    """Ticket-level attachments (ticket.attachments) must be surfaced even when
    no conversation carries them -- the bug behind "no image was attached"."""

    TICKET = {
        "id": 47480,
        "subject": "Email Overlap",
        "requester": {"name": "Chris Jollie", "email": "cjollie@wellertruck.com"},
        "created_at": "2026-09-30T16:18:20Z",
        "attachments": [{
            "id": 21119235017,
            "name": "EMAIL OVERLAP.jpg",
            "content_type": "image/jpeg",
            "size": 146846,
            "created_at": "2026-09-30T16:18:16Z",
            "attachment_url": "https://x.attachments.freshservice.com/a/b.jpg?Signature=abc",
        }],
    }

    class _FakeClient:
        def __init__(self, ticket, conversations=None):
            self._ticket = ticket
            self._convs = conversations or []
            self.calls = []

        def get_one(self, path, key="ticket", **kw):
            self.calls.append(("get_one", path))
            return self._ticket

        def get_json(self, path, **kw):
            self.calls.append(("get_json", path))
            if path.endswith("/conversations"):
                return {"conversations": self._convs}
            return {}

    def test_ticket_attachment_collected_without_conversations(self):
        c = self._FakeClient(self.TICKET)
        items = ct._collect_attachments(c, 47480)
        self.assertEqual(len(items), 1)
        a = items[0]
        self.assertEqual(a["attachment_id"], 21119235017)
        self.assertEqual(a["scope"], "ticket")
        self.assertEqual(a["alt"], "EMAIL OVERLAP.jpg")
        self.assertTrue(a["url"].endswith("Signature=abc"))
        self.assertEqual(a["conversation_id"], None)

    def test_ticket_attachments_excluded_when_scoped_to_a_conversation(self):
        c = self._FakeClient(self.TICKET, [{"id": 5, "body": "", "attachments": []}])
        items = ct._collect_attachments(c, 47480, conversation_id=5)
        self.assertEqual(items, [])

    def test_summarize_ticket_exposes_attachments(self):
        s = tt.summarize_ticket(self.TICKET)
        self.assertEqual(s["attachment_count"], 1)
        self.assertEqual(s["attachments"][0]["name"], "EMAIL OVERLAP.jpg")
        self.assertEqual(s["attachments"][0]["attachment_id"], 21119235017)

    def test_summarize_ticket_without_attachments_is_empty(self):
        s = tt.summarize_ticket({"id": 1})
        self.assertEqual(s["attachment_count"], 0)
        self.assertEqual(s["attachments"], [])

    # ---- inline screenshots pasted into the ticket *body* --------------
    # FreshService renders a portal/email screenshot as an inline <img> in
    # ``ticket.description`` and does NOT copy it into ``ticket.attachments``;
    # scanning only conversations + attachments missed exactly this case.
    DESC_TICKET = {
        "id": 47504,
        "subject": "Inv 403812577",
        "requester": {"name": "Kari Williams", "email": "kwilliams@wellertruck.com"},
        "created_at": "2026-10-01T11:21:17Z",
        "attachments": [],
        "description": (
            '<div>see the screenshot</div>'
            '<img style="max-width: 1562px;" '
            'src="https://attachment.freshservice.com/inline/attachment?token=eyJx" '
            'class="inline-image" data-id = "21119324543" data-store-type = "1">'
            '<img width="177" height="49" '
            'src="https://attachment.freshservice.com/inline/attachment?token=eyJ5" '
            'class="inline-image" data-id = "21119324542" data-store-type = "1">'
        ),
    }

    def test_inline_description_images_are_collected(self):
        c = self._FakeClient(self.DESC_TICKET)
        items = ct._collect_attachments(c, 47504)
        self.assertEqual(len(items), 2)
        for a in items:
            self.assertEqual(a["scope"], "ticket_description")
            self.assertEqual(a["from_email"], "kwilliams@wellertruck.com")
            self.assertEqual(a["conversation_id"], None)
        # the real screenshot (no width/height attrs) and the logo-preserving one
        ids = {a["attachment_id"] for a in items}
        self.assertEqual(ids, {21119324543, 21119324542})
        sized = {a["attachment_id"]: (a["width"], a["height"]) for a in items}
        self.assertEqual(sized[21119324542], (177, 49))
        self.assertEqual(sized[21119324543], (None, None))

    def test_inline_description_images_excluded_when_scoped_to_a_conversation(self):
        c = self._FakeClient(self.DESC_TICKET, [{"id": 5, "body": "", "attachments": []}])
        items = ct._collect_attachments(c, 47504, conversation_id=5)
        self.assertEqual(items, [])

    def test_summarize_ticket_exposes_inline_description_images(self):
        s = tt.summarize_ticket(self.DESC_TICKET)
        self.assertEqual(s["inline_image_count"], 2)
        self.assertEqual(s["inline_images"][0]["attachment_id"], 21119324543)


class ChangeSummaryTests(unittest.TestCase):
    def test_enum_ids_map_to_names(self):
        c = {"id": 276, "subject": "Std change", "status": 6,
             "change_type": 2, "risk": 4, "impact": 3, "priority": 1}
        s = cht.summarize_change(c)
        self.assertEqual(s["status"], "Closed")
        self.assertEqual(s["change_type"], "Standard")
        self.assertEqual(s["risk"], "Very High")
        self.assertEqual(s["impact"], "High")
        self.assertEqual(s["priority"], "Low")
        self.assertEqual(s["status_id"], 6)

    def test_unknown_enum_is_none_not_crash(self):
        s = cht.summarize_change({"id": 1, "status": 99, "risk": None})
        self.assertIsNone(s["status"])
        self.assertIsNone(s["risk"])

    def test_attachments_and_services_summarised(self):
        s = cht.summarize_change({
            "id": 1,
            "attachments": [{"id": 5, "name": "plan.pdf",
                             "content_type": "application/pdf", "size": 10}],
            "assets": [{"id": 1}, {"id": 2}],
            "impacted_services": ["Email"],
        })
        self.assertEqual(s["attachment_count"], 1)
        self.assertEqual(s["attachments"][0]["name"], "plan.pdf")
        self.assertEqual(s["asset_count"], 2)
        self.assertEqual(s["impacted_services"], ["Email"])

    def test_require_change_id_accepts_bare_and_junk(self):
        self.assertEqual(cht._require_change_id("276"), 276)
        self.assertEqual(cht._require_change_id(276), 276)
        with self.assertRaises(ValueError):
            cht._require_change_id("abc")


if __name__ == "__main__":
    unittest.main()