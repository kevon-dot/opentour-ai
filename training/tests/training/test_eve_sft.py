from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opentour_training.training import eve_sft


class TextFixtureTokenizer:
    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert not tokenize
        turns = []
        for message in messages:
            role = message["role"]
            turns.append(f"[{role}]{message['content']}")
        if add_generation_prompt:
            turns.append("[assistant]")
        else:
            turns.append("<eos>")
        return "".join(turns)

    def encode(self, text, *, add_special_tokens, truncation):
        assert not add_special_tokens
        assert not truncation
        return list(text.encode())


class EveRehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tokenizer = TextFixtureTokenizer()
        self.messages = [{"role": "user", "content": "What does the snapshot show?"}]
        self.row = {
            "case_ref": "dev-1",
            "family_ref": "dev-family",
            "messages": self.messages,
            "answer": "It reports a clear sky.",
        }
        self.protected = {
            "case_refs": ["held-1"],
            "family_refs": ["held-family"],
            "prompt_digests": [eve_sft.digest([{"role": "user", "content": "held question"}])],
        }

    def test_only_completion_and_eos_are_targets(self):
        (example,) = eve_sft.fit_examples(self.tokenizer, [self.row], self.protected)
        prefix = eve_sft.encode(self.tokenizer, self.messages, generation=True)
        self.assertEqual(example["input_ids"][: len(prefix)], prefix)
        self.assertEqual(example["labels"][: len(prefix)], [-100] * len(prefix))
        self.assertEqual(example["labels"][len(prefix) :], list(b"It reports a clear sky.<eos>"))

    def test_heldout_case_family_and_aliased_prompt_are_denied(self):
        attempts = (
            {"case_ref": "held-1"},
            {"family_ref": "held-family"},
            {"messages": [{"role": "user", "content": "held question"}]},
        )
        for change in attempts:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "protected"):
                eve_sft.fit_examples(self.tokenizer, [self.row | change], self.protected)

    def test_missing_protected_membership_is_denied(self):
        with self.assertRaisesRegex(ValueError, "protected_membership_required"):
            eve_sft.fit_examples(self.tokenizer, [self.row], {})

    def test_overlong_answer_is_rejected_without_truncation(self):
        with self.assertRaisesRegex(ValueError, "exceeds_context"):
            eve_sft.fit_examples(
                self.tokenizer, [self.row | {"answer": "x" * eve_sft.MAX_TOKENS}], self.protected
            )

    def test_invalid_boundary_is_rejected(self):
        with patch.object(eve_sft, "encode", side_effect=[[1, 2], [1, 3, 4]]):
            with self.assertRaisesRegex(ValueError, "unverified_assistant_token_boundary"):
                eve_sft.completion_example(self.tokenizer, self.messages, "answer")

    def test_wrong_checkpoint_and_wrong_architecture_are_denied(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            for name in eve_sft.MODEL_FILES:
                (base / name).write_bytes(b"fixture")
            with self.assertRaisesRegex(ValueError, "checkpoint_digest_mismatch"):
                eve_sft.verify_base(base)
            fixture_sha = eve_sft.file_hashes(base, ("model.safetensors",))["model.safetensors"]
            with patch.object(eve_sft, "BASE_SHA256", fixture_sha):
                (base / "config.json").write_text('{"architectures": ["OtherModel"]}')
                with self.assertRaisesRegex(ValueError, "unexpected_eve_architecture"):
                    eve_sft.verify_base(base)
                config = {"architectures": ["MistralForCausalLM"]}
                (base / "config.json").write_text(json.dumps(config))
                companions = eve_sft.file_hashes(base, tuple(eve_sft.COMPANION_SHA256))
                with patch.object(eve_sft, "COMPANION_SHA256", companions):
                    self.assertEqual(eve_sft.verify_base(base)["model.safetensors"], fixture_sha)
                    for name in companions:
                        with self.subTest(companion=name):
                            original = (base / name).read_bytes()
                            (base / name).write_bytes(original + b" ")
                            with self.assertRaisesRegex(ValueError, "companion_digest_mismatch"):
                                eve_sft.verify_base(base)
                            (base / name).write_bytes(original)
