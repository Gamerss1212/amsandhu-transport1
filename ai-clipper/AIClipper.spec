# PyInstaller recipe: pyinstaller AIClipper.spec  ->  dist/AIClipper/AIClipper(.exe)
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = [("config.example.yaml", "."), (".env.example", "."),
         ("clipper/web/templates", "clipper/web/templates"), ("clipper/editing/fonts", "clipper/editing/fonts")]
for pkg in ("faster_whisper", "imageio_ffmpeg", "yt_dlp_ejs"):  # whisper VAD, ffmpeg, YouTube JS solver
    datas += collect_data_files(pkg)
binaries = collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime") + collect_dynamic_libs("curl_cffi")
hiddenimports = collect_submodules("clipper") + collect_submodules("uvicorn") + collect_submodules("yt_dlp_ejs")

from deno import find_deno_bin  # JavaScript runtime YouTube downloads need

binaries += [(find_deno_bin(), "denobin")]

a = Analysis(["launcher.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             excludes=["pytest", "tkinter", "matplotlib", "IPython", "torch"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AIClipper", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="AIClipper", upx=False)
