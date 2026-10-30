"""Tests never start real background work: a session start in a test would otherwise launch curation, the doctor and
discovery as detached processes (and the doctor a 300 MB memory server) against a temp folder."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lantern_core import discovery, doctor, insights, jobs, survey  # noqa: E402

jobs.real_start = jobs.start
mock.patch.object(jobs, "start", lambda *a, **k: []).start()
doctor.real_start_server = doctor.start_server
mock.patch.object(doctor, "start_server", lambda: None).start()
mock.patch.object(discovery, "discover", lambda depth: []).start()  # a first session in a test must not scout the real machine
survey.real_start = survey.start
mock.patch.object(survey, "start", lambda *a, **k: False).start()  # nor start a background survey
insights.real_run = insights.run
mock.patch.object(insights, "run", lambda db, now=None: []).start()  # nor inspect the real disk and units
