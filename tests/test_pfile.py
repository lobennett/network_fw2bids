"""Import the existing CNI spiral reconstruction, never send P-files to dcm2niix."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest

from network_fw2bids.planning import SubjectPlanner
from network_fw2bids.conversion import DicomConverter
from network_fw2bids.errors import PlanningError, ConversionError
from tests.fakes import FakeAcquisition, FakeSession, FakeSubject, FakeProject, FakeClient


def planner(tmp_path):
    files = [SimpleNamespace(name='scan_pfile.7.zip', type='pfile', file_id='pfile'),
             SimpleNamespace(name='scan.nii.gz', type='nifti', file_id='mag',
                             info={'SPIREC': {'psd_name': 'sprt', 'patient_id': 'PRIVATE', 'te': 0.007}}),
             SimpleNamespace(name='scan_fieldmap.nii.gz', type='nifti', file_id='field',
                             info={'Units': 'Hz', 'SPIREC': {'psd_name': 'sprt', 'patient_id': 'PRIVATE'}})]
    acq = FakeAcquisition('fmap-fieldmap', datetime(2020, 1, 1), files)
    acq.id = 'acquisition'
    downloads = []
    def download(name, destination):
        downloads.append(name)
        image = nib.Nifti1Image(np.ones((4, 5, 6), dtype=np.float32), np.eye(4))
        image.header['descrip'] = b'PRIVATE'
        image.header['data_type'] = b'PRIVATE'
        image.header.extensions.append(nib.nifti1.Nifti1Extension(6, b'PRIVATE'))
        nib.save(image, destination)
    acq.download_file = download
    session = FakeSession('100', acq.timestamp, [acq])
    return SubjectPlanner(FakeClient(FakeProject([FakeSubject('s03', [session])])), 'project'), acq, downloads


def test_selects_pfile_reconstruction_and_records_source_inventory(tmp_path):
    p, _, _ = planner(tmp_path)
    plans = p.plan('s03')
    assert len(plans) == 1
    assert plans[0].modality == 'fmap'
    assert p.selection['acquisitions'][0]['decision'] == 'selected'
    assert {f['name'] for f in p.selection['acquisitions'][0]['files']} == {
        'scan_pfile.7.zip', 'scan.nii.gz', 'scan_fieldmap.nii.gz'}


def test_fetches_full_acquisition_when_listing_omits_reconstruction_metadata(tmp_path):
    from copy import deepcopy
    p, acq, _ = planner(tmp_path)
    full = deepcopy(acq)
    for f in acq.files[1:]:
        f.info = {'BIDS': {}}
    p._client.get_acquisition = lambda identity: full
    assert len(p.plan('s03')) == 1


@pytest.mark.parametrize('missing', ['scan.nii.gz', 'scan_fieldmap.nii.gz'])
def test_incomplete_reconstruction_requires_upstream_spiral_recon(tmp_path, missing):
    p, acq, _ = planner(tmp_path)
    acq.files = [f for f in acq.files if f.name != missing]
    with pytest.raises(PlanningError, match='spiral-recon'):
        p.plan('s03')


def test_import_preserves_values_geometry_units_and_strips_identifiers(tmp_path):
    p, _, downloads = planner(tmp_path)
    plans = p.plan('s03')
    assert len(plans) == 1
    destination = tmp_path / 'bids'
    destination.mkdir()
    record = DicomConverter()._convert_archive(plans[0], destination, tmp_path / 'work')
    assert downloads == ['scan.nii.gz', 'scan_fieldmap.nii.gz']
    images = list(destination.rglob('*.nii.gz'))
    assert len(images) == 2
    for path in images:
        img = nib.load(path)
        np.testing.assert_array_equal(img.get_fdata(), np.ones((4, 5, 6)))
        np.testing.assert_array_equal(img.affine, np.eye(4))
        assert not img.header.extensions
        assert b'PRIVATE' not in img.header.binaryblock
    metadata = json.loads(next(destination.rglob('*_fieldmap.json')).read_text())
    assert metadata['Units'] == 'Hz'
    assert 'PRIVATE' not in json.dumps(record)
    assert 'PRIVATE' not in ''.join(f.read_text() for f in destination.rglob('*.json'))
    assert record['method'] == 'cni-spiral-import'
    assert record['pfile']['file_id'] == 'pfile'
    assert {row['file_id'] for row in record['sources']} == {'mag', 'field'}
    assert all(len(row['sha256']) == 64 for row in record['sources'])


def test_rejects_non_hz_fieldmap_without_relabeling_units(tmp_path):
    p, acq, _ = planner(tmp_path)
    acq.files[-1].info['Units'] = 'rad/s'
    with pytest.raises(PlanningError, match='Hz'):
        p.plan('s03')


def test_mismatched_geometry_is_not_published(tmp_path):
    p, acq, _ = planner(tmp_path)
    def download(name, destination):
        shape = (3, 4, 5) if 'fieldmap' in name else (4, 4, 5)
        nib.save(nib.Nifti1Image(np.ones(shape), np.eye(4)), destination)
    acq.download_file = download
    plans = p.plan('s03')
    assert len(plans) == 1
    target = tmp_path / 'bids'
    target.mkdir()
    with pytest.raises(ConversionError, match='geometry'):
        DicomConverter()._convert_archive(plans[0], target, tmp_path / 'work')
    assert not list(target.rglob('*.nii.gz'))


def test_cni_five_dimensional_magnitude_keeps_both_acquired_volumes(tmp_path):
    p, acq, _ = planner(tmp_path)
    def download(name, destination):
        data = np.ones((4, 5, 6, 1), dtype=np.float32)
        if 'fieldmap' not in name:
            data = np.stack([data, data * 2], axis=3)
        nib.save(nib.Nifti1Image(data, np.eye(4)), destination)
    acq.download_file = download
    target = tmp_path / 'bids'
    target.mkdir()
    DicomConverter()._convert_archive(p.plan('s03')[0], target, tmp_path / 'work')
    mag = nib.load(next(target.rglob('*_magnitude.nii.gz')))
    assert mag.shape == (4, 5, 6, 2)
    assert np.all(mag.get_fdata()[..., 0] == 1)
    assert np.all(mag.get_fdata()[..., 1] == 2)
    assert nib.load(next(target.rglob('*_fieldmap.nii.gz'))).shape == (4, 5, 6)
