import unittest

from scripts.drain_unread import (
    build_single_round_prompt,
    classify_fatal,
    expand_raw_command,
    extract_json_object,
    normalize_round_result,
)


class DrainUnreadTests(unittest.TestCase):
    def test_expand_raw_command(self) -> None:
        argv = ["--config", "x.toml", "--raw-command", "drain-unread --profile chrome --allow-send"]
        self.assertEqual(
            expand_raw_command(argv),
            ["--config", "x.toml", "drain-unread", "--profile", "chrome", "--allow-send"],
        )

    def test_extract_json_object_strips_code_fence(self) -> None:
        payload = extract_json_object("```json\n{\"action\":\"wait_for_unread\"}\n```")
        self.assertEqual(payload["action"], "wait_for_unread")

    def test_normalize_round_result_fills_defaults(self) -> None:
        payload = normalize_round_result({"action": "processed", "candidate_name": "张三", "sent": "true"})
        self.assertEqual(payload["action"], "processed")
        self.assertEqual(payload["candidate_name"], "张三")
        self.assertTrue(payload["sent"])
        self.assertEqual(payload["resume_download"], "not_applicable")

    def test_classify_fatal_by_step(self) -> None:
        self.assertTrue(classify_fatal({"error_step": "preflight", "error_reason": "wrong_page"}))
        self.assertFalse(classify_fatal({"error_step": "send", "error_reason": "send_failed"}))

    def test_prompt_contains_skip_candidates(self) -> None:
        prompt = build_single_round_prompt("chrome", True, ["张三", "李四"])
        self.assertIn("张三", prompt)
        self.assertIn("allow_send 固定为 true", prompt)


if __name__ == "__main__":
    unittest.main()
