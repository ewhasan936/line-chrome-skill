#!/usr/bin/env python3
"""Contract tests for send-sticker result parsing and CLI validation.

These tests do not touch Chrome or LINE. The live happy-path coverage lives in
test_reply_sticker.py and requires LINE_TEST_ROOM.
"""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_meaning_not_mapped_rejected_before_chrome_access(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stickers.json"
            args = argparse.Namespace(to="room", package=None, sticker=None, meaning="thanks")
            with mock.patch.object(cli, "USER_STICKER_TAGS_PATH", path), \
                    mock.patch.object(cli, "_require_tab", side_effect=AssertionError("Chrome touched")):
                data = cli.cmd_send_sticker(args, {}, {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "validate")
        self.assertEqual(data["reason"], "meaning_not_mapped")

    def test_meaning_conflicts_with_explicit_index(self):
        args = argparse.Namespace(to="room", package=0, sticker=None, meaning="thanks")
        data = cli.cmd_send_sticker(args, {}, {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "validate")
        self.assertEqual(data["reason"], "meaning_conflicts_with_index")

    def test_meaning_resolves_to_sticker_indexes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stickers.json"
            path.write_text(
                '{"version":1,"tags":{"thanks":{"package":2,"sticker":3,"label":"Thanks"}}}',
                encoding="utf-8",
            )
            args = argparse.Namespace(to="room", package=None, sticker=None, meaning="thanks")

            def fake_send(sel, loc, package_idx, sticker_idx):
                return {
                    "ok": True,
                    "verified_by": "sticker_bubble",
                    "package": package_idx,
                    "sticker": sticker_idx,
                    "trusted_input": "core_graphics",
                    "sticker_bubbles": [1, 2],
                }

            with mock.patch.object(cli, "USER_STICKER_TAGS_PATH", path), \
                    mock.patch.object(cli, "_require_tab", return_value={"url": "chrome-extension://x/index.html#/chats"}), \
                    mock.patch.object(cli, "_open_visible_room_fast", return_value={"ok": True}), \
                    mock.patch.object(cli, "_do_send_sticker", side_effect=fake_send):
                data = cli.cmd_send_sticker(args, {}, {})
        self.assertTrue(data["ok"])
        self.assertEqual(data["package"], 2)
        self.assertEqual(data["sticker"], 3)
        self.assertEqual(data["meaning"], "thanks")
        self.assertEqual(data["resolved_tag"], "thanks")
        self.assertEqual(data["sticker_label"], "Thanks")


class StickerTagCommandTests(unittest.TestCase):
    def test_set_show_resolve_and_remove_tag(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stickers.json"
            with mock.patch.object(cli, "USER_STICKER_TAGS_PATH", path):
                set_args = argparse.Namespace(
                    action="set", tag="Thanks", package=1, sticker=4, label="polite thanks"
                )
                set_data = cli.cmd_sticker_tags(set_args, {}, {})
                self.assertTrue(set_data["ok"])

                resolved = cli._resolve_sticker_meaning("thanks")
                self.assertEqual(resolved["package"], 1)
                self.assertEqual(resolved["sticker"], 4)
                self.assertEqual(resolved["label"], "polite thanks")

                show_args = argparse.Namespace(
                    action="show", tag=None, package=None, sticker=None, label=None
                )
                show_data = cli.cmd_sticker_tags(show_args, {}, {})
                self.assertIn("Thanks", show_data["tags"])

                remove_args = argparse.Namespace(
                    action="remove", tag="thanks", package=None, sticker=None, label=None
                )
                remove_data = cli.cmd_sticker_tags(remove_args, {}, {})
                self.assertTrue(remove_data["ok"])
                self.assertIsNone(cli._resolve_sticker_meaning("thanks"))


if __name__ == "__main__":
    unittest.main()
