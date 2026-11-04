"""SequenceWorker executes re-centering moves only between exposures."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from argos.core.imaging.fits_writer import FrameContext  # noqa: E402
from argos.core.imaging.recenter import KIND_RECENTER, MoveRequest  # noqa: E402
from argos.core.imaging.sequencer import SequencePlan, SequenceStep  # noqa: E402
from argos.core.imaging.session_log import SESSION_FILENAME  # noqa: E402
from argos.workers.sequence_worker import SequenceWorker  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


class _FakeCamera:
    def __init__(self):
        self.exposing = False

    def set_gain(self, gain):
        pass

    def try_get_gain(self):
        return 80

    def start_exposure(self, seconds, light=True):
        self.exposing = True

    def is_image_ready(self):
        return True

    def get_image_array(self):
        self.exposing = False
        return np.full((32, 48), 1000, dtype=np.uint16)

    def stop_exposure(self):
        self.exposing = False


class _FakeTelescope:
    def __init__(self, camera):
        self.camera = camera
        self.calls: list[tuple[int, float]] = []

    def move_axis(self, axis, rate):
        assert not self.camera.exposing, "mount moved during an exposure"
        self.calls.append((axis, rate))


def _worker(tmp_path, count=4, dither_every=0):
    camera = _FakeCamera()
    scope = _FakeTelescope(camera)
    plan = SequencePlan(
        steps=[SequenceStep(exposure_s=0.0, count=count, dither_every=dither_every)],
        object_name="Test",
    )
    worker = SequenceWorker(
        camera=camera,
        telescope=scope,
        filterwheel=None,
        plan=plan,
        frame_context_provider=lambda **kw: FrameContext(software="Argos test", **kw),
        base_dir=tmp_path,
    )
    return worker, scope


def _request(t0=0.4, t1=-0.2):
    return MoveRequest(kind=KIND_RECENTER, durations_s=(t0, t1), rate_deg_s=0.05, settle_s=0.0)


def test_queued_move_runs_between_frames_and_marks_next_frame(tmp_path):
    worker, scope = _worker(tmp_path)
    nudges = []
    saved = []
    worker.mount_nudged.connect(lambda kind, info: nudges.append((kind, info)))

    def on_saved(path, record):
        saved.append(record)
        if len(saved) == 2:
            worker.request_move(_request())

    worker.frame_saved.connect(on_saved)
    assert worker._run()

    # Primary +rate, secondary −rate, then both stopped.
    assert scope.calls[:2] == [(0, 0.05), (1, -0.05)]
    assert sorted(scope.calls[2:]) == [(0, 0.0), (1, 0.0)]
    assert scope.calls[2] == (1, 0.0)  # the shorter pulse stops first
    assert nudges == [
        (KIND_RECENTER, {"durations_s": (0.4, -0.2), "rate_deg_s": 0.05, "next_frame": 3})
    ]
    assert [r.mount_move for r in saved] == [None, None, KIND_RECENTER, None]
    session = next(tmp_path.rglob(SESSION_FILENAME))
    frames = json.loads(session.read_text())["frames"]
    assert frames[2]["mount_move"] == KIND_RECENTER


def test_no_request_no_motion(tmp_path):
    worker, scope = _worker(tmp_path)
    assert worker._run()
    assert scope.calls == []


def test_newer_request_replaces_unexecuted_one(tmp_path):
    worker, scope = _worker(tmp_path, count=2)

    def on_saved(path, record):
        if not scope.calls and record.mount_move is None:
            worker.request_move(_request(1.0, 0.0))
            worker.request_move(_request(0.0, 0.3))

    worker.frame_saved.connect(on_saved)
    worker._run()
    assert (1, 0.05) in scope.calls and (0, 0.05) not in scope.calls


def test_dither_reports_its_move(tmp_path):
    worker, scope = _worker(tmp_path, count=2, dither_every=1)
    worker._interruptible_sleep = lambda s: True  # skip the real pulse/settle
    nudges = []
    worker.mount_nudged.connect(lambda kind, info: nudges.append((kind, info["next_frame"])))
    worker._run()
    assert nudges == [("dither", 2), ("dither", 3)]


def test_move_without_mount_is_reported_skipped(tmp_path):
    worker, _ = _worker(tmp_path, count=2)
    worker._telescope = None
    nudges = []
    worker.mount_nudged.connect(lambda kind, info: nudges.append(info))
    worker.frame_saved.connect(lambda p, r: worker.request_move(_request()))
    worker._run()
    assert nudges and nudges[0]["skipped"] == "no mount"
