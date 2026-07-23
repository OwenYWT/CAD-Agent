"""Autodesk Fusion 360 Add-in lifecycle entry point."""

from __future__ import annotations

import json
import logging
import queue
import traceback
from pathlib import Path

try:  # Import succeeds only inside Fusion Desktop.
    import adsk.core
    import adsk.fusion
except ImportError:  # Pure-Python tests intentionally run without Fusion.
    adsk = None

from . import __version__
from .agent_transport import CloudAgentWorker
from .config import load_config
from .dispatcher import Dispatcher
from .fusion_api import FusionApiFacade
from .palette import ControllerError, PaletteController
from .protocol import ExecutionJournal
from .transport import ConnectorWorker


EVENT_ID = "com.cad-agent.fusion360.dispatch"
PALETTE_ID = "com.cad-agent.fusion360.palette"
_handlers: list[object] = []
_worker = None
_controller = None
_palette = None
_custom_event = None
_inbound = None
_outbound = None
log = logging.getLogger("CADAgentFusionConnector")


def _native_high_risk_confirmation(ui, message: str) -> bool:
    result = ui.messageBox(
        message,
        "CAD Agent approval",
        adsk.core.MessageBoxButtonTypes.YesNoButtonType,
        adsk.core.MessageBoxIconTypes.WarningIconType,
    )
    return result == adsk.core.DialogResults.DialogYes


def _create_palette(ui):
    existing = ui.palettes.itemById(PALETTE_ID)
    if existing:
        # A previous workspace can retain a hidden embedded browser after an
        # Add-in reload.  Recreate it so no stale JavaScript state or handler is
        # trusted by the new controller generation.
        existing.deleteMe()
    url = (Path(__file__).resolve().parent / "palette.html").as_uri()
    return ui.palettes.add(
        PALETTE_ID,
        "CAD Agent",
        url,
        True,
        True,
        True,
        420,
        640,
        True,
    )


def run(_context):
    global _worker, _controller, _palette, _custom_event, _inbound, _outbound
    if adsk is None:
        raise RuntimeError("CADAgentFusionConnector must run inside Autodesk Fusion")
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        if _worker is not None or _custom_event is not None:
            raise RuntimeError("CAD Agent Fusion Connector is already running")
        config = load_config()
        journal = ExecutionJournal(config.journal_path)
        facade = FusionApiFacade(app, ui, config)
        dispatcher = Dispatcher(facade, journal)
        registration = FusionApiFacade.registration(app, config, __version__)
        _inbound = queue.Queue(maxsize=config.queue_size)
        _outbound = queue.Queue(maxsize=config.queue_size)
        _custom_event = app.registerCustomEvent(EVENT_ID)

        if config.mode == "local_runtime":
            class RuntimeMainThreadHandler(adsk.core.CustomEventHandler):
                def __init__(self):
                    super().__init__()

                def notify(self, _args):
                    # Bound each event so Fusion's UI loop can keep processing.
                    for _ in range(4):
                        try:
                            task, cancel = _inbound.get_nowait()
                        except queue.Empty:
                            break
                        result = dispatcher.dispatch(task, cancel)
                        _outbound.put((task, result))

            handler = RuntimeMainThreadHandler()
            _custom_event.add(handler)
            _handlers.append(handler)
            _worker = ConnectorWorker(
                config,
                registration,
                _inbound,
                _outbound,
                # This is the only callable the network worker receives from an
                # Autodesk object.  It never receives app/ui/facade/Palette.
                signal_main_thread=lambda: app.fireCustomEvent(EVENT_ID),
            )
        else:
            capabilities = dict(registration["capabilities"])
            capabilities["runtime_online"] = False
            cloud_registration = {**registration, "capabilities": capabilities}
            _palette = _create_palette(ui)

            _controller = PaletteController(
                config,
                dispatcher,
                transport=None,  # Assigned immediately after worker creation.
                send_to_palette=lambda action, data: _palette.sendInfoToHTML(action, data),
                capabilities=capabilities,
                confirm_high_risk=lambda message: _native_high_risk_confirmation(ui, message),
            )

            class AgentMainThreadHandler(adsk.core.CustomEventHandler):
                def __init__(self):
                    super().__init__()

                def notify(self, _args):
                    for _ in range(4):
                        try:
                            message = _outbound.get_nowait()
                        except queue.Empty:
                            break
                        _controller.handle_worker_message(message)

            class PaletteIncomingHandler(adsk.core.HTMLEventHandler):
                def __init__(self):
                    super().__init__()

                def notify(self, args):
                    try:
                        _controller.handle_palette_event(str(args.action), str(args.data))
                        args.returnData = json.dumps({"accepted": True}, separators=(",", ":"))
                    except ControllerError as exc:
                        args.returnData = json.dumps(
                            {"accepted": False, "error": exc.as_dict()},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )

            event_handler = AgentMainThreadHandler()
            palette_handler = PaletteIncomingHandler()
            _custom_event.add(event_handler)
            _palette.incomingFromHTML.add(palette_handler)
            _handlers.extend([event_handler, palette_handler])
            _worker = CloudAgentWorker(
                config,
                _inbound,
                _outbound,
                # Official CustomEvent is the sole worker-to-main-thread bridge.
                signal_main_thread=lambda: app.fireCustomEvent(EVENT_ID),
                registration=cloud_registration,
            )
            _controller.transport = _worker
            _controller.replay_pending_reports()

        _worker.start()
        log.info("CAD Agent Fusion Connector started", extra={"mode": config.mode})
    except Exception:
        _cleanup(app, bounded_timeout=3.0)
        ui.messageBox("CAD Agent Fusion Connector failed to start.\n\n" + traceback.format_exc())


def _cleanup(app, *, bounded_timeout: float) -> None:
    global _worker, _controller, _palette, _custom_event, _inbound, _outbound
    # Reject Palette input first, then stop/join the worker.  Only after the
    # worker can no longer call fireCustomEvent do we remove event handlers.
    if _controller is not None:
        try:
            _controller.stop()
        except Exception:
            log.exception("Failed to stop Palette controller")
    joined = True
    if _worker is not None:
        try:
            stopped = _worker.stop(timeout=bounded_timeout)
            joined = True if stopped is None else bool(stopped)
        except Exception:
            joined = False
            log.exception("Failed to stop Fusion connector worker")
    if not joined:
        # Fail closed: retaining the event is safer than allowing a late worker
        # callback to target an unregistered CustomEvent.
        raise RuntimeError("Fusion connector worker did not stop within the bounded timeout")
    _worker = None
    if _palette is not None:
        try:
            _palette.deleteMe()
        except Exception:
            log.exception("Failed to delete CAD Agent Palette")
        _palette = None
    if _custom_event is not None:
        try:
            app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            log.exception("Failed to unregister CAD Agent CustomEvent")
        _custom_event = None
    _handlers.clear()
    _controller = None
    _inbound = None
    _outbound = None


def stop(_context):
    if adsk is None:
        return
    app = adsk.core.Application.get()
    try:
        _cleanup(app, bounded_timeout=3.0)
    except Exception:
        app.userInterface.messageBox(
            "CAD Agent Fusion Connector failed to stop cleanly.\n\n" + traceback.format_exc()
        )
