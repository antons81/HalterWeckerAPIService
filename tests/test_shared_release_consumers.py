"""Consumer preflight must fail before any production pointer mutation."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validate_shared_release_consumers import image_supported_providers, preflight, provider_ids
from services.release_activation_requirements import ActivationRequirementError


class ConsumerPreflightTests(unittest.TestCase):
    def test_provider_list_normalizes_whitespace_and_duplicates(self):
        self.assertEqual(provider_ids(" israel-mot, germany,israel-mot, "), ("israel-mot", "germany"))

    def test_image_capability_probe_uses_exact_image_without_network(self):
        with patch("scripts.validate_shared_release_consumers.docker_json", return_value=["israel-mot"]) as call:
            supported = image_supported_providers("sha256:current", ("israel-mot", "germany"))
        self.assertEqual(supported, ("israel-mot",))
        arguments = call.call_args.args
        self.assertIn("sha256:current", arguments)
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn("r.provider_capability", arguments[-2])

    def test_unsupported_runtime_fails_before_starting_consumers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "release.json").write_text(json.dumps({"providers": {"germany": {}, "israel-mot": {}}}))
            with patch("scripts.validate_shared_release_consumers.docker_json", return_value=[{"Image": "current"}]), \
                 patch("scripts.validate_shared_release_consumers.image_supported_providers", return_value=("israel-mot",)), \
                 patch("scripts.validate_shared_release_consumers.validate_shared_release") as validation, \
                 patch("scripts.validate_shared_release_consumers.subprocess.run") as containers:
                with self.assertRaisesRegex(ActivationRequirementError, "unsupported runtime providers: germany"):
                    preflight(root, ("germany", "israel-mot"), ("germany",), "static", "route")
                validation.assert_not_called()
                containers.assert_not_called()

    def test_consumer_preflight_precedes_pointer_switch(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/run_stop_data_pipeline.sh").read_text()
        activation = script.split("activate_incremental_production() {", 1)[1]
        self.assertLess(activation.index("validate_incremental_consumers || return 1"),
                        activation.index('replace_link "$CURRENT_RELEASE" "$candidate_target"'))


if __name__ == "__main__":
    unittest.main()
