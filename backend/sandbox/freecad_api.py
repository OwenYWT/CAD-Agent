"""Native API execution in a child process; export/validation stay in the parent.

The security boundary is the existing networkless, read-only sandbox container,
not a Python keyword filter. User programs receive no application credentials.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


class APIExecutionError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code=code


def execute_program(document, args):
    import FreeCAD as App
    import ast
    import re
    source = args['source']
    ast.parse(source, mode='exec')
    environment = args.get('environment', 'headless')
    if environment not in {'headless', 'gui'}:
        raise ValueError('unknown FreeCAD execution environment')
    modules = args.get('modules') or []
    if any(not isinstance(m,str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*',m) for m in modules):
        raise ValueError('invalid module name')
    work = Path(tempfile.mkdtemp(prefix='freecad-api-'))
    before = work/'input.FCStd'; after = work/'output.FCStd'; status = work/'status.json'
    document.saveAs(str(before))
    invocation = uuid.uuid4().hex
    program = work/'program.py'; program.write_text(source, encoding='utf-8')
    script = work/'run.FCMacro'
    script.write_text('''import FreeCAD as App
import Part, Sketcher, importlib, json, os, sys, traceback
try:
    document = App.openDocument(BEFORE)
    App.setActiveDocument(document.Name)
    if GUI:
        import FreeCADGui as Gui
    for module in MODULES:
        importlib.import_module(module)
    exec(compile(open(PROGRAM, encoding="utf-8").read(), PROGRAM, "exec"), globals())
    if document is None or App.getDocument(document.Name) is not document:
        raise RuntimeError("API program must preserve its document identity")
    document.recompute()
    document.saveAs(AFTER)
    result = {"invocation": INVOCATION, "status": "succeeded"}
except BaseException as exc:
    result = {"invocation": INVOCATION, "status": "failed", "exception_type":type(exc).__name__, "error": type(exc).__name__+": "+str(exc), "traceback": traceback.format_exc()}
with open(STATUS, "w", encoding="utf-8") as output:
    json.dump(result, output)
sys.stdout.flush(); sys.stderr.flush()
if GUI:
    os._exit(0 if result["status"] == "succeeded" else 1)
'''.replace('BEFORE',repr(str(before))).replace('AFTER',repr(str(after)))
       .replace('PROGRAM',repr(str(program))).replace('STATUS',repr(str(status)))
       .replace('INVOCATION',repr(invocation)).replace('MODULES',repr(modules))
       .replace('GUI',repr(environment=='gui')), encoding='utf-8')
    env = {k:v for k,v in os.environ.items() if not k.startswith('CAD_FREECAD_')}
    display = None
    if environment == 'gui':
        env['QTWEBENGINE_DISABLE_SANDBOX']='1'
        reader, writer = os.pipe()
        try:
            display = subprocess.Popen(['Xvfb','-displayfd',str(writer),'-screen','0','1280x1024x24','-nolisten','tcp'],
                pass_fds=(writer,),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        finally:
            os.close(writer)
        with os.fdopen(reader) as channel:
            number = channel.readline().strip()
        if not number.isdigit():
            display.wait()
            raise APIExecutionError('api_runtime_unavailable','virtual display failed to initialize')
        env['DISPLAY']=':'+number
        command=['/opt/freecad/usr/bin/freecad',str(script)]
    else:
        command=['/opt/freecad/bin/FreeCADCmd','-c',
                 f"exec(compile(open({str(script)!r}).read(), {str(script)!r}, 'exec'))"]
    try:
        completed = subprocess.run(command, env=env, cwd=work, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, close_fds=True)
    finally:
        if display is not None:
            display.terminate();display.wait()
    if not status.is_file():
        raise APIExecutionError('api_runtime_unavailable','FreeCAD API process returned no result: '+completed.stderr[-2000:])
    result=json.loads(status.read_text())
    if result.get('invocation') != invocation or result.get('status') != 'succeeded' or completed.returncode != 0:
        code=('api_dependency_unavailable' if result.get('exception_type') in {'ModuleNotFoundError','ImportError'} else 'api_execution_failed')
        raise APIExecutionError(code,'FreeCAD API failed: '+str(result.get('error') or completed.stderr[-2000:]))
    if not after.is_file() or after.is_symlink():
        raise RuntimeError('FreeCAD API did not save a native document')
    # Reopen in this trusted process. Script globals cannot replace the parent's
    # shape checker, exporter, projector or result-channel functions.
    reopened = App.openDocument(str(after))
    App.closeDocument(document.Name)
    return reopened
