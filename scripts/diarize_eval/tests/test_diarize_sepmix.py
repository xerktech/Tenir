"""Unit tests for diarize_sepmix's separator loader (XERK-3071)."""

from __future__ import annotations

import contextlib
import os
import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diarize_sepmix as sm


def _fake_clearvoice(monkeypatch, seen: dict) -> None:
    """Stand-ins for torch and ClearerVoice: the real ones need weights and minutes of CPU."""
    torch = types.ModuleType("torch")
    torch.inference_mode = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, "torch", torch)

    class Model:
        model, device, args = "net", "cpu", "cfg"

    class ClearVoice:
        def __init__(self, task: str, model_names: list[str]) -> None:
            seen["init"] = (task, model_names, os.getcwd())
            self.models = [Model()]

    def decode_one_audio(model, device, inputs, args):
        seen["decode"] = (model, device, inputs.shape, args)
        # ClearerVoice pads to its decode window and returns float64 per speaker.
        padded = np.pad(inputs[0], (0, 100)).astype(np.float64)
        return [padded, -padded]

    cv = types.ModuleType("clearvoice")
    cv.ClearVoice = ClearVoice
    decode = types.ModuleType("clearvoice.utils.decode")
    decode.decode_one_audio = decode_one_audio
    monkeypatch.setitem(sys.modules, "clearvoice", cv)
    monkeypatch.setitem(sys.modules, "clearvoice.utils", types.ModuleType("clearvoice.utils"))
    monkeypatch.setitem(sys.modules, "clearvoice.utils.decode", decode)


def test_a_bare_name_loads_a_clearvoice_separator_with_two_input_length_streams(
    monkeypatch, tmp_path
):
    seen: dict = {}
    _fake_clearvoice(monkeypatch, seen)
    before = os.getcwd()

    separate = sm.load_separator("MossFormer2_SS_16K", tmp_path / "ckpt")
    streams = separate(np.ones(1600, dtype=np.float32))

    task, names, cwd = seen["init"]
    assert (task, names) == ("speech_separation", ["MossFormer2_SS_16K"])
    # Weights land under --checkpoints, and the caller's working directory is restored.
    assert Path(cwd) == (tmp_path / "ckpt").resolve()
    assert os.getcwd() == before
    assert seen["decode"] == ("net", "cpu", (1, 1600), "cfg")
    assert streams.shape == (2, 1600) and streams.dtype == np.float32
    assert np.all(streams[0] == 1) and np.all(streams[1] == -1)


def test_a_failed_clearvoice_load_still_restores_the_working_directory(monkeypatch, tmp_path):
    seen: dict = {}
    _fake_clearvoice(monkeypatch, seen)

    def boom(task, model_names):
        raise RuntimeError("no weights")

    monkeypatch.setattr(sys.modules["clearvoice"], "ClearVoice", boom)
    before = os.getcwd()
    with pytest.raises(RuntimeError, match="no weights"):
        sm.load_separator("MossFormer2_SS_16K", tmp_path / "ckpt")
    assert os.getcwd() == before


class _Tensor:
    """Just enough of a torch tensor for the SepFormer branch."""

    def __init__(self, a: np.ndarray) -> None:
        self.a = a

    def __getitem__(self, k):
        return _Tensor(self.a[k])

    @property
    def T(self):
        return _Tensor(self.a.T)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


@pytest.mark.parametrize("cuda", [True, False])
def test_a_hub_id_loads_sepformer_on_the_gpu_when_there_is_one(monkeypatch, tmp_path, cuda):
    seen: dict = {}
    torch = types.ModuleType("torch")
    torch.inference_mode = contextlib.nullcontext
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    torch.from_numpy = _Tensor

    class Sepformer:
        @staticmethod
        def from_hparams(source, savedir, run_opts):
            seen.update(source=source, device=run_opts["device"])
            return Sepformer()

        def separate_batch(self, batch):
            x = batch.a[0]
            return _Tensor(np.stack([x, -x], axis=-1)[None])  # (B, T, speakers)

    sb = types.ModuleType("speechbrain.inference.separation")
    sb.SepformerSeparation = Sepformer
    monkeypatch.setitem(sys.modules, "torch", torch)
    for m in ("speechbrain", "speechbrain.inference"):
        monkeypatch.setitem(sys.modules, m, types.ModuleType(m))
    monkeypatch.setitem(sys.modules, "speechbrain.inference.separation", sb)

    separate = sm.load_separator("speechbrain/sepformer-whamr16k", tmp_path)
    streams = separate(np.ones(800, dtype=np.float32))

    assert seen == {
        "source": "speechbrain/sepformer-whamr16k",
        "device": "cuda" if cuda else "cpu",
    }
    assert streams.shape == (2, 800)
    assert np.all(streams[0] == 1) and np.all(streams[1] == -1)


def test_a_nan_stream_is_scaled_to_silence_not_nan():
    out = sm._scaled(np.array([np.nan, 0.5, -0.25], dtype=np.float32))
    assert np.all(np.isfinite(out))
