import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """The synthetic source video plus its transcript."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg is not installed")
    from fixtures import make_test_srt, make_test_video
    directory = tmp_path_factory.mktemp("media")
    return make_test_video(directory), make_test_srt(directory)
