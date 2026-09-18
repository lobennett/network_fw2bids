from dataclasses import dataclass
import re


FUNCTIONAL = re.compile(r"^task-(?P<task>[A-Za-z0-9]+)_bold(?:_\d+|_run_\d+)?$")
TASKS = {
    "rest",
    "cuedTS",
    "spatialTS",
    "directedForgetting",
    "flanker",
    "goNogo",
    "nBack",
    "shapeMatching",
    "stopSignal",
    "cuedTSWFlanker",
    "directedForgettingWCuedTS",
    "directedForgettingWFlanker",
    "flankerWShapeMatching",
    "nBackWShapeMatching",
    "nBackWSpatialTS",
    "shapeMatchingWCuedTS",
    "spatialTSWCuedTS",
    "spatialTSWShapeMatching",
    "stopSignalWDirectedForgetting",
    "stopSignalWFlanker",
}


@dataclass(frozen=True)
class AcquisitionRule:
    modality: str
    suffix: str | None = None
    task: str | None = None
    acquisition: str | None = None
    direction: str | None = None


NON_FUNCTIONAL = {
    "NEW Sag_MPRAGE_T1": AcquisitionRule(
        modality="anat", suffix="T1w", acquisition="SagMPRAGE"
    ),
    "T2w CUBE PROMO .8mm sag": AcquisitionRule(
        modality="anat", suffix="T2w", acquisition="CubePromo"
    ),
    "DTI_pe0_g105": AcquisitionRule(
        modality="dwi", suffix="dwi", acquisition="g105", direction="AP"
    ),
    "DTI_pe1_g105": AcquisitionRule(
        modality="dwi", suffix="dwi", acquisition="g105", direction="PA"
    ),
    "DTI_pe1_g71": AcquisitionRule(
        modality="dwi", suffix="dwi", acquisition="g71", direction="PA"
    ),
    "fmap-fieldmap": AcquisitionRule(modality="fmap"),
}

SKIP_ACQUISITIONS = {
    "3Plane Loc SSFSE",
    "3Plane Loc SSFSE_1",
    "GE HOS FOV28",
    "GE HOS FOV28_1",
    "GE HOS FOV28_2",
    "GE HOS FOV28_3",
    "GE HOS FOV28_4",
    "HO Shim",
    "Processed Images",
    "Processed Images_1",
    "run-1_sbref",
    "fmap-fieldmap_1",
    "T1w MPRAGE PROMO",
}

SUBJECT_ALIASES = {
    "s19-2": "s19",
    "s29-2": "s29",
    "s43-2": "s43",
    "ex26207": "s297",
}
SESSION_OVERRIDES = {
    "s03": {"22752": {"reassign_to": "s10"}},
    "s29": {"22424": {"exclude": True}},
}
SESSION_MERGES = {
    "s1258": {"unknown_2": "28338"},
    "s1391": {"unknown": "28270"},
    "s1445": {"unknown_5": "28037"},
}


def map_acquisition(label: str) -> AcquisitionRule | None:
    if label in SKIP_ACQUISITIONS or label.endswith("_qa-reject"):
        return None
    match = FUNCTIONAL.fullmatch(label)
    if match and match["task"] in TASKS:
        return AcquisitionRule(modality="func", suffix="bold", task=match["task"])
    return NON_FUNCTIONAL.get(label)


def normalize_label(label: str) -> str:
    return re.sub("sub-", "", re.sub("ses-", "", label))


def relevant_subject_labels(canonical: str) -> set[str]:
    return (
        {canonical}
        | {label for label, target in SUBJECT_ALIASES.items() if target == canonical}
        | {
            source
            for source, overrides in SESSION_OVERRIDES.items()
            for override in overrides.values()
            if override.get("reassign_to") == canonical
        }
    )
