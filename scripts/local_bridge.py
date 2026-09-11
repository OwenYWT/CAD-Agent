#!/usr/bin/env python3
"""Repository entry point for the same standalone client served by the API."""
import importlib.util
from pathlib import Path

path=Path(__file__).resolve().parents[1]/'backend/app/integrations/local_bridge_client.py'
spec=importlib.util.spec_from_file_location('cad_local_bridge',path)
client=importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

if __name__=='__main__':client.main()
