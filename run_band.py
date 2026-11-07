"""Run all three negotiation agents connected to Band.

  .venv/bin/python run_band.py               # brain from AGENT_BRAIN (default gemini)
  .venv/bin/python run_band.py --brain scripted

Then in Band: create a room, add "Deal Coordinator", and send `@Deal Coordinator start`.
"""
import argparse
import asyncio
import logging

from dotenv import load_dotenv

from band import configure_logging

from negotiation.band_runtime import run

if __name__ == "__main__":
    load_dotenv(".env")
    configure_logging(logging.INFO, extra_loggers={"negotiation.band": logging.INFO})
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", choices=["gemini", "claude", "scripted"])
    asyncio.run(run(ap.parse_args().brain))
