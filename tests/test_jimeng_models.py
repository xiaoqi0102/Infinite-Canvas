import unittest
from types import SimpleNamespace

import main


class JimengVideoModelTests(unittest.TestCase):
    def test_seedance_mini_is_forwarded_to_cli(self):
        payload = SimpleNamespace(model="seedance2.0mini", resolution="4k")
        args = []

        main.jimeng_append_model_resolution_args(args, payload, include_model=True)

        self.assertEqual(
            args,
            ["--model_version=seedance2.0mini", "--video_resolution=720p"],
        )


if __name__ == "__main__":
    unittest.main()
