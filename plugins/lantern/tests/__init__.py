"""Tests never start real background work: a session start in a test would otherwise launch curation, the doctor and
discovery as detached processes (and the doctor a 300 MB memory server) against a temp folder."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lantern_core import doctor, jobs  # noqa: E402

mock.patch.object(jobs, "start", lambda *a, **k: []).start()
mock.patch.object(doctor, "start_server", lambda: None).start()
