"""Tests for the M4 output-side guardrails (capabilities/output_guardrails.py)
and their wiring into step5_finalize.py. Pure local Python + cryptography -
no LLM, network, or AWS access needed.

Run with: .venv/Scripts/python.exe -m unittest discover -s tests -p "test_output_guardrails.py" -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.capabilities.metadata import MetadataTracker
from src.capabilities.output_guardrails import (
    check_content_policy,
    decrypt_payload,
    encrypt_payload,
    redact_payload,
    redact_pii,
)
from src.config import PipelineConfig
from src.pipeline.step5_finalize import run_step5_finalize


class RedactPiiTests(unittest.TestCase):
    def test_redacts_email(self):
        text, found = redact_pii("Contact bob@example.com for details.")
        self.assertEqual(found, ["EMAIL"])
        self.assertIn("[REDACTED_EMAIL]", text)
        self.assertNotIn("bob@example.com", text)

    def test_redacts_arn_before_account_id(self):
        text, found = redact_pii("Resource arn:aws:iam::123456789012:role/Admin was modified.")
        self.assertEqual(found, ["ARN"])
        self.assertIn("[REDACTED_ARN]", text)
        self.assertNotIn("123456789012", text)  # consumed by the ARN match, not left behind

    def test_redacts_bare_account_id(self):
        text, found = redact_pii("Account 999999999999 exceeded budget.")
        self.assertEqual(found, ["ACCOUNT_ID"])
        self.assertIn("[REDACTED_ACCOUNT_ID]", text)

    def test_clean_text_untouched(self):
        text, found = redact_pii("EC2 spend rose 12% this week.")
        self.assertEqual(found, [])
        self.assertEqual(text, "EC2 spend rose 12% this week.")


class ContentPolicyTests(unittest.TestCase):
    def test_flags_denylisted_phrase_case_insensitive(self):
        hits = check_content_policy("This report is FOR Internal Use Only.")
        self.assertIn("internal use only", hits)

    def test_no_hits_on_clean_text(self):
        self.assertEqual(check_content_policy("Spend is trending down."), [])

    def test_custom_denylist_overrides_default(self):
        hits = check_content_policy("project codename bluebird", denylist=["codename bluebird"])
        self.assertEqual(hits, ["codename bluebird"])


class RedactPayloadTests(unittest.TestCase):
    def test_walks_nested_structure_and_reports_paths(self):
        payload = {
            "summary": {
                "key_findings": ["Owner bob@example.com approved this.", "Spend rose 10%."],
            },
            "analysis": {"anomalies": [{"finding": "Spike", "evidence": "account 123456789012"}]},
            "count": 5,
            "flag": True,
        }
        redacted, report = redact_payload(payload)

        self.assertIn("[REDACTED_EMAIL]", redacted["summary"]["key_findings"][0])
        self.assertEqual(redacted["summary"]["key_findings"][1], "Spend rose 10%.")
        self.assertIn("[REDACTED_ACCOUNT_ID]", redacted["analysis"]["anomalies"][0]["evidence"])
        # Non-string values pass through untouched.
        self.assertEqual(redacted["count"], 5)
        self.assertIs(redacted["flag"], True)

        self.assertIn("summary.key_findings[0]", report["pii_hits"])
        self.assertEqual(report["pii_hits"]["summary.key_findings[0]"], ["EMAIL"])
        self.assertIn("analysis.anomalies[0].evidence", report["pii_hits"])

    def test_empty_payload_yields_empty_report(self):
        redacted, report = redact_payload({})
        self.assertEqual(redacted, {})
        self.assertEqual(report, {"pii_hits": {}, "policy_hits": {}})


class EncryptDecryptPayloadTests(unittest.TestCase):
    def test_round_trip_and_on_disk_is_not_plaintext(self):
        payload = {"finding": "EC2 spend spiked", "confidence": 0.9}
        with tempfile.TemporaryDirectory() as tmp:
            enc_path = Path(tmp) / "test.json.enc"
            encrypt_payload(payload, enc_path)

            raw_bytes = enc_path.read_bytes()
            self.assertNotIn(b"EC2 spend spiked", raw_bytes)

            decrypted = decrypt_payload(enc_path)
            self.assertEqual(decrypted, payload)


class Step5FinalizeGuardrailIntegrationTests(unittest.TestCase):
    def _cfg(self, tmp_dir: str) -> PipelineConfig:
        cfg = PipelineConfig(user_query="test", sources=["local_csv"])
        cfg.output_folder = Path(tmp_dir)
        cfg.output_folder.mkdir(parents=True, exist_ok=True)
        return cfg

    def test_redacts_pii_and_writes_encrypted_copy_when_enabled(self):
        combined = {
            "business_metadata": {"total_cost_observed": 100.0, "tables": {}},
            "analysis": {"kpis": {}, "anomalies": [], "trends": [], "tag_findings": []},
            "summary": {
                "key_findings": ["Contact jane@example.com about this spike."],
                "recommendations": [], "next_steps": [],
            },
            "charts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            cfg.capabilities.output_guardrails = True
            result = run_step5_finalize(cfg, combined, MetadataTracker())

            self.assertIn("encrypted_path", result)
            self.assertTrue(Path(result["encrypted_path"]).exists())

            json_text = Path(result["json_path"]).read_text(encoding="utf-8")
            self.assertNotIn("jane@example.com", json_text)
            self.assertIn("[REDACTED_EMAIL]", json_text)

            md_text = Path(result["markdown_path"]).read_text(encoding="utf-8")
            self.assertNotIn("jane@example.com", md_text)

            decrypted = decrypt_payload(Path(result["encrypted_path"]))
            self.assertNotIn("jane@example.com", json.dumps(decrypted))

            self.assertIn("summary.key_findings[0]", result["guardrail_report"]["pii_hits"])

    def test_no_encrypted_copy_when_disabled(self):
        combined = {
            "business_metadata": {"total_cost_observed": 0.0, "tables": {}},
            "analysis": {"kpis": {}, "anomalies": [], "trends": [], "tag_findings": []},
            "summary": {"key_findings": [], "recommendations": [], "next_steps": []},
            "charts": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            cfg.capabilities.output_guardrails = False
            result = run_step5_finalize(cfg, combined, MetadataTracker())

            self.assertNotIn("encrypted_path", result)
            self.assertEqual(result["guardrail_report"], {"pii_hits": {}, "policy_hits": {}})


if __name__ == "__main__":
    unittest.main()
