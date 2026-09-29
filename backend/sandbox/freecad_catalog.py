"""Discover the installed distribution, without claiming functional verification."""
import importlib
import inspect
from pathlib import Path


def discover():
    import FreeCAD as App
    root=Path(App.getResourceDir())/'Mod'
    names={'FreeCAD', 'Part', 'Sketcher', 'Mesh', 'Import', 'Spreadsheet'}
    names.update(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith('_'))
    if App.GuiUp:
        names.add('FreeCADGui')
    modules=[]
    for name in sorted(names):
        entry={'module':name,'status':'installed_only','symbols':[]}
        try:
            module=importlib.import_module(name)
            entry['status']='importable_unverified'
            for symbol in sorted(dir(module)):
                if symbol.startswith('_'):continue
                value=getattr(module,symbol)
                if not callable(value):continue
                entry['symbols'].append({'name':name+'.'+symbol,'doc':getattr(value,'__doc__',None) or '',
                    'members':[member for member in dir(value) if not member.startswith('_')] if isinstance(value,type) else []})
                if isinstance(value, type):
                    for member in sorted(dir(value)):
                        if member.startswith('_'):
                            continue
                        method = inspect.getattr_static(value, member)
                        if callable(method) or inspect.ismethoddescriptor(method):
                            entry['symbols'].append({'name':name+'.'+symbol+'.'+member,
                                'owner':name+'.'+symbol, 'kind':'method',
                                'doc':getattr(method,'__doc__',None) or ''})
        except Exception as exc:
            entry['import_error']=type(exc).__name__+': '+str(exc)
        modules.append(entry)
    commands=[];workbenches=[]
    if App.GuiUp:
        import FreeCADGui as Gui
        workbenches=sorted(Gui.listWorkbenches())
        # Registered commands are discovered, not executed. Activating arbitrary
        # workbenches may require external tools or interactive initialization.
        commands=sorted(Gui.listCommands())
    document = App.newDocument('CapabilityDiscovery')
    try:
        object_types = sorted(document.supportedTypes())
    finally:
        App.closeDocument(document.Name)
    return {'schema_version':'freecad-api-catalog.v1','freecad':'.'.join(App.Version()[:3]),
            'gui':bool(App.GuiUp),'modules':modules,'registered_commands':commands,
            'registered_object_types':object_types,
            'registered_workbenches':workbenches,'functional_coverage':'not_certified_by_discovery'}
