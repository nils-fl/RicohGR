"""Snap meter (tools/focus/snap_meter.py), run inside WATCH.SH on the Mac against the mocks of watch_harness.py:
AF runs, Snap and its read-back, presses during batches, the label, the tag, SAMP.BIN and the tracer, the log
decoder. Needs the decoded 1.60 image."""
import re
import subprocess
from pathlib import Path

import pytest

if not (Path(__file__).resolve().parents[2] / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/firmware/gr_unpack.py)", allow_module_level=True)

from watch_harness import *  # noqa: E402,F401,F403


@pytest.mark.parametrize("mm,slot", [(261, "0 30"), (400, "0 30"), (550, "1 100"), (829, "1 100"), (1330, "2 150"), (1745, "3 200"),
                                     (2110, "3 200"), (2500, "4 250"), (3000, "5 350"), (4000, "5 350"),
                                     (5952, "6 500"), (12164, "6 500")])
def test_nearest_snap_setting_in_dioptres(tmp_path, mm, slot):
    assert " ".join(awk_out(tmp_path, f"nearslot {mm}")) == slot


@pytest.mark.parametrize("subject,f,snap", [(1330, 2.8, 1330), (20000, 8.0, round(1000 / fb.half_zone(8.0))),
                                            (150, 5.6, 200)])
def test_meter_is_the_subject_or_the_hyperfocal_distance(tmp_path, subject, f, snap):
    assert abs(int(awk_out(tmp_path, f"meter {subject} {f}")[0]) - snap) <= 1


def test_meter_sets_snap_after_the_lens_settles_and_restores_on_leaving(tmp_path):
    # 5 m: below the hyperfocal distance at f/2.8, beyond it at f/8, so the new aperture moves the snap.
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 5000, 1, 101),
            (3, "U2", "2.8", 7000, 5000, 1, 102), (4, "U2", "8", 7000, 5000, 10, 103),
            (5, "P", "8", 7000, 5000, 10, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "meter subject 5000 av 2.8 snap 5000 slot 500 patch 0" in log
    assert "meter av 8" in log and "meter subject 5000 av 8 snap " in log
    assert "meter subject 5000 av 8 snap 5000 " not in log
    assert patch.splitlines() == ["M.BIN ", "CPTAG.BIN ", "CPTAG.BIN restore", "M.BIN restore", "M.BIN ",
                                  "CPTAG.BIN ", "CPTAG.BIN restore", "M.BIN restore", "CPEMPTY.BIN ", "CPEMPTY.BIN restore"]
    assert relay.count("UserData Set StillSnapDistanceCm 500'") == 2   # both near the 5 m label
    # As Fn (AF/Snap toggle) does it: AF mode into the back-up slot, focus Snap. The new aperture comes
    # in Snap: only the distance.
    assert relay.count("StillBackUpFocus Select'") == 1 and relay.count("StillFocus Snap'") == 1
    assert "AUD_beep" not in relay and "SW_fn" not in relay and "CommonTempFocus" not in relay
    assert "meter set 1 focus 10 backup 1 cm 500" in log                   # read back after the batch
    assert "StillFullPressSnap" not in relay and "StillFocus Select" not in relay   # leaving: P recalls its own
    assert "SW_sws" not in relay                                           # the meter never presses
    assert "meter fn" not in log                                           # Fn = AF/Snap toggle in U2: quiet


def test_a_new_aperture_in_af_does_not_switch_to_snap(tmp_path):
    """After Fn back to AF, P's aperture changes with the light: no Snap until a half-press."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 5000, 1, 101),
            (3, "U2", "2.8", 7000, 5000, 1, 102), (4, "U2", "2.8", 7000, 5000, 10, 103),
            (5, "U2", "2.8", 7000, 5000, 1, 104), (6, "U2", "8", 7000, 5000, 1, 105),
            (7, "U2", "8", 7000, 5000, 1, 106)]
    log, relay, patch = run_watch(tmp_path, rows, limit=8)
    assert "meter focus 1 backup 13" in log and "meter av" not in log
    assert relay.count("StillFocus Snap'") == 1


def test_a_half_press_right_after_the_dial_counts(tmp_path):
    """The lens reading from before the turn is the base: AF already done when U2 is seen still meters."""
    rows = [(1, "P", "2.8", 7000, 2500, 1, 100), (2, "P", "2.8", 7000, 2500, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 1, 103),
            (5, "U2", "2.8", 7000, 1330, 1, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "meter subject 1330" in log


def test_meter_ignores_snap_and_mf_and_an_unmoved_lens(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 10, 100), (2, "U2", "2.8", 7000, 1330, 10, 101),
            (3, "U2", "2.8", 7000, 1330, 9, 102), (4, "U2", "2.8", 7000, 2500, 1, 103),
            (5, "U2", "2.8", 7000, 2500, 1, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "meter subject" not in log and patch == ""


def test_meter_batch_fits_the_relay():
    """The largest Snap batch (the redraw, cm, the longest back-up name, Snap) and the tag batch."""
    sub = fw.watch_sh()
    pre = fw.ttl("UserData Set StillSnapDistanceCm 500", sm.MSET_MS)
    cm = fw.ttl("UserData Set StillSnapDistanceCm 350", sm.MSET_MS)
    longest = max(fw.FOCUS_NAMES.values(), key=len)
    bk = fw.ttl(f"UserData Set StillBackUpFocus {longest}", sm.MSET_MS)
    snap = fw.ttl("UserData Set StillFocus Snap", sm.MSNAP_MS)
    assert pre.replace("500", "%s") in sub and bk.replace(longest, "%s") in sub and snap in sub
    frame = rc.solo_frame_bytes()
    assert len(pre + cm + bk + snap) + frame <= rc.RELAY_SIZE - 7
    tag = re.search(r'printf "([^"]*SetupCopyrightAttach On[^"]*)" GRSNAP', SCRIPT).group(1).replace("\\n", "\n")
    assert len(tag.replace("%s", "GRSNAP:65535:999999")) + frame <= rc.RELAY_SIZE - 7
    assert "AUD_beep" not in sub


def test_meter_measures_again_on_every_new_distance_but_not_at_the_snap_distance(tmp_path):
    """Fn back to AF between the measurements; the lens rests at the snap distance (the hyperfocal one for the
    far subject), which is the base, not a subject."""
    h = 1000 / fb.half_zone(2.8)                                           # hyperfocal mm at f/2.8
    far = 30000
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 10, 103),
            (5, "U2", "2.8", 7000, 1330, 1, 104), (6, "U2", "2.8", 7000, far, 1, 105),
            (7, "U2", "2.8", 7000, far, 1, 106), (8, "U2", "2.8", 7000, round(h), 10, 107),
            (9, "U2", "2.8", 7000, round(h), 1, 108), (10, "U2", "2.8", 7001, round(h) + 50, 1, 109),
            (11, "U2", "2.8", 7001, round(h) + 50, 1, 110)]
    log, relay, patch = run_watch(tmp_path, rows, limit=12)
    assert "meter subject 1330 av 2.8 snap 1330" in log
    assert f"meter subject {far} av 2.8 snap {round(h)}" in log or f"meter subject {far} av 2.8 snap {round(h) + 1}" in log
    assert f"meter subject {round(h) + 50} " in log and f"meter subject {round(h)} " not in log
    assert "StillSnapDistanceCm 150'" in relay and "StillSnapDistanceCm 500'" in relay
    assert log.count("meter focus 1 backup 13") == 2


def test_fn_back_to_af_measures_again_even_at_the_same_distance(tmp_path):
    """Snap set; Fn (AF/Snap toggle) gives AF with the lens still at 1330; AF hunts and lands at 1330 again."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 10, 103),
            (6, "U2", "2.8", 7001, 1330, 10, 105), (7, "U2", "2.8", 7001, 1330, 1, 106),
            (9, "U2", "2.8", 7001, 1330, 1, 108, "af"), (10, "U2", "2.8", 7001, 1330, 1, 109)]
    log, relay, patch = run_watch(tmp_path, rows, limit=12)
    lines = [l for l in log.splitlines() if l.startswith("meter ")]
    assert [l.split(" av")[0] for l in lines if "subject" in l or l.startswith("meter focus")] == [
        "meter subject 1330", "meter focus 1 backup 13", "meter subject 1330"]
    assert relay.count("StillFocus Snap'") == 2 and relay.count("StillBackUpFocus Select'") == 2


def test_back_from_a_hyperfocal_snap_waits_for_the_half_press(tmp_path):
    """The lens rests at the hyperfocal snap distance, not at the last subject: not a subject itself."""
    h = round(1000 / fb.half_zone(2.8))
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 30000, 1, 101),
            (3, "U2", "2.8", 7000, 30000, 1, 102), (4, "U2", "2.8", 7000, h, 10, 103),
            (6, "U2", "2.8", 7000, h, 1, 105), (9, "U2", "2.8", 7000, 2000, 1, 108)]
    log, relay, patch = run_watch(tmp_path, rows, limit=12)
    assert log.count("meter subject") == 2 and "meter subject 2000" in log
    assert f"meter subject {h}" not in log


def test_in_snap_a_moving_lens_is_not_measured(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 10, 103),
            (6, "U2", "2.8", 7000, 4000, 10, 105), (9, "U2", "2.8", 7000, 600, 10, 108)]
    log, relay, patch = run_watch(tmp_path, rows, limit=12)
    assert log.count("meter subject") == 1


def test_snap_is_read_back_and_only_the_missing_line_sent_again(tmp_path):
    """Runs 13 to 16: the camera stayed AF after an acknowledged batch. The cm and back-up stores took; Snap
    did not: the next batch is StillFocus Snap alone."""
    log, relay, patch = run_watch(tmp_path, snap_rows(), limit=7, drop=1)
    b = batches(relay)
    assert "StillSnapDistanceCm 150'" in b[0] and "StillBackUpFocus Select'" in b[0] and "StillFocus Snap'" in b[0]
    assert b[1] == "sendln 'UserData Set StillFocus Snap'\nmpause 300\n"
    assert "meter set 1 focus 1 backup 1 cm 150" in log and "meter set 2 focus 10 backup 1 cm 150" in log
    assert "snap failed" not in log and "meter tag GRSNAP:1330:1330" in log
    assert log.index("meter set 2") < log.index("meter tag")                # the tag only after Snap is there


def test_a_dropped_batch_is_sent_again_whole(tmp_path):
    log, relay, patch = run_watch(tmp_path, snap_rows(), limit=7, drop_all=1)
    b = batches(relay)
    assert b[0] == b[1] and "StillFocus Snap'" in b[1]
    assert "meter set 1 focus 1 backup 13 cm 250" in log and "meter set 2 focus 10 backup 1 cm 150" in log


def test_snap_gives_up_after_three_batches(tmp_path):
    log, relay, patch = run_watch(tmp_path, snap_rows(), limit=7, drop=5)
    assert relay.count("StillFocus Snap'") == 3 and "meter set 3 focus 1" in log and "meter snap failed" in log
    assert "GRSNAP" not in relay                                            # no tag without Snap
    assert log.count("meter subject") == 1                                  # the lens did not move again


def test_snap_the_camera_takes_back_is_sent_again(tmp_path):
    """Focus back to the AF mode with the back-up still the AF mode (not 13, as Fn writes): not the user."""
    log, relay, patch = run_watch(tmp_path, snap_rows(8), limit=9, revert=1)
    assert "meter focus 1 backup 1" in log and "meter snap lost 1" in log
    assert relay.count("StillFocus Snap'") == 2
    lost = log[log.index("meter snap lost 1"):]
    assert "meter set 1 focus 10 backup 1 cm 150" in lost
    assert batches(relay)[2] == "sendln 'UserData Set StillFocus Snap'\nmpause 300\n"   # Snap, tag, Snap again


def test_fn_back_to_af_is_not_taken_back(tmp_path):
    rows = snap_rows(4) + [(5, "U2", "2.8", 7000, 1330, 10, 105), (6, "U2", "2.8", 7000, 1330, 1, 106),
                           (7, "U2", "2.8", 7000, 1330, 1, 107), (8, "U2", "2.8", 7000, 1330, 1, 108)]
    log, relay, patch = run_watch(tmp_path, rows, limit=9)
    assert "meter focus 1 backup 13" in log and "snap lost" not in log and relay.count("StillFocus Snap'") == 1


def test_snap_that_came_is_not_sent_again(tmp_path):
    log, relay, patch = run_watch(tmp_path, snap_rows(8), limit=9)
    assert "meter set 2" not in log and relay.count("StillFocus Snap'") == 1


def test_snap_photos_carry_the_exact_distance_and_af_photos_do_not(tmp_path):
    """Copyright empty: GRSNAP:<snap>:<subject> in its own batch once Snap is read back; Fn back to AF clears
    it in one batch, once the lens has been quiet for two rounds."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 479, 1, 101),
            (3, "U2", "2.8", 7000, 479, 1, 102), (4, "U2", "2.8", 7000, 479, 10, 104),
            (5, "U2", "2.8", 7000, 479, 1, 106), (6, "U2", "2.8", 7000, 479, 1, 107),
            (7, "U2", "2.8", 7000, 479, 1, 108), (8, "U2", "2.8", 7000, 479, 1, 109)]
    log, relay, patch = run_watch(tmp_path, rows, limit=9)
    b = batches(relay)
    assert "StillFocus Snap'" in b[0] and "GRSNAP" not in b[0]
    assert "SetupCopyright GRSNAP:479:479'" in b[1] and "SetupCopyrightAttach On'" in b[1]
    assert "SetupCopyright x'" in b[2] and "SetupCopyrightAttach Off'" in b[2]   # attach byte 1: it was Off
    assert patch.splitlines()[:5] == ["M.BIN ", "CPTAG.BIN ", "CPTAG.BIN restore", "CPEMPTY.BIN ", "CPEMPTY.BIN restore"]
    assert "meter tag GRSNAP:479:479" in log and "tag cleared attach Off" in log
    assert log.index("meter focus 1 backup 13") < log.index("tag cleared")
    # The card marker: put with the tag (attach byte 1), deleted with the clear (PULL.SH does both).
    assert (tmp_path / "w" / "tag.put").read_text() == "1\n" and (tmp_path / "w" / "tag.del").exists()


def test_the_tag_stays_while_in_snap(tmp_path):
    log, relay, patch = run_watch(tmp_path, snap_rows(8), limit=9)
    assert "meter tag GRSNAP:1330:1330" in log and "tag cleared" not in log


def test_snap_meter_leaves_a_set_copyright_alone(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 479, 1, 101),
            (3, "U2", "2.8", 7000, 479, 1, 102), (4, "U2", "2.8", 7000, 479, 10, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5, cpy_first=0x41)
    assert "SetupCopyright" not in relay and "CPTAG" not in patch and "meter subject 479" in log


def test_the_label_shows_the_exact_distance(tmp_path):
    """0.479 m: the 0.3 m setting, both its labels (0.3m, 0.4m) read 48cm; restored on leaving."""
    log, relay, patch = run_watch(tmp_path, meter_rows(479), limit=6, lang=1)
    a, b = sm.LABEL_IDS[30]
    assert label_sites((tmp_path / "w" / "L.BIN").read_bytes()) == [
        (lbl_str(a), 0x002E0030, 0x00380034), (lbl_str(a) + 4, 0x006D0033, 0x006D0063),
        (lbl_str(b), 0x002E0030, 0x00380034), (lbl_str(b) + 4, 0x006D0034, 0x006D0063)]
    assert "meter subject 479 av 2.8 snap 479 slot 30 patch 0 label 48cm" in log
    assert "StillSnapDistanceCm 30'" in relay and "StillSnapDistanceCm 500'" not in relay   # 250 in use
    lines = patch.splitlines()
    assert lines.index("L.BIN ") < lines.index("M.BIN ") and "L.BIN restore" in lines


def test_the_label_writes_only_the_words_that_change(tmp_path):
    """2.95 m on 3.5 m: "3." stays, one word."""
    log, relay, patch = run_watch(tmp_path, meter_rows(2950), limit=6, lang=1)
    i = sm.LABEL_IDS[350][0]
    assert label_sites((tmp_path / "w" / "L.BIN").read_bytes()) == [(lbl_str(i) + 4, 0x006D0035, 0x006D0030)]
    assert "slot 350 patch 0 label 3.0m" in log


def test_a_three_character_label_gets_its_length(tmp_path):
    """Beyond the hyperfocal distance at f/2.8 (12.2 m): 12m, the length word 5 -> 4."""
    log, relay, patch = run_watch(tmp_path, meter_rows(30000), limit=6, lang=1)
    i = sm.LABEL_IDS[350][0]
    assert label_sites((tmp_path / "w" / "L.BIN").read_bytes()) == [
        (lbl_ent(i), 0x105, 0x104), (lbl_str(i), 0x002E0033, 0x00320031), (lbl_str(i) + 4, 0x006D0035, 0x0000006D)]
    assert "snap 12164 slot 350 patch 0 label 12m" in log


def test_a_stock_text_needs_no_patch(tmp_path):
    log, relay, patch = run_watch(tmp_path, meter_rows(1500), limit=6, lang=1)
    assert "slot 150 patch 0 label 1.5m" in log and "L.BIN" not in patch


def test_the_label_on_the_setting_in_use_is_redrawn(tmp_path):
    """2.4 m: 2.5 m, already the setting (250): 5 m first, so the widget sees a new id."""
    log, relay, patch = run_watch(tmp_path, meter_rows(2400), limit=5, lang=1)
    snap = next(b for b in relay.split("----\n") if "StillFocus Snap'" in b)
    assert snap.index("StillSnapDistanceCm 500'") < snap.index("StillSnapDistanceCm 250'") and "label 2.4m" in log
    assert "GRSNAP" not in snap and "SetupCopyright GRSNAP:2400:2400'" in relay


def test_without_the_label_chain_the_nearest_setting(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 1000, 1, 100), (2, "U2", "2.8", 7000, 2400, 1, 101),
            (3, "U2", "2.8", 7000, 2400, 1, 102), (4, "U2", "2.8", 7000, 2400, 10, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5)
    assert "meter label off" in log and "slot 250 patch 0 label none" in log and "L.BIN" not in patch


@pytest.mark.parametrize("mm,text", [(479, "48cm"), (200, "20cm"), (994, "99cm"), (995, "1.0m"), (3540, "3.5m"),
                                     (9949, "9.9m"), (9950, "10m"), (12164, "12m"), (65535, "66m")])
def test_label_text_is_at_most_four_characters(mm, text):
    fn = re.search(r"^lbltxt\(\) \{.*\}$", SCRIPT, re.M).group(0)
    out = subprocess.run(["sh", "-c", fn + f"\nlbltxt {mm}"], capture_output=True, text=True).stdout
    assert out == text and len(out) <= 4


def test_af_before_the_watcher_looked_at_power_on(tmp_path):
    """Power-on in U2 and AF done before the first look: the lens reads valid (1000000 until the first AF)."""
    rows = [(k, "U2", "2.8", 7000, 1330, 1, 20 + k) for k in range(1, 5)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5)
    assert "meter entry lens 1330 focus 1 backup 13 cm 250 pos 0 caf 1 samp 1" in log
    assert "meter subject before the start 1330" in log and log.count("meter subject 1330") == 1


def test_no_af_yet_at_power_on(tmp_path):
    rows = [(k, "U2", "2.8", 7000, 1000000, 1, 20 + k) for k in range(1, 4)] + [(4, "U2", "2.8", 7000, 1795, 1, 24)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "meter entry lens 4294967295" in log and "before the start" not in log
    assert "meter af 2." in log and log.count("meter subject 1795") == 1


def test_later_an_unmoved_lens_is_no_subject(tmp_path):
    log, relay, patch = run_watch(tmp_path, still_rows(1330), limit=5)
    assert "meter subject" not in log and "meter af" not in log


def test_a_press_during_a_batch_is_measured_next(tmp_path):
    """Run 17: half-pressed on a new subject while the Snap batch of the one before went out."""
    log, relay, patch = run_watch(tmp_path, snap_rows(6), limit=7, press=1, press_mm=700)
    assert "meter pressed during batch 700" in log
    subj = [l for l in log.splitlines() if l.startswith("meter subject")]
    assert subj[0].startswith("meter subject 1330") and subj[1].startswith("meter subject 700")
    assert "meter tag GRSNAP:1330" not in log and "meter tag GRSNAP:700:700" in log


def test_a_press_ending_with_the_batch_is_not_lost(tmp_path):
    """Run 18 (34.9 s, 57.1 s): Fn and a half-press during the tag batch; the AF ended just before the batch did,
    and the background watch was killed inside its lens check. It is now waited for."""
    log, relay, patch = run_watch(tmp_path, snap_rows(6), limit=7, press=1, press_mm=700, af_slow=1.5)
    assert "meter pressed during batch 700 waited" in log
    subj = [l for l in log.splitlines() if l.startswith("meter subject")]
    assert subj[1].startswith("meter subject 700")


def test_without_samp_the_peek_path_still_measures(tmp_path):
    log, relay, patch = run_watch(tmp_path, meter_rows(1330), limit=6, samp=False)
    assert log.count("meter samp off") == 1 and "samp 0" in log and "meter subject 1330" in log
    assert "meter set 1 focus 10 backup 1 cm 150" in log and "tron" not in log


def test_a_broken_samp_falls_back(tmp_path):
    log, relay, patch = run_watch(tmp_path, meter_rows(1330), limit=6, samp_broken=True)
    assert "meter samp off 0" in log and "meter subject 1330" in log


def test_the_meter_waits_in_samp(tmp_path):
    """AF: one wait per round on the action word (leaves 1), then the lens once it is back; Snap: the focus word."""
    log, relay, patch = run_watch(tmp_path, meter_rows(1330), limit=6)
    calls = [c.split() for c in (tmp_path / "samp.txt").read_text().splitlines() if c.startswith("w ")]
    caf = f"{card.CAF_OBJECT + sm.CAF_ACTION:x}"
    lens = f"{LC + p3.LENS_MM:x}"
    extras = [f"{OBJ + p3.OFF['snap']:x}", f"{OBJ + p3.OFF['pos']:x}", f"{OBJ + p3.OFF['focus']:x}"]
    af = [c for c in calls if c[1] == f"{sm.WAIT_MS:x}" and c[6] == caf]
    assert af and af[0][1:8] == [f"{sm.WAIT_MS:x}", "0", "0", "0", "0", caf, "1"] and af[0][8:] == extras
    still = [c for c in calls if c[3] == lens and c[5] == "1"]
    assert still and still[0][1:8] == [f"{500:x}", f"{sm.AF_STILL_MS:x}", lens, "0", "1", "0", "100"]
    assert any(c[6] == f"{OBJ + p3.OFF['focus']:x}" and c[7] == "a" for c in calls)   # Snap: the focus word
    assert "meter af 2." in log and "meter subject 1330" in log


def test_the_tracer_runs_per_visit_and_is_drained(tmp_path):
    log, relay, patch = run_watch(tmp_path, still_rows(2500, 12), limit=13)
    calls = (tmp_path / "samp.txt").read_text().splitlines()
    tracers = [c for c in calls if c.startswith("t ")]
    assert 1 < len(tracers) <= sm.TRACE_MAX and tracers[0].split()[1:3] == [f"{sm.TRACE_MS:x}", f"{sm.TRACE_RECORDS:x}"]
    assert "meter tron lens focus cm cur pos caf" in log and log.count("meter tron") == 1
    assert "tr 100.000 0 7 7" in log and "tr 101.005 1 7 7" in log
    assert not (tmp_path / "tr.dat").exists() and not (tmp_path / "tr.d").exists()


def test_meter_warns_when_fn_is_not_the_af_snap_toggle(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 2500, 1, 101)]
    log, relay, patch = run_watch(tmp_path, rows, limit=3, fn=0)
    assert "meter fn 0 not af snap toggle" in log


def test_meter_ignores_a_garbage_lens_reading(tmp_path):
    """Seen on the camera right after turning to U2: 536870893 mm."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 536870893, 1, 101),
            (3, "U2", "2.8", 7000, 536870893, 1, 102), (4, "U2", "2.8", 7000, 536870893, 1, 103)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5)
    assert "meter subject" not in log and patch == ""


def test_a_leftover_tag_on_the_meter_mode_waits_for_a_quiet_moment(tmp_path):
    """Power-on in U2: the loop starts at once; the tag is cleared after two quiet rounds."""
    log, relay, patch = run_watch(tmp_path, still_rows(2500, 5), cpy_first=0x47, limit=6, tag_file="1\n")
    assert log.index("meter entry") < log.index("tag cleared attach Off")
    assert "tag on" not in log and "tag off" not in log                      # the copyright read is our tag


def test_a_measurement_before_the_leftover_is_cleared_replaces_it(tmp_path):
    log, relay, patch = run_watch(tmp_path, snap_rows(8), cpy_first=0x47, limit=9, tag_file="1\n")
    assert "meter tag GRSNAP:1330:1330" in log and "tag cleared" not in log   # Snap photos keep the new tag
    assert not (tmp_path / "w" / "tag.put").exists()                         # the marker is still the original


def test_the_log_decoder_merges_the_tracer_by_time():
    lines = ["solo watch start macro U1 meter U2 1.0", "solo meter subject 1 2.0", "solo watch start macro U1 meter U2 8.32",
             "solo meter tron lens s1p s1 focus 20.5", "solo tr 20.400 3 851969 851969 21.0",
             "solo meter subject 893 av 2.8 snap 893 slot 150 patch 0 label 89cm 30.9",
             "solo tr 30.105 2 0 1 31.0", "solo tr 31.205 3 851969 65546 31.5", "ack 3 35.26"]
    got = fw.decode_log(lines)
    assert got == [(8.32, "watch start macro U1 meter U2"), (20.4, "  tr focus Select backup 13 (start)"),
                   (20.5, "meter tron lens s1p s1 focus"), (30.105, "  tr s1 0 -> 1"),
                   (30.9, "meter subject 893 av 2.8 snap 893 slot 150 patch 0 label 89cm"),
                   (31.205, "  tr focus Select backup 13 -> Snap backup Select")]
    assert len(fw.decode_log(lines, every=True)) == len(got) + 2
