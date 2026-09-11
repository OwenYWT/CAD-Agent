from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.freecad.contracts import InstanceCreateArgs, InstancePlacementArgs
from app.services.document_geometry import validate_scene
from app.workflows.temporal import FreeCADStructuredModificationV1


def test_instance_contracts_reject_invalid_transforms_and_external_sources():
    value = {'object':'InstanceA','source':'BodyA','translation_mm':[1,2,3]}
    assert InstanceCreateArgs.model_validate(value).rotation_axis == (0,0,1)
    for change in [{'translation_mm':[1,2]}, {'rotation_axis':[0,0,0]},
                   {'rotation_deg':float('nan')}, {'source':'../../other.FCStd'}, {'source':'BodyA.Face1'}]:
        with pytest.raises(ValidationError):
            InstanceCreateArgs.model_validate({**value, **change})
    with pytest.raises(ValidationError):
        InstancePlacementArgs.model_validate(value)


def test_scene_requires_finite_transforms_unique_names_and_known_definitions():
    scene = {'schema_version':'cad-scene.v1','units':'mm',
        'instances':[{'kernel_name':'BodyA','geometry_sha256':'a'*64,'matrix':[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]}],
        'definitions':{'a'*64:{'bounds_mm':[0,0,0,10,10,10]}}}
    assert validate_scene(scene) is scene
    unknown = deepcopy(scene); unknown['instances'][0]['geometry_sha256'] = 'b'*64
    duplicate = deepcopy(scene); duplicate['instances'] *= 2
    nan = deepcopy(scene); nan['instances'][0]['matrix'][0] = float('nan')
    empty = deepcopy(scene); empty['instances'] = []
    for invalid in [unknown,duplicate,nan,empty]:
        with pytest.raises(ValueError): validate_scene(invalid)


def test_native_edit_extension_preserves_historical_parameter_payload_identity():
    old = {'schema_version':'freecad-structured-modification.v1','expected_state_sha256':'a'*64,
           'parameter_updates':[{'parameter_id':'Pad.Length','value':12.0}]}
    assert FreeCADStructuredModificationV1.model_validate(old).model_dump(mode='json') == old
    native = {'expected_state_sha256':'a'*64,'native_edits':[{'action':'assembly.place',
        'args':{'object':'InstanceA','translation_mm':[20,0,0]}}]}
    assert FreeCADStructuredModificationV1.model_validate(native).native_edits[0].args['rotation_deg']==0
    with pytest.raises(ValidationError):
        FreeCADStructuredModificationV1.model_validate({**old,'native_edits':native['native_edits']})
    with pytest.raises(ValidationError):
        FreeCADStructuredModificationV1.model_validate({**native,'native_edits':[{'action':'property.set','args':{'object':'Pad','property':'Length','value':4}}]})
