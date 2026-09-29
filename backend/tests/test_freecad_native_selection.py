import pytest
from app.freecad.contracts import FreeCADOperationPlan
from app.freecad.selection import SelectionError, validate_selected_operations

@pytest.mark.parametrize('action,args',[
    ('feature.loft',{'name':'New','profiles':['A','B']}),
    ('feature.sweep',{'name':'New','profile':'A','path':'B'}),
    ('feature.revolve',{'name':'New','profile':'A','axis':'z','angle_deg':90}),
    ('feature.polar_pattern',{'name':'New','originals':['Hole'],'axis':'z','occurrences':4,'angle_deg':360}),
    ('feature.linear_pattern',{'name':'New','originals':['Hole'],'axis':'x','occurrences':4,'length_mm':30}),
])
def test_new_native_features_respect_selected_body(action,args):
    state={'objects':[{'name':'Body','type_id':'PartDesign::Body','structure':{'status':'measured','members':['A','B','Hole']}},
        {'name':'A','type_id':'Sketcher::SketchObject'}, {'name':'B','type_id':'Sketcher::SketchObject'},
        {'name':'Hole','type_id':'PartDesign::Pocket','out':['A']}],
        'selection_context':{'features':[{'kernel_name':'Body'}]}}
    plan=FreeCADOperationPlan.model_validate({'operations':[
        {'op_id':'new','action':action,'args':args},
        {'op_id':'export','action':'document.export','args':{'formats':['fcstd','step']}}]})
    validate_selected_operations(plan,state)
    state['selection_context']['features']=[{'kernel_name':'Hole'}]
    with pytest.raises(SelectionError):validate_selected_operations(plan,state)
