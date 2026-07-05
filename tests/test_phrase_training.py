"""Tests for whisper_sync.phrase_training - step 5 PR A pure logic.

The trainer itself (torch, openwakeword.train, datasets) never runs
here; these pin the CI-safe parts: name sanitization, config content
and serialization, and the three-stage command construction.
"""

import json
import tempfile
import unittest
from pathlib import Path

from whisper_sync.phrase_training import (
    build_training_config, write_training_config, training_commands,
    trained_model_path, sanitize_model_name,
    ACAV_FEATURES, VALIDATION_FEATURES,
)


class SanitizeNameTests(unittest.TestCase):
    def test_phrase_becomes_snake_case(self):
        self.assertEqual(sanitize_model_name("Hey Hal!"), "hey_hal")
        self.assertEqual(sanitize_model_name("that's all"), "that_s_all")

    def test_unusable_phrase_raises(self):
        with self.assertRaises(ValueError):
            sanitize_model_name("!!!")


class BuildConfigTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/ws")
        self.config = build_training_config("Hey Hal", self.root)

    def test_notebook_values_present(self):
        self.assertEqual(self.config["target_phrase"], ["hey hal"])
        self.assertEqual(self.config["model_name"], "hey_hal")
        self.assertEqual(self.config["n_samples"], 1000)
        self.assertEqual(self.config["steps"], 10000)

    def test_paths_land_under_the_workspace_root(self):
        root = str(self.root)
        self.assertTrue(self.config["output_dir"].startswith(root))
        self.assertTrue(
            self.config["piper_sample_generator_path"].startswith(root))
        for p in (self.config["rir_paths"]
                  + self.config["background_paths"]):
            self.assertTrue(p.startswith(root))
        self.assertIn(ACAV_FEATURES,
                      self.config["feature_data_files"]["ACAV100M_sample"])
        self.assertIn(VALIDATION_FEATURES,
                      self.config["false_positive_validation_data_path"])

    def test_overrides_win(self):
        config = build_training_config("hey hal", self.root,
                                       {"steps": 42, "n_samples": 7})
        self.assertEqual(config["steps"], 42)
        self.assertEqual(config["n_samples"], 7)

    def test_trained_model_path_matches_trainer_output(self):
        path = trained_model_path(self.config)
        self.assertEqual(path.name, "hey_hal.onnx")
        self.assertEqual(str(path.parent),
                         str(Path(self.config["output_dir"])))


class WriteConfigTests(unittest.TestCase):
    def test_written_file_is_json_and_therefore_yaml(self):
        # The trainer loads the config with yaml.load; JSON is a strict
        # subset of YAML 1.2, so a JSON file needs no yaml dep here.
        config = build_training_config("hey hal", "C:/ws")
        with tempfile.TemporaryDirectory() as td:
            path = write_training_config(config, Path(td) / "x" / "c.yml")
            loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, config)


class CommandTests(unittest.TestCase):
    def test_three_official_stages_in_order(self):
        cmds = training_commands("C:/ws/c.yml", "C:/ws/py.exe")
        self.assertEqual([c[-1] for c in cmds],
                         ["--generate_clips", "--augment_clips",
                          "--train_model"])
        for cmd in cmds:
            self.assertEqual(cmd[0], "C:/ws/py.exe")
            self.assertIn("openwakeword.train", cmd)
            self.assertIn("C:/ws/c.yml", cmd)


if __name__ == "__main__":
    unittest.main()
