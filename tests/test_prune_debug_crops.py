"""prune_debug_crops reaps ONLY stale debug_* crops.

The figure crops (q*/docximg_) are the de-facto image store variant generation
reads back at build time, so they must survive. Recent debug crops (a possible
concurrent run) must survive too.
"""
import os
import time

from app.services import file_processor as fp


def test_prunes_only_old_debug_crops(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "IMAGE_SAVE_DIR", tmp_path)

    old_debug = tmp_path / "debug_q7_p1_aaaaaa.png"
    new_debug = tmp_path / "debug_q8_p1_bbbbbb.png"
    figure = tmp_path / "q7_p1_cccccc.png"          # referenced by variants
    docximg = tmp_path / "docximg_dddddd.png"        # referenced by variants
    for f in (old_debug, new_debug, figure, docximg):
        f.write_bytes(b"x")

    old = time.time() - 7200                          # 2h ago → stale
    os.utime(old_debug, (old, old))

    removed = fp.prune_debug_crops(older_than_seconds=3600)

    assert removed == 1
    assert not old_debug.exists()                     # stale debug → gone
    assert new_debug.exists()                         # fresh debug → kept
    assert figure.exists()                            # figure crop → kept
    assert docximg.exists()                           # docx image → kept


def test_missing_dir_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "IMAGE_SAVE_DIR", tmp_path / "nope")
    assert fp.prune_debug_crops() == 0
