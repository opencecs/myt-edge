#!/usr/bin/env python3
"""Check the MCU signer settings selected for dual-board and legacy builds."""
import unittest

from board_config import board_config


class BoardConfigTest(unittest.TestCase):
    def test_sequence_override_preserves_shared_signer(self):
        shared = {"private_key": "sign.key", "public_key": "sign.pub", "sequence": 6,
                  "host_min": "0.1.0"}
        config = {"board": {"signing": shared, "host_version": "0.1.0"},
                  "boards": {"Y4": {"board": {"signing": {"sequence": 9}}}, "Y12": {}}}
        self.assertEqual(board_config(config, "Y4")["board"],
                         {"host_version": "0.1.0", "signing": {**shared, "sequence": 9}})
        self.assertEqual(board_config(config, "Y12")["board"]["signing"], shared)
        self.assertEqual(shared["sequence"], 6)

    def test_explicit_empty_signer_stays_disabled(self):
        config = {"board": {"signing": {"public_key": "sign.pub"}},
                  "boards": {"Y4": {"board": {"signing": {}}}}}
        self.assertEqual(board_config(config, "Y4")["board"]["signing"], {})

    def test_separate_signer_keeps_its_own_compatibility_policy(self):
        signer = {"private_key": "board.key", "public_key": "board.pub", "sequence": 10}
        config = {"board": {"signing": {"host_min": "1.0.0"}},
                  "boards": {"Y4": {"board": {"signing": signer}}}}
        self.assertEqual(board_config(config, "Y4")["board"]["signing"], signer)

    def test_legacy_configuration(self):
        config = {"hardware": "Y4-6187", "board": {"signing": {"sequence": 6}}}
        self.assertEqual(board_config(config, None), config)


if __name__ == "__main__":
    unittest.main()
