import unittest

from network_fw2bids.rules import (
    AcquisitionRule,
    map_acquisition,
    normalize_label,
    relevant_subject_labels,
)


class TestRules(unittest.TestCase):
    def test_maps_functional_acquisition(self) -> None:
        self.assertEqual(
            map_acquisition("task-flanker_bold"),
            AcquisitionRule(modality="func", suffix="bold", task="flanker"),
        )

    def test_maps_diffusion_acquisition(self) -> None:
        self.assertEqual(
            map_acquisition("DTI_pe0_g105"),
            AcquisitionRule(
                modality="dwi", suffix="dwi", acquisition="g105", direction="AP"
            ),
        )

    def test_skips_localizer_and_rejected_scan(self) -> None:
        self.assertIsNone(map_acquisition("3Plane Loc SSFSE"))
        self.assertIsNone(map_acquisition("task-flanker_bold_qa-reject"))

    def test_finds_aliases_and_reassignment_sources(self) -> None:
        self.assertEqual(relevant_subject_labels("s10"), {"s10", "s03"})
        self.assertEqual(normalize_label("ses-01"), "01")
