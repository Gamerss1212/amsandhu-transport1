import os

from viralforge.config import Config, write_default_config


def test_defaults_are_sane():
    cfg = Config()
    assert (cfg.render.width, cfg.render.height) == (1080, 1920)
    assert cfg.candidates.min_duration < cfg.candidates.target_duration < cfg.candidates.max_duration
    assert cfg.audio.loudness_target == -14.0
    assert 0.0 <= cfg.scoring.heuristic_weight <= 1.0


def test_yaml_merge_is_partial(tmp_path):
    path = tmp_path / "viralforge.yaml"
    path.write_text("render:\n  fps: 60\noutput:\n  clips: 9\n", encoding="utf-8")
    cfg = Config.load(str(path))
    assert cfg.render.fps == 60
    assert cfg.output.clips == 9
    assert cfg.render.width == 1080          # untouched keys keep their defaults


def test_env_overrides_are_typed(monkeypatch):
    monkeypatch.setenv("VF_RENDER_FPS", "60")
    monkeypatch.setenv("VF_CAPTIONS_ENABLED", "false")
    monkeypatch.setenv("VF_AUDIO_LOUDNESS_TARGET", "-16.5")
    cfg = Config()
    cfg.apply_env()
    assert cfg.render.fps == 60 and isinstance(cfg.render.fps, int)
    assert cfg.captions.enabled is False
    assert cfg.audio.loudness_target == -16.5


def test_written_default_config_round_trips(tmp_path):
    path = tmp_path / "out.yaml"
    write_default_config(str(path))
    cfg = Config.load(str(path))
    assert cfg.render.height == 1920
