import argparse
import io
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest import mock

from monash_study_kit import cli, edquery
from monash_study_kit.edlib import EdAuthError
from monash_study_kit.moodlelib import MoodleAuthError, MoodleError
from monash_study_kit.output import want_json


class ExitCodeTests(unittest.TestCase):
    """退出码要让调用方分得清需要登录、网络问题、参数错误和找不到。"""

    def run_main(self, exc, argv=("status",)):
        with mock.patch.object(cli, "cmd_status", side_effect=exc), redirect_stderr(io.StringIO()):
            return cli.main(list(argv))

    def test_each_error_maps_to_its_code(self):
        cases = [
            (MoodleAuthError("Moodle 登录已过期"), cli.EXIT_AUTH),
            (EdAuthError("Ed 返回 401"), cli.EXIT_AUTH),
            (edquery.NotFound("没有帖子 FIT2102#999"), cli.EXIT_NOT_FOUND),
            (LookupError("找不到课程 FIT9999"), cli.EXIT_NOT_FOUND),
            (ValueError("看不懂的时间"), cli.EXIT_USAGE),
            (MoodleError("GET /x -> 500"), cli.EXIT_NETWORK),
            (urllib.error.URLError("timed out"), cli.EXIT_NETWORK),
            (ConnectionResetError(), cli.EXIT_NETWORK),
            (KeyboardInterrupt(), cli.EXIT_INTERRUPTED),
        ]
        for exc, code in cases:
            self.assertEqual(self.run_main(exc), code, repr(exc))

    def test_bad_arguments_exit_1_not_2(self):
        # argparse 默认退 2，会被当成“需要登录”
        for argv in (["todo", "--days", "x"], ["nosuchcommand"], ["moodle"], ["ed", "threads", "--json", "--text"],
                     ["due", "--json", "--text"]):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                cli.main(argv)
            self.assertEqual(cm.exception.code, cli.EXIT_USAGE, argv)

    def test_text_flag_beats_pipe(self):
        self.assertFalse(want_json(argparse.Namespace(json=False, text=True)))
        self.assertTrue(want_json(argparse.Namespace(json=True, text=False)))


if __name__ == "__main__":
    unittest.main()


class ConfigTests(unittest.TestCase):
    def test_numbers_can_have_decimals(self):
        self.assertEqual(cli.parse_setting("tz_offset", "9.5", 8), 9.5)     # 阿德莱德
        self.assertEqual(cli.parse_setting("tz_offset", "10", 8), 10)
        self.assertEqual(cli.parse_setting("auto_sync_hours", "0.5", 1), 0.5)

    def test_bad_values_are_rejected_with_a_hint(self):
        with self.assertRaises(ValueError):
            cli.parse_setting("tz_offset", "ten", 8)
        with self.assertRaises(ValueError):
            cli.parse_setting("web_notes", "flase", True)
        self.assertIs(cli.parse_setting("web_notes", "off", True), False)
        self.assertEqual(cli.parse_setting("browser", "C:\\Chrome\\chrome.exe", ""), "C:\\Chrome\\chrome.exe")
