#!/usr/bin/env python3
"""Contract tests for local productivity features.

These tests exercise only local config and message-analysis logic. They do not
touch Chrome or LINE.
"""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cli


class AllowedRoomTests(unittest.TestCase):
    def test_allowed_room_blocks_send_before_chrome_access(self):
        with tempfile.TemporaryDirectory() as td:
            allowed_path = Path(td) / "allowed-rooms.json"
            allowed_path.write_text(
                '{"version":1,"enabled":true,"rooms":["나만의 그룹"]}',
                encoding="utf-8",
            )
            args = argparse.Namespace(
                to="Other Room",
                text="hello",
                profile=None,
                no_profile=False,
                follow_up_at=None,
                follow_up_in=None,
                follow_up_note=None,
            )
            with mock.patch.object(cli, "USER_ALLOWED_ROOMS_PATH", allowed_path), \
                    mock.patch.object(cli, "_require_tab", side_effect=AssertionError("Chrome touched")):
                data = cli.cmd_send(args, {}, {})
        self.assertFalse(data["ok"])
        self.assertEqual(data["stage"], "allowed_rooms")
        self.assertEqual(data["reason"], "room_not_allowed")


class ToneProfileTests(unittest.TestCase):
    def test_room_profile_applies_prefix_and_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            profile_path = Path(td) / "tone-profiles.json"
            profile_path.write_text(
                '{"version":1,"profiles":{"polite":{"prefix":"안녕하세요. ","suffix":" 감사합니다."}},'
                '"rooms":{"Team":"polite"}}',
                encoding="utf-8",
            )
            with mock.patch.object(cli, "USER_TONE_PROFILES_PATH", profile_path):
                data = cli._profile_message_text("Team", "확인했습니다")
        self.assertEqual(data["profile"], "polite")
        self.assertEqual(data["text"], "안녕하세요. 확인했습니다 감사합니다.")


class MessageAnalysisTests(unittest.TestCase):
    def test_needs_reply_only_after_last_sent_by_default(self):
        messages = [
            {"direction": "received", "text": "오늘 가능할까요?"},
            {"direction": "sent", "text": "네 확인했습니다"},
            {"direction": "received", "text": "자료도 보내줄 수 있나요?"},
            {"direction": "received", "text": "감사합니다"},
        ]
        candidates = cli._find_needs_reply(messages)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["text"], "자료도 보내줄 수 있나요?")

    def test_brief_counts_requests_and_candidates(self):
        messages = [
            {"direction": "sent", "text": "안녕하세요"},
            {"direction": "received", "text": "내일 회의 가능할까요?"},
        ]
        brief = cli._brief_from_messages("Team", messages, preview=2)
        self.assertEqual(brief["message_count"], 2)
        self.assertEqual(brief["question_or_request_count"], 1)
        self.assertEqual(brief["needs_reply_count"], 1)
        self.assertIn("summary", brief)
        self.assertIn("최근 대화", brief["summary_text"])
        self.assertEqual(brief["summary"]["recent_flow"][-1]["speaker"], "상대")
        self.assertEqual(brief["summary"]["open_questions"][0]["text"], "내일 회의 가능할까요?")

    def test_brief_without_room_uses_current_history(self):
        args = argparse.Namespace(
            room=[],
            rooms=None,
            limit_rooms=10,
            limit=10,
            max_runtime_ms=900,
            preview=0,
            include_messages=False,
        )
        history = {
            "header": "Current Room",
            "messages": [{"direction": "received", "text": "확인 가능할까요?"}],
        }
        with mock.patch.object(cli, "_require_tab", return_value={"url": "chrome-extension://x/index.html#/chats/1"}), \
                mock.patch.object(cli, "_read_current_history", return_value=history), \
                mock.patch.object(cli, "_default_visible_rooms", side_effect=AssertionError("slow room list used")):
            data = cli.cmd_brief(args, {}, {})
        self.assertFalse(data["partial"])
        self.assertEqual(data["rooms"], ["Current Room"])
        self.assertEqual(data["items"][0]["needs_reply_count"], 1)

    def test_needs_reply_without_room_uses_current_history(self):
        args = argparse.Namespace(
            room=[],
            rooms=None,
            limit_rooms=10,
            limit=10,
            max_runtime_ms=900,
            include_before_last_sent=False,
        )
        history = {
            "header": "Current Room",
            "messages": [{"direction": "received", "text": "자료 보내줄 수 있나요?"}],
        }
        with mock.patch.object(cli, "_require_tab", return_value={"url": "chrome-extension://x/index.html#/chats/1"}), \
                mock.patch.object(cli, "_read_current_history", return_value=history), \
                mock.patch.object(cli, "_default_visible_rooms", side_effect=AssertionError("slow room list used")):
            data = cli.cmd_needs_reply(args, {}, {})
        self.assertFalse(data["partial"])
        self.assertEqual(data["rooms"][0]["room"], "Current Room")
        self.assertEqual(data["candidate_count"], 1)


class FollowUpTests(unittest.TestCase):
    def test_add_due_and_done_followup(self):
        with tempfile.TemporaryDirectory() as td:
            follow_path = Path(td) / "follow-ups.json"
            with mock.patch.object(cli, "USER_FOLLOW_UPS_PATH", follow_path):
                add_args = argparse.Namespace(
                    action="add",
                    item_id=None,
                    room="Team",
                    text="check response",
                    at="2000-01-01T00:00",
                    in_=None,
                    all=False,
                )
                added = cli.cmd_follow_ups(add_args, {}, {})
                item_id = added["item"]["id"]

                due_args = argparse.Namespace(
                    action="due", item_id=None, room=None, text=None, at=None, in_=None, all=False
                )
                due = cli.cmd_follow_ups(due_args, {}, {})
                self.assertEqual(due["due_count"], 1)

                done_args = argparse.Namespace(
                    action="done", item_id=item_id, room=None, text=None, at=None, in_=None, all=False
                )
                done = cli.cmd_follow_ups(done_args, {}, {})
                self.assertTrue(done["ok"])


class ScheduleTests(unittest.TestCase):
    def test_add_and_dry_run_scheduled_send(self):
        with tempfile.TemporaryDirectory() as td:
            schedule_path = Path(td) / "scheduled-sends.json"
            allowed_path = Path(td) / "allowed-rooms.json"
            with mock.patch.object(cli, "USER_SCHEDULE_PATH", schedule_path), \
                    mock.patch.object(cli, "USER_ALLOWED_ROOMS_PATH", allowed_path):
                add_args = argparse.Namespace(
                    action="add",
                    item_id=None,
                    to="Team",
                    text="standup reminder",
                    at="2000-01-01T09:00",
                    in_=None,
                    repeat="none",
                    profile=None,
                    no_profile=False,
                    meaning=None,
                    package=None,
                    sticker=None,
                    all=False,
                    dry_run=False,
                )
                added = cli.cmd_schedule(add_args, {}, {})
                self.assertTrue(added["ok"])

                run_args = argparse.Namespace(
                    action="run",
                    item_id=None,
                    to=None,
                    text=None,
                    at=None,
                    in_=None,
                    repeat="none",
                    profile=None,
                    no_profile=False,
                    meaning=None,
                    package=None,
                    sticker=None,
                    all=False,
                    dry_run=True,
                )
                due = cli.cmd_schedule(run_args, {}, {})
        self.assertEqual(due["due_count"], 1)


if __name__ == "__main__":
    unittest.main()
