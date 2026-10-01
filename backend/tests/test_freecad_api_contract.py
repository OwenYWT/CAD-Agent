import pytest
from pydantic import ValidationError

from app.freecad.api_catalog import CapabilityQuery, query_capabilities
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.selection import SelectionError, validate_selected_operations


def plan(source="document.addObject('Part::Box','Box')"):
    return {'operations':[
        {'op_id':'api','action':'api.execute','args':{'source':source}},
        {'op_id':'save','action':'document.export','args':{'formats':['fcstd','step'],'objects':['Box']}}]}


def test_native_api_accepts_real_program_and_rejects_syntax_errors():
    assert FreeCADOperationPlan.model_validate(plan()).operations[0].action=='api.execute'
    with pytest.raises(ValidationError,match='syntax error'):
        FreeCADOperationPlan.model_validate(plan('if :'))


def test_api_cannot_mix_with_typed_transactions_or_bypass_selection_scope():
    value=plan();value['operations'].insert(0,{'op_id':'read','action':'document.inspect','args':{}})
    with pytest.raises(ValidationError,match='exactly'):
        FreeCADOperationPlan.model_validate(value)
    state={'objects':[{'name':'Box','type_id':'Part::Box'}],
        'selection_context':{'features':[{'kernel_name':'Box'}]}}
    with pytest.raises(SelectionError,match='选择范围'):
        validate_selected_operations(FreeCADOperationPlan.model_validate(plan()),state)


def test_catalog_is_measured_paged_and_does_not_claim_functional_verification():
    modules=query_capabilities(CapabilityQuery(limit=2))
    assert len(modules['entries'])==2 and modules['next_offset']==2
    assert modules['functional_coverage']=='not_certified_by_discovery'
    helix=query_capabilities(CapabilityQuery(module='Part',symbol='makeHelix'))
    assert any(row['name']=='Part.makeHelix' and row['doc'] for row in helix['entries'])
    with pytest.raises(ValueError,match='absent'):
        query_capabilities(CapabilityQuery(module='not_an_installed_module'))


def test_native_methods_and_document_types_have_measured_discovery_entries():
    methods = query_capabilities(CapabilityQuery(module='Part',symbol='Part.Shape.makeThickness'))
    assert any(x['name']=='Part.Shape.makeThickness' and x['doc'] for x in methods['entries'])
    types = query_capabilities(CapabilityQuery(category='object_type',symbol='PartDesign::AdditiveHelix'))
    assert types['entries']==[{'name':'PartDesign::AdditiveHelix','status':'registered_unverified'}]


@pytest.mark.parametrize('category,symbol', [('command','PartDesign_AdditiveHelix'), ('workbench','TechDrawWorkbench')])
def test_registered_gui_capabilities_are_discoverable_without_claiming_execution(category, symbol):
    result=query_capabilities(CapabilityQuery(category=category,symbol=symbol))
    assert any(row['name']==symbol and row['status']=='registered_unverified' for row in result['entries'])
    assert result['functional_coverage']=='not_certified_by_discovery'


def test_chamfer_scope_schema_requires_disabling_legacy_all_edges_default():
    import jsonschema
    from app.freecad.contracts import FeatureChamferArgs
    schema=FeatureChamferArgs.model_json_schema()
    legacy={'name':'Chamfer','target':'Body','size_mm':1}
    scoped={**legacy,'edge_scope':'outer','use_all_edges':False}
    for valid in (legacy,scoped):
        jsonschema.validate(valid,schema)
        FeatureChamferArgs.model_validate(valid)
    for invalid in ({**legacy,'edge_scope':'outer'}, {**scoped,'use_all_edges':True},
                    {**legacy,'use_all_edges':False}):
        with pytest.raises(jsonschema.ValidationError):jsonschema.validate(invalid,schema)
        with pytest.raises(ValidationError):FeatureChamferArgs.model_validate(invalid)
