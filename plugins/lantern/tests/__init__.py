"""Tests never start real background work: a session start in a test would otherwise launch curation, the doctor and
discovery as detached processes (and the doctor a 300 MB memory server) against a temp folder."""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile

from lantern_core import discovery, doctor, insights, jobs, packs, paths, survey  # noqa: E402

_POINTER = Path(tempfile.mkdtemp()) / "data-dir"  # tests must never rewrite the user's own data-dir pointer
paths.real_pointer = paths._pointer
mock.patch.object(paths, "_pointer", lambda: _POINTER).start()

jobs.real_start = jobs.start
mock.patch.object(jobs, "start", lambda *a, **k: []).start()
doctor.real_start_server = doctor.start_server
mock.patch.object(doctor, "start_server", lambda: None).start()
mock.patch.object(discovery, "discover", lambda depth: []).start()  # a first session in a test must not scout the real machine
discovery.real_frontiers = discovery.frontiers
mock.patch.object(discovery, "frontiers", lambda: []).start()  # nor ask the real password-manager CLIs whether they are signed in
survey.real_start = survey.start
mock.patch.object(survey, "start", lambda *a, **k: False).start()  # nor start a background survey
insights.real_run = insights.run
mock.patch.object(insights, "run", lambda db, now=None: []).start()  # nor inspect the real disk and units
_SKILLS = Path(tempfile.mkdtemp()) / "skills"  # nor write a pack's skill into the user's own ~/.claude/skills
packs.real_skills_dir = packs.skills_dir
mock.patch.object(packs, "skills_dir", lambda: _SKILLS).start()
