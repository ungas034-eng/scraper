#!/usr/bin/env python3
"""
MaxPreps Scraper — Streamlit UI

Jalankan:
  pip install streamlit
  streamlit run app.py

Deploy:
  - Streamlit Community Cloud: hubungkan repo, main file = app.py
  - Lokal / VPS: streamlit run app.py --server.port 8501
  - PythonAnywhere: kurang ideal (bukan WSGI); lebih cocok Streamlit Cloud / VPS
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import streamlit as st

from maxpreps_scraper import (
    STATES,
    SPORTS,
    WATCH_LIVE_DEFAULT,
    OUTPUT_DIR,
    OUTPUT_FILE,
    TEMP_DIR,
    load_watch_map,
    save_watch_link,
    resolve_watch_url,
    list_dates,
    list_dates_with_fallback,
    scrape_state,
    scrape_states_parallel,
    temp_output_path,
    write_blocks,
    state_file,
    all_states_file,
    to_mdy,
    format_tanggal,
    list_schedule_files,
    safe_schedule_path,
    DEFAULT_STATE_WORKERS,
    DEFAULT_GAME_WORKERS,
    ON_PYTHONANYWHERE,
)

st.set_page_config(
    page_title="MaxPreps Scraper",
    page_icon="🏈",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ---------- session defaults ----------
def _init_state():
    defaults = {
        "dates": [],
        "dates_source": "",
        "preview": "",
        "outfile": "",
        "outfile_bytes": None,
        "outfile_name": "",
        "last_flash": "",
        "last_error": "",
        "editor_content": "",
        "editor_file": "",
        "editor_msg": "",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


def _sport_options():
    return {v[0]: k for k, v in SPORTS.items()}


def _state_options():
    opts = {"ALL STATES": "all"}
    for code, name in STATES.items():
        opts[f"{name} ({code.upper()})"] = code
    return opts


# ---------- sidebar ----------
with st.sidebar:
    st.title("🏈 MaxPreps")
    st.caption("Scrape · Game Info · Download")
    page = st.radio("Menu", ["Scrape", "Edit TXT"], label_visibility="collapsed")
    st.divider()
    st.markdown(
        f"<small>Worker: state={DEFAULT_STATE_WORKERS} · game={DEFAULT_GAME_WORKERS}"
        f"{' · PA mode' if ON_PYTHONANYWHERE else ''}</small>",
        unsafe_allow_html=True,
    )
    st.markdown('<small>scraper by <b>Ridwan</b></small>', unsafe_allow_html=True)


# ===================== SCRAPE PAGE =====================
if page == "Scrape":
    st.header("Scrape jadwal")

    col1, col2 = st.columns(2)
    sport_labels = _sport_options()
    state_labels = _state_options()

    with col1:
        sport_label = st.selectbox("Sport", list(sport_labels.keys()), index=0)
        sport = sport_labels[sport_label]
    with col2:
        # default Texas
        state_keys = list(state_labels.keys())
        default_idx = next((i for i, k in enumerate(state_keys) if state_labels[k] == "tx"), 1)
        state_label = st.selectbox("State", state_keys, index=default_idx)
        state = state_labels[state_label]

    mapping = load_watch_map()
    default_watch = (
        mapping.get(f"{state}/{sport}")
        or mapping.get(state)
        or mapping.get("default")
        or WATCH_LIVE_DEFAULT
    )
    watch = st.text_input("📺 Watch live URL", value=default_watch, placeholder="https://prepwire.com/live/texas")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        add_title = st.checkbox("Tambah ?title=", value=True, help="?title=TeamA Vs. TeamB di watch URL")
    with c2:
        split_state = st.checkbox("Simpan ke schedules/", value=True, help="Juga tulis file permanent")
    with c3:
        overwrite = st.checkbox("Timpa file", value=False)
    with c4:
        mascots = st.checkbox("Ambil mascot", value=False, help="Lebih lambat (fetch halaman game)")

    st.divider()

    # --- cek tanggal ---
    if st.button("📅 Cek tanggal", use_container_width=True):
        st.session_state.last_error = ""
        st.session_state.last_flash = ""
        with st.spinner("Mengambil kalender MaxPreps…"):
            try:
                if state == "all":
                    code, url, dates, probe = list_dates_with_fallback(sport)
                    who = f"ALL STATES (acuan {STATES.get(probe, probe)})"
                else:
                    code, url, dates = list_dates(state, sport)
                    who = f"{STATES.get(state, state)} / {SPORTS[sport][0]}"
                st.session_state.dates = dates
                st.session_state.dates_source = f"HTTP {code} · {url}"
                if code != 200:
                    st.session_state.last_error = (
                        f"Gagal ambil kalender (HTTP {code}). "
                        "Cek koneksi / whitelist maxpreps.com / VPN."
                    )
                elif dates:
                    st.session_state.last_flash = f"{len(dates)} tanggal punya game — {who}."
                else:
                    st.session_state.last_error = "Kalender kosong (off-season atau diblokir)."
            except Exception as ex:
                st.session_state.last_error = f"{type(ex).__name__}: {ex}"

    if st.session_state.last_flash:
        st.success(st.session_state.last_flash)
    if st.session_state.last_error:
        st.error(st.session_state.last_error)
    if st.session_state.dates_source:
        st.caption(st.session_state.dates_source)

    dates = st.session_state.dates
    selected_mdy = None
    if dates:
        labels = [f"{d}  ·  {format_tanggal(d)}  ·  {c} game{'s' if c != 1 else ''}" for d, c in dates]
        pick = st.radio("Tanggal tersedia (hari ini & mendatang)", labels, index=0)
        selected_mdy = dates[labels.index(pick)][0]
    else:
        st.info("Klik **Cek tanggal** dulu untuk melihat hari yang ada pertandingan.")

    st.divider()

    if st.button("🚀 Scrape tanggal terpilih", type="primary", use_container_width=True, disabled=not selected_mdy):
        st.session_state.last_error = ""
        st.session_state.last_flash = ""
        st.session_state.preview = ""
        st.session_state.outfile = ""
        st.session_state.outfile_bytes = None

        if not selected_mdy:
            st.session_state.last_error = "Pilih tanggal dulu."
        else:
            mdy = to_mdy(selected_mdy)
            if watch:
                save_watch_link(watch, "default" if state == "all" else state, sport)

            is_all = state == "all"
            gi_limit = (12 if mascots else 8) if is_all else (40 if mascots else 30)
            if ON_PYTHONANYWHERE:
                gi_limit = min(gi_limit, 15)
            st_workers = DEFAULT_STATE_WORKERS if is_all else 1
            g_workers = DEFAULT_GAME_WORKERS

            progress = st.progress(0, text="Mulai scrape…")
            status = st.empty()

            try:
                if is_all:
                    status.info(f"Scrape {len(STATES)} state (paralel {st_workers} worker)…")
                    progress.progress(10, text="Mengambil semua state…")
                    all_blocks, failed, last_code, last_url, last_blocks = scrape_states_parallel(
                        list(STATES.keys()), sport, mdy, watch,
                        mascots, gi_limit, False, add_title,
                        with_game_info=True, game_info_limit=gi_limit,
                        state_workers=st_workers, game_workers=g_workers,
                    )
                    progress.progress(80, text="Menulis file…")
                    dest = temp_output_path("all", sport, mdy)
                    written, _ = write_blocks(dest, all_blocks, overwrite=True)
                    if split_state or overwrite:
                        write_blocks(all_states_file(sport, mdy), all_blocks, overwrite=overwrite)
                    total_matches = len(all_blocks)
                    st.session_state.preview = "".join(all_blocks[:8])
                    st.session_state.outfile = dest
                    msg = (
                        f"Selesai · {total_matches} match → `{os.path.basename(dest)}` "
                        f"(written {written})"
                    )
                    if failed:
                        st.session_state.last_error = "Gagal: " + ", ".join(failed[:12])
                    if total_matches == 0 and not failed:
                        st.session_state.last_error = "Tidak ada match yang diambil."
                    st.session_state.last_flash = msg
                else:
                    status.info(f"Scrape {STATES.get(state, state)}…")
                    progress.progress(20, text="Mengambil halaman skor…")
                    wurl = resolve_watch_url(watch, load_watch_map(), state, sport)
                    code, url, blocks = scrape_state(
                        state, sport, mdy, wurl or WATCH_LIVE_DEFAULT,
                        mascots, gi_limit, False, add_title=add_title,
                        with_game_info=True, game_info_limit=gi_limit,
                        game_workers=g_workers,
                    )
                    progress.progress(80, text="Menulis file…")
                    if code != 200:
                        st.session_state.last_error = (
                            f"Scrape gagal HTTP {code}. "
                            "Cek koneksi / whitelist maxpreps.com."
                        )
                    else:
                        dest = temp_output_path(state, sport, mdy)
                        written, _ = write_blocks(dest, blocks, overwrite=True)
                        if split_state:
                            write_blocks(state_file(state, sport), blocks, overwrite=overwrite)
                        else:
                            write_blocks(OUTPUT_FILE, blocks, overwrite=overwrite)
                        st.session_state.preview = "".join(blocks[:8])
                        st.session_state.outfile = dest
                        st.session_state.last_flash = (
                            f"Selesai · {len(blocks)} match → `{os.path.basename(dest)}` "
                            f"(written {written})"
                        )
                        if not blocks:
                            st.session_state.last_error = "Parser tidak menemukan game card."

                # siapkan bytes untuk download button
                if st.session_state.outfile and os.path.isfile(st.session_state.outfile):
                    with open(st.session_state.outfile, "rb") as fh:
                        st.session_state.outfile_bytes = fh.read()
                    st.session_state.outfile_name = os.path.basename(st.session_state.outfile)

                progress.progress(100, text="Selesai")
            except Exception as ex:
                st.session_state.last_error = f"{type(ex).__name__}: {ex}"
            finally:
                progress.empty()
                status.empty()

    if st.session_state.last_flash:
        st.success(st.session_state.last_flash)
    if st.session_state.last_error and page == "Scrape":
        # already shown above for dates; show again after scrape if needed
        if "HTTP" in st.session_state.last_error or "Gagal" in st.session_state.last_error or "Parser" in st.session_state.last_error or "match" in st.session_state.last_error.lower():
            st.error(st.session_state.last_error)

    if st.session_state.outfile_bytes:
        st.download_button(
            label="⬇️ Download hasil (.txt)",
            data=st.session_state.outfile_bytes,
            file_name=st.session_state.outfile_name or "schedules.txt",
            mime="text/plain",
            type="primary",
            use_container_width=True,
        )
        st.caption(f"File sementara: `{st.session_state.outfile}`")

    if st.session_state.preview:
        with st.expander("Preview (8 match pertama)", expanded=True):
            st.code(st.session_state.preview, language=None)


# ===================== EDITOR PAGE =====================
else:
    st.header("Edit file TXT")
    files = list_schedule_files()
    # juga tampilkan tmp_out
    if os.path.isdir(TEMP_DIR):
        for name in sorted(os.listdir(TEMP_DIR)):
            if name.endswith(".txt"):
                p = os.path.join(TEMP_DIR, name)
                if p not in files:
                    files.append(p)

    if not files:
        st.warning("Belum ada file schedules. Lakukan scrape dulu.")
    else:
        labels = [os.path.basename(f) for f in files]
        choice = st.selectbox("Pilih file", labels)
        path = files[labels.index(choice)]

        if st.button("📂 Buka file") or st.session_state.editor_file != path:
            try:
                with open(path, encoding="utf-8") as fh:
                    st.session_state.editor_content = fh.read()
                st.session_state.editor_file = path
                st.session_state.editor_msg = f"Dibuka: {os.path.basename(path)} ({len(st.session_state.editor_content)} karakter)"
            except Exception as ex:
                st.error(str(ex))

        if st.session_state.editor_msg:
            st.info(st.session_state.editor_msg)

        st.subheader("Find & Replace")
        fc1, fc2 = st.columns(2)
        with fc1:
            find = st.text_input("Cari")
        with fc2:
            repl = st.text_input("Ganti dengan")
        case_sens = st.checkbox("Case sensitive")

        bc1, bc2 = st.columns(2)
        with bc1:
            if st.button("Replace all", use_container_width=True):
                if not find:
                    st.warning("Isi kata yang dicari.")
                elif not st.session_state.editor_file:
                    st.warning("Buka file dulu.")
                else:
                    content = st.session_state.editor_content or ""
                    if case_sens:
                        count = content.count(find)
                        new_content = content.replace(find, repl)
                    else:
                        pattern = re.compile(re.escape(find), re.IGNORECASE)
                        count = len(pattern.findall(content))
                        new_content = pattern.sub(repl, content)
                    st.session_state.editor_content = new_content
                    with open(st.session_state.editor_file, "w", encoding="utf-8") as fh:
                        fh.write(new_content)
                    st.success(f"{count} kemunculan diganti di {os.path.basename(st.session_state.editor_file)}")
        with bc2:
            if st.button("💾 Simpan manual", use_container_width=True):
                if st.session_state.editor_file:
                    # content from text area below is bound via session on next run;
                    # use current session content
                    with open(st.session_state.editor_file, "w", encoding="utf-8") as fh:
                        fh.write(st.session_state.editor_content or "")
                    st.success(f"Disimpan: {os.path.basename(st.session_state.editor_file)}")

        new_text = st.text_area(
            "Isi file",
            value=st.session_state.editor_content,
            height=400,
            key="editor_area",
        )
        # sync textarea edits back
        if new_text != st.session_state.editor_content:
            st.session_state.editor_content = new_text

        if st.session_state.editor_content:
            st.download_button(
                "⬇️ Download file ini",
                data=st.session_state.editor_content.encode("utf-8"),
                file_name=os.path.basename(st.session_state.editor_file or "edit.txt"),
                mime="text/plain",
            )
