"""Bambu-style 3MF building: project settings for Bambu Studio."""

from __future__ import annotations

import io
import json
import struct
import zipfile

import pytest

from os2slice.threemf import Part, build_3mf, project_settings_of, with_project_settings


def triangle_stl() -> bytes:
    tri = struct.pack("<3f", 0, 0, 1) + struct.pack("<9f", 0, 0, 0, 10, 0, 0, 0, 10, 0) + b"\0\0"
    return bytes(80) + struct.pack("<I", 1) + tri


def test_with_project_settings_labels_the_file_as_bambu_studio() -> None:
    base = build_3mf([Part("A", triangle_stl(), 1)], "Obj")
    assert (
        '<metadata name="Application">os2slice</metadata>'
        in zipfile.ZipFile(io.BytesIO(base)).read("3D/3dmodel.model").decode()
    )
    settings = json.dumps({"version": "02.08.04.57"}).encode()
    out = with_project_settings(base, settings)
    z = zipfile.ZipFile(io.BytesIO(out))
    # Bambu Studio loads a 3MF's settings only from files it thinks it wrote.
    assert "BambuStudio-02.08.04.57" in z.read("3D/3dmodel.model").decode()
    assert project_settings_of(out) == settings
    assert "Metadata/model_settings.config" in z.namelist()


def test_project_settings_refusals() -> None:
    base = build_3mf([Part("A", triangle_stl(), 1)], "Obj")
    with pytest.raises(ValueError):
        with_project_settings(base, b'{"version": "<evil>"}')
    with pytest.raises(ValueError):
        with_project_settings(base, b"not json")
    with pytest.raises(ValueError):
        project_settings_of(b"not a zip")
    with pytest.raises(ValueError):
        project_settings_of(base)  # no settings inside


def test_copies_are_instances_of_one_object() -> None:
    offsets = [(-20.0, 0.0), (0.0, 0.0), (20.5, -3.0)]
    out = build_3mf([Part("A", triangle_stl(), 1)], "Obj", [1], offsets)
    z = zipfile.ZipFile(io.BytesIO(out))
    model = z.read("3D/3dmodel.model").decode()
    assert model.count("<item ") == 3 and model.count('objectid="2"') == 3
    assert 'transform="1 0 0 0 1 0 0 0 1 20.5 -3 0"' in model
    ms = z.read("Metadata/model_settings.config").decode()
    assert ms.count("<object ") == 1  # one object...
    assert ms.count("<model_instance>") == 3  # ...on the plate three times
    assert 'key="instance_id" value="2"' in ms


def test_one_copy_by_default() -> None:
    model = zipfile.ZipFile(io.BytesIO(build_3mf([Part("A", triangle_stl(), 1)], "Obj")))
    assert 'transform="1 0 0 0 1 0 0 0 1 0 0 0"' in model.read("3D/3dmodel.model").decode()
