import contextlib
import io
import unittest

from busagent.cli import demo


class DemoTest(unittest.TestCase):
    def test_all_scenarios_pass(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = demo()
        self.assertEqual(code, 0, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
