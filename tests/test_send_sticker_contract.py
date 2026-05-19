#!/usr/bin/env python3
"""Contract tests for send-sticker result parsing and CLI validation.

These tests do not touch Chrome or LINE. The live happy-path coverage lives in
test_reply_sticker.py and requires LINE_TEST_ROOM.
"""
import argparse
import unittest

import cli


class SendStickerPointTests(unittest.TestCase):
    def test_parse_ok_point(self):
        ok, detail = cli._parse_pipe_point("OK|123|456")
        self.assertTrue(ok)
        self.assertEqual(detail, {"x": 123, "y": 456})

    def test_parse_error_point(self):
        ok, detail = cli._parse_pipe_point("ERR|package_out_of_range|packages=3")
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "package_out_of_range")
        self.assertEqual(detail["detail"], "packages=3")

    def test_parse_malformed_point(self):
        ok, detail = cli._parse_pipe_point("wat")
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "bad_point_response")


class SendStickerResultTests(unittest.TestCase):
    def parse(self, raw):
        return cli._parse_send_sticker_result(raw, 0, 1)

    def test_success_requires_new_sticker_bubble(self):
        data = self.parse('{"stickerBubbles":2}@@C@@OK|10|20@@K@@{"stickerBubbles":3}')
        self.assertTrue(data["ok"])
        self.assertEqual(data["verified_by"], "sticker_bubble")
        self.assertEqual(data["package"], 0)
        self.assertEqual(data["sticker"], 1)
        self.assertEqual(data["trusted_input"], "core_graphics")
        self.assertEqual(data["sticker_bubbles"], [2, 3])

    def test_no_new_bubble_is_not_confirmed(self):
        data = self.parse('{"stickerBubbles":2}@@C@@OK|10|20@@K@@{"stickerBubbles":2}')
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "not_confirmed")
        self.assertEqual(data["reason"], "no new sticker bubble appeared")

    def test_no_tab_abort(self):
        data = self.parse("ABORT_NO_TAB")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "locate_tab")

    def test_open_button_abort(self):
        data = self.parse("ABORT_OPEN@@ERR|no_sticker_button|")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "open_picker")
        self.assertEqual(data["reason"], "no_sticker_button")

    def test_trusted_click_abort_has_permission_hint(self):
        data = self.parse("ABORT_TRUSTED_CLICK@@Not authorized (-25211)")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "trusted_click")
        self.assertEqual(data["reason"], "trusted_input_unavailable")
        self.assertIn("Accessibility", data["hint"])

    def test_picker_abort(self):
        data = self.parse("ABORT_PICKER@@OK|10|20")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "open_picker")
        self.assertEqual(data["reason"], "picker_unavailable")

    def test_package_out_of_range_abort(self):
        data = self.parse("ABORT_STICKER@@ERR|package_out_of_range|packages=2")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "select_sticker")
        self.assertEqual(data["reason"], "package_out_of_range")
        self.assertEqual(data["detail"]["detail"], "packages=2")

    def test_sticker_out_of_range_abort(self):
        data = self.parse("ABORT_STICKER@@ERR|sticker_out_of_range|stickers=8")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "select_sticker")
        self.assertEqual(data["reason"], "sticker_out_of_range")
        self.assertEqual(data["detail"]["detail"], "stickers=8")

    def test_verify_abort(self):
        data = self.parse('ABORT_VERIFY@@{"stickerBubbles":2}@@K@@{"stickerBubbles":2}')
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "not_confirmed")

    def test_unknown_shape(self):
        data = self.parse("not-a-known-response")
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "unknown")


class SendStickerValidationTests(unittest.TestCase):
    def test_negative_package_rejected_before_chrome_access(self):
        args = argparse.Namespace(to="room", package=-1, sticker=0)
        data = cli.cmd_send_sticker(args, {}, {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "validate")
        self.assertEqual(data["reason"], "negative_index")

    def test_negative_sticker_rejected_before_chrome_access(self):
        args = argparse.Namespace(to="room", package=0, sticker=-1)
        data = cli.cmd_send_sticker(args, {}, {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "validate")
        self.assertEqual(data["reason"], "negative_index")


if __name__ == "__main__":
    unittest.main()
