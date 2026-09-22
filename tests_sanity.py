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


if __name__ == "__main__":
    unittest.main()