"""Import CNI's existing spiral P-file reconstruction from Flywheel."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import nibabel as nib
import numpy as np
from flywheel.rest import ApiException

from .errors import PlanningError, ConversionError


@dataclass(frozen=True)
class FieldmapPlan:
    acquisition: Any
    pfile: Any
    magnitude: Any
    fieldmap: Any
    relative_prefix: Path
    modality: str = 'fmap'
    task: None = None

    @property
    def source_files(self) -> tuple:
        return self.pfile, self.magnitude, self.fieldmap


def plan_fieldmap(acquisition, prefix: Path) -> FieldmapPlan:
    files = acquisition.files
    pfiles = [f for f in files if f.name.endswith('_pfile.7.zip')]
    if len(pfiles) != 1:
        raise PlanningError(f'{acquisition.label}: expected one *_pfile.7.zip')
    stem = pfiles[0].name.removesuffix('_pfile.7.zip')
    selected = []
    for name in (stem + '.nii.gz', stem + '_fieldmap.nii.gz'):
        matches = [f for f in files if f.name == name]
        if len(matches) != 1:
            raise PlanningError(f'{acquisition.label}: missing or ambiguous {name}; run CNI spiral-recon on Flywheel')
        info = matches[0].info or {}
        if info.get('SPIREC', {}).get('psd_name') != 'sprt':
            raise PlanningError(f'{name}: expected CNI spiral-recon sprt metadata')
        selected.append(matches[0])
    if (selected[1].info or {}).get('Units') != 'Hz':
        raise PlanningError('CNI fieldmap must explicitly report Units=Hz')
    return FieldmapPlan(acquisition, pfiles[0], *selected, prefix)


def import_fieldmap(plan: FieldmapPlan, workspace: Path) -> tuple[list[Path], dict]:
    """Keep magnitude repeats; remove only trailing singleton axes and header identifiers."""
    images, sources, geometry = [], [], None
    for role, file in (('magnitude', plan.magnitude), ('fieldmap', plan.fieldmap)):
        path = workspace / f'{role}.nii.gz'
        try:
            plan.acquisition.download_file(file.name, str(path))
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            image = nib.load(path)
            data = np.asarray(image.dataobj)
            original_shape = list(data.shape)
            while data.ndim > 3 and data.shape[-1] == 1:
                data = data[..., 0]
            allowed_dims = (3, 4) if role == 'magnitude' else (3,)
            if data.ndim not in allowed_dims or not np.isfinite(data).all() or not np.isfinite(image.affine).all():
                raise ValueError('expected finite 3D fieldmap and 3D/4D magnitude')
            if geometry is not None and (data.shape[:3] != geometry[0] or not np.allclose(image.affine, geometry[1], atol=1e-5, rtol=0)):
                raise ValueError('magnitude and fieldmap geometry differ')
            geometry = data.shape[:3], image.affine.copy()
            header = image.header.copy()
            header.extensions.clear()
            for key in ('descrip', 'aux_file', 'intent_name', 'data_type', 'db_name'):
                if key in header:
                    header[key] = b''
            clean = type(image)(data, image.affine, header)
            clean.set_qform(image.get_qform(), int(image.header['qform_code']))
            clean.set_sform(image.get_sform(), int(image.header['sform_code']))
            nib.save(clean, path)
            metadata = {'Manufacturer': 'GE', 'Units': 'Hz'} if role == 'fieldmap' else {'Manufacturer': 'GE'}
            (workspace / f'{role}.json').write_text(json.dumps(metadata) + '\n')
        except (ApiException, OSError, ValueError, nib.filebasedimages.ImageFileError) as error:
            raise ConversionError(f'could not import CNI {role}: {error}') from error
        images.append(path)
        sources.append({**source_id(file), 'sha256': digest, 'role': role,
                        'source_shape': original_shape, 'output_shape': list(data.shape)})
    # Keep reconstruction identity; do not copy its command, patient or exam fields.
    command = (plan.fieldmap.info or {}).get('SPIREC', {}).get('recon_command', '')
    executable = re.search(r'\bspirec\d+\b', command)
    return images, {'method': 'cni-spiral-import', 'acquisition_id': getattr(plan.acquisition, 'id', None),
                    'pfile': source_id(plan.pfile), 'sources': sources,
                    'software': {'name': 'CNI spiral-recon', 'reconstruction': executable.group() if executable else 'SPIREC'}}


def source_id(file) -> dict:
    return {'name': file.name, 'file_id': getattr(file, 'file_id', None)}
