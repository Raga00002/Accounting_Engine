"""New Bill page: capture (photos / voice / typed) -> read ALL at once -> tabular review -> save.

Batch model: every read bill becomes a *card* (draft + image + frozen items frame). All cards are
shown together – a summary table on top, then one review card per bill – and can be saved one by
one or all together. Voice / typed / manual entries are single-card batches.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from engine import config, db, extractor, service, speech, tally_export
from engine.extractor import ExtractionError
from engine.models import Bill, BillDraft, Party
from engine.speech import SpeechError
from engine.tally_import import canon_unit
from ui.common import ITEM_COLUMN_CONFIG, df_to_items, download_buttons, items_to_df, money, remember, remembered, toast_ok

NEW_PARTY = "__new__"
PICK_OTHER = "__pick__"
ADD_NEW = "__add_new__"
USE_EXISTING = "__use_existing__"
UNITS = ["Bag", "Pkt", "Tin", "Kg", "Ltr", "Qtl", "Pcs", "Box", "Nos", "Set"]
_CACHE_MAX = 64


# --------------------------------------------------------------------------- #
# session helpers
# --------------------------------------------------------------------------- #
@dataclass
class CardState:
    """What the widgets of one review card currently say (recomputed on every rerun)."""
    party: Optional[Party] = None
    bill_date: Optional[date] = None
    bill_no: str = ""
    amount: Optional[float] = None
    decisions: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    computed_total: Optional[float] = None
    match_kind: str = ""

    @property
    def can_save(self) -> bool:
        return self.party is not None and not self.missing and self.amount is not None


def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("nb_cards", [])  # list[dict]: draft, image, items_df, k, saved (Bill|None), error, state
    ss.setdefault("nb_version", 0)
    ss.setdefault("nb_queue", [])  # uploaded (name, bytes) not yet read
    ss.setdefault("nb_seen_fps", {})
    ss.setdefault("nb_transcript", "")
    ss.setdefault("nb_match_cache", {})
    ss.setdefault("nb_item_cache", {})
    ss.setdefault("nb_saved_ids", [])  # bills saved in this batch -> Tally files offered together
    ss.setdefault("nb_show_input", True)


def _cache_get(cache: dict, key, make):
    if key not in cache:
        if len(cache) >= _CACHE_MAX:
            cache.pop(next(iter(cache)))
        cache[key] = make()
    return cache[key]


def _new_card(draft: BillDraft, image: tuple[bytes, str] | None, error: str | None = None) -> dict:
    ss = st.session_state
    ss.nb_version += 1
    if error is None:
        service.normalise_items(draft.items)
    return {
        "draft": draft, "image": image, "items_df": items_to_df(draft.items), "k": f"c{ss.nb_version}",
        "saved": None, "error": error, "state": None,
        "label": (image[1] if image else {"voice": "voice", "text": "typed", "manual": "manual"}.get(draft.source, draft.source)),
    }


def _add_cards(cards: list[dict]) -> None:
    ss = st.session_state
    ss.nb_cards.extend(cards)
    ss.nb_show_input = False
    st.rerun()


def _reset_all() -> None:
    ss = st.session_state
    ss.nb_cards = []
    ss.nb_match_cache = {}
    ss.nb_item_cache = {}
    ss.nb_saved_ids = []
    ss.nb_show_input = True
    ss.nb_version += 1


def _dequeue(name: str, data: bytes | None = None) -> None:
    ss = st.session_state
    keep = []
    for n, d in ss.nb_queue:
        if n == name and (data is None or d == data):
            ss.nb_seen_fps.pop((n, len(d)), None)
            continue
        keep.append((n, d))
    ss.nb_queue = keep


def _read_files(files: list[tuple[str, bytes]]) -> list[dict]:
    """Extract every file; a failure becomes an error card instead of aborting the batch."""
    parties, items = service.known_names()
    cards: list[dict] = []
    prog = st.progress(0.0, text="Reading bills…")
    for i, (name, data) in enumerate(files, start=1):
        prog.progress((i - 1) / max(1, len(files)), text=f"Reading {i}/{len(files)}: {name}")
        try:
            draft = extractor.extract_from_image(data, name, parties, items, mock_key=Path(name).stem)
            cards.append(_new_card(draft, (data, name)))
        except ExtractionError as e:
            cards.append(_new_card(BillDraft(source="image"), (data, name), error=str(e)))
        except Exception as e:  # noqa: BLE001
            cards.append(_new_card(BillDraft(source="image"), (data, name), error=f"Unexpected error: {e}"))
    prog.progress(1.0, text=f"Read {len(files)} bill(s).")
    return cards


def _read_text(text: str, source: str) -> dict:
    parties, items = service.known_names()
    with st.spinner("Understanding the bill…"):
        draft = extractor.extract_from_text(text, parties, items, source=source)
    return _new_card(draft, None)


# --------------------------------------------------------------------------- #
# stage 1: input
# --------------------------------------------------------------------------- #
def _input_stage() -> None:
    from engine import runtime

    cust = st.session_state.get("ae_cust") or {}
    is_admin = st.session_state.get("ae_is_admin", True)
    if cust.get("welcome_text"):
        st.markdown(cust["welcome_text"])
    if config.EXTRACTOR_MODE == "mock" and is_admin:
        st.info("Extractor is in **mock** mode (AE_EXTRACTOR_MODE=mock) – sample bills return canned results. "
                "Set AE_EXTRACTOR_MODE=live and ANTHROPIC_API_KEY in .env for real reading.", icon="🧪")
    elif config.EXTRACTOR_MODE != "mock" and not runtime.has_api_key():
        st.warning(("No Claude API key is configured – ask the administrator, or add your own key under My Account. "
                    if runtime.current().user_id else
                    "ANTHROPIC_API_KEY is not set – photo and voice reading will fail until you add it to .env. ")
                   + "Manual entry still works.", icon="🔑")

    voice_on = (cust.get("features") or {}).get("voice", True)
    if voice_on:
        tab_photo, tab_voice, tab_type = st.tabs(["📷 Photos / Upload", "🎤 Voice", "⌨️ Type / Manual"])
    else:
        tab_photo, tab_type = st.tabs(["📷 Photos / Upload", "⌨️ Type / Manual"])
        tab_voice = None

    with tab_photo:
        ss = st.session_state
        col_u, col_c = st.columns([3, 2])
        with col_u:
            files = st.file_uploader(
                "Bill photo(s) or PDF – select as many as you like", type=["jpg", "jpeg", "png", "webp", "pdf"],
                accept_multiple_files=True, key=f"nb_upl_{ss.nb_version}",
            )
        with col_c:
            use_cam = st.toggle("Use camera", value=False, key="nb_use_cam")
            shot = st.camera_input("Take a photo of the bill", key=f"nb_cam_{ss.nb_version}") if use_cam else None
            if shot is not None and st.button("➕ Add photo to the batch", key=f"nb_cam_add_{ss.nb_version}"):
                data = shot.getvalue()
                ss.nb_cam_n = ss.get("nb_cam_n", 0) + 1  # a counter, not len(queue): removing a file must not reuse a name
                name = f"camera_{ss.nb_cam_n}.jpg"
                ss.nb_queue.append((name, data))
                ss.nb_seen_fps[(name, len(data))] = -1
                ss.nb_version += 1  # clears the camera widget so the same shot is not added twice
                st.rerun()

        # Copy new uploads into the queue (the widget forgets them once we leave this stage).
        current_fps = {(f.name, f.size) for f in files or []}
        for f in files or []:
            fp = (f.name, f.size)
            if fp not in ss.nb_seen_fps:
                ss.nb_seen_fps[fp] = ss.nb_version
                ss.nb_queue.append((f.name, f.getvalue()))
        for fp, ver in list(ss.nb_seen_fps.items()):  # removed with the uploader's ✕ -> leaves the queue
            if ver == ss.nb_version and fp not in current_fps:
                ss.nb_queue = [(n, d) for n, d in ss.nb_queue if (n, len(d)) != fp]
                del ss.nb_seen_fps[fp]

        pending = ss.nb_queue
        if pending:
            st.markdown(f"**{len(pending)} bill(s) ready to read:** " + " · ".join(n for n, _ in pending))
            thumbs = st.columns(min(6, len(pending)))
            for i, (n, d) in enumerate(pending[:12]):
                with thumbs[i % len(thumbs)]:
                    if n.lower().endswith(".pdf"):
                        st.caption(f"📄 {n}")
                    else:
                        try:
                            st.image(d, caption=n, width=150)
                        except Exception:  # noqa: BLE001
                            st.caption(f"{n} – preview not available")
            b1, b2 = st.columns([2, 1])
            if b1.button(f"🔍 Read all {len(pending)} bill(s)", type="primary", key=f"nb_read_all_{ss.nb_version}"):
                cards = _read_files(list(pending))
                ss.nb_queue = []
                ss.nb_seen_fps = {}
                _add_cards(cards)
            if b2.button("🗑️ Clear list", key=f"nb_clear_q_{ss.nb_version}"):
                ss.nb_queue = []
                ss.nb_seen_fps = {}
                ss.nb_version += 1
                st.rerun()
        else:
            st.caption("Upload one or many bill photos, then click *Read all*. Every bill is read and shown in a table for review.")

    if tab_voice is not None:
        with tab_voice:
            lang_label = st.selectbox("Language", list(config.SPEECH_LANGUAGES), index=0, key="nb_lang")
            lang = config.SPEECH_LANGUAGES[lang_label]
            st.caption("Say it naturally, e.g. *\"Das Store, 5 bag BPT rice at 1540, 2 bag Sri rice 52 kilo at 5550 per quintal, bill number 13, today.\"*")
            try:
                from streamlit_mic_recorder import mic_recorder

                audio = mic_recorder(start_prompt="🎙️ Start recording", stop_prompt="⏹️ Stop", format="wav",
                                     just_once=True, use_container_width=False, key="nb_mic")
            except ImportError:
                audio = None
                st.warning("Microphone component not installed (pip install streamlit-mic-recorder). You can upload a WAV file instead.")
            if audio and audio.get("bytes"):
                try:
                    st.session_state.nb_transcript = speech.transcribe(
                        audio["bytes"], audio.get("sample_rate", 16000), audio.get("sample_width", 2), lang
                    )
                    toast_ok("Transcribed – check the text below, then parse.")
                except SpeechError as e:
                    st.error(str(e))
            up = st.file_uploader("…or upload a WAV / FLAC recording", type=["wav", "flac", "aiff"], key=f"nb_audio_{st.session_state.nb_version}")
            if up is not None and st.button("Transcribe file", key="nb_transcribe_file"):
                try:
                    st.session_state.nb_transcript = speech.transcribe_file(up.getvalue(), lang)
                except SpeechError as e:
                    st.error(str(e))
            transcript = st.text_area("Transcript (edit if needed)", key="nb_transcript", height=120)
            if st.button("🧠 Parse transcript into a bill", type="primary", key="nb_parse_voice", disabled=not (transcript or "").strip()):
                try:
                    _add_cards([_read_text(transcript, "voice")])
                except ExtractionError as e:
                    st.error(str(e))
                except Exception as e:  # noqa: BLE001
                    st.error(f"Unexpected error while parsing the transcript: {e}")


    with tab_type:
        typed = st.text_area("Describe the bill in plain words", height=140, key="nb_typed",
                             placeholder="P. Roy – 20 bag RR rice 5.20 qtl @ 4090, 10 bag RR10 1.00 qtl @ 4290, 15 pkt mung dal 4.50 qtl @ 9550. Bill 02 dated 26-09-2026")
        c1, c2 = st.columns(2)
        if c1.button("🧠 Parse text into a bill", type="primary", key="nb_parse_text", disabled=not (typed or "").strip()):
            try:
                _add_cards([_read_text(typed, "text")])
            except ExtractionError as e:
                st.error(str(e))
            except Exception as e:  # noqa: BLE001
                st.error(f"Unexpected error while parsing the text: {e}")
        if c2.button("📝 Start a blank bill (manual entry)", key="nb_blank"):
            _add_cards([_new_card(BillDraft(source="manual", bill_date=date.today()), None)])


# --------------------------------------------------------------------------- #
# stage 2: review sections (all keyed by the card key ``k``)
# --------------------------------------------------------------------------- #
def _party_section(card: dict) -> tuple[Party | None, str]:
    draft: BillDraft = card["draft"]
    k = card["k"]
    st.markdown("**1 · Party**")
    ck = (k, draft.party_name_raw or "", db.parties_fingerprint())
    res = _cache_get(st.session_state.nb_match_cache, ck, lambda: service.resolve_party(draft))
    raw = draft.party_name_raw or ""
    options: list[str] = []
    labels: dict[str, str] = {}
    for c in res.candidates:
        key = str(c.party.id)
        options.append(key)
        via = f" (via alias “{c.matched_on}”)" if c.matched_on != c.party.name else ""
        labels[key] = f"{c.party.name} — {c.party.group_name}{via} · match {c.score:.0f}%"
    options.append(NEW_PARTY)
    labels[NEW_PARTY] = f"➕ New party: “{raw}”" if raw else "➕ New party"
    options.append(PICK_OTHER)
    labels[PICK_OTHER] = "🔍 Choose another existing party"

    if res.kind == "auto":
        default_idx: int | None = 0
        st.success(f"Matched **{res.best.name}** from the database for “{raw}”.", icon="✅")
    elif res.kind == "new":
        default_idx = options.index(NEW_PARTY)
        st.warning(f"“{raw}” is not in the database – add it below (it will be saved for next time).", icon="🆕") if raw \
            else st.warning("No party name was found – choose or add one.", icon="❓")
    else:
        default_idx = None  # a wrong pre-selection would book the bill to the wrong party
        st.warning(f"“{raw}” is not an exact match – please confirm the party.", icon="❓")

    pkey = f"nb_party_{k}"
    prev = remembered(card, pkey, None)
    choice = st.radio("Party", options, index=options.index(prev) if prev in options else default_idx,
                      format_func=lambda x: labels[x], key=pkey)
    remember(card, pkey, choice)
    if choice is None or choice not in labels:
        return None, res.kind

    if choice == PICK_OTHER:
        parties = db.list_parties()
        if not parties:
            st.info("No parties in the database yet.")
            return None, res.kind
        by_id = {p.id: f"{p.name} — {p.group_name}" for p in parties}
        ids = list(by_id)
        skey = f"nb_pick_party_{k}"
        prev = remembered(card, skey, None)
        pid = st.selectbox("Existing party", ids, key=skey, index=ids.index(prev) if prev in by_id else None,
                           placeholder="Type to search…", format_func=lambda i: by_id.get(i, str(i)))
        remember(card, skey, pid)
        if pid not in by_id:
            return None, res.kind
        p = db.get_party(pid)
        _party_caption(p)
        return p, res.kind

    if choice == NEW_PARTY:
        with st.container(border=True):
            c1, c2 = st.columns(2)
            f = lambda name: f"nb_np_{name}_{k}"  # noqa: E731
            groups = ["Sundry Debtors", "Sundry Creditors"]
            name = remember(card, f("name"), c1.text_input("Party name *", value=remembered(card, f("name"), raw), key=f("name")))
            g0 = remembered(card, f("group"), service.default_group(draft.txn_type))
            group = remember(card, f("group"), c2.selectbox("Group", groups, index=groups.index(g0) if g0 in groups else 0, key=f("group")))
            address = remember(card, f("addr"), st.text_area("Address (one line per row)", value=remembered(card, f("addr"), ""),
                                                             key=f("addr"), height=80))
            c3, c4, c5 = st.columns(3)
            state = remember(card, f("state"), c3.text_input("State", value=remembered(card, f("state"), config.DEFAULT_STATE), key=f("state")))
            country = remember(card, f("country"), c4.text_input("Country", value=remembered(card, f("country"), config.DEFAULT_COUNTRY), key=f("country")))
            pincode = remember(card, f("pin"), c5.text_input("Pincode", value=remembered(card, f("pin"), ""), key=f("pin")))
            c6, c7 = st.columns([2, 1])
            ob = remember(card, f("ob"), c6.number_input("Opening balance (optional)", min_value=0.0, step=100.0,
                                                         value=float(remembered(card, f("ob"), 0.0)), key=f("ob")))
            dc0 = remembered(card, f("obdc"), "Dr" if group == "Sundry Debtors" else "Cr")
            ob_drcr = remember(card, f("obdc"), c7.selectbox("Dr/Cr", ["Dr", "Cr"], index=0 if dc0 == "Dr" else 1, key=f("obdc")))
        if not name.strip():
            st.error("Party name is required.")
            return None, res.kind
        existing = db.get_party_by_name(name)
        if existing:
            st.warning(f"“{existing.name}” already exists – it will be used (details above are ignored).")
            return existing, res.kind
        return Party(name=name.strip(), group_name=group, address=address.strip() or None,
                     state=state.strip() or None, country=country.strip() or None, pincode=pincode.strip() or None,
                     opening_balance=ob if ob else None, opening_drcr=ob_drcr if ob else None), res.kind

    p = db.get_party(int(choice))
    _party_caption(p)
    return p, res.kind


def _party_caption(p: Party | None) -> None:
    if not p:
        return
    bits = [p.group_name]
    if p.address:
        bits.append(p.address.replace("\n", ", "))
    if p.state:
        bits.append(p.state)
    if p.pincode:
        bits.append(str(p.pincode))
    if p.opening_balance is not None:
        bits.append(f"Opening {money(p.opening_balance)} {p.opening_drcr or ''}")
    st.caption(" · ".join(bits))


def _auto_number_cb(key: str) -> None:
    st.session_state[key] = db.next_bill_no(config.BILL_NO_PREFIX)


def _details_section(card: dict) -> tuple[date, str]:
    draft: BillDraft = card["draft"]
    k = card["k"]
    st.markdown("**2 · Bill details**")
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        dkey = f"nb_date_{k}"
        d = remember(card, dkey, st.date_input("Bill date *", value=remembered(card, dkey, draft.bill_date or date.today()),
                                               key=dkey, format="DD/MM/YYYY"))
        if draft.bill_date is None and draft.source != "manual":
            st.caption("⚠️ No date found on the bill – defaulted to today. Please confirm.")
    with c2:
        key = f"nb_billno_{k}"
        if key not in st.session_state:
            st.session_state[key] = remembered(card, key, draft.bill_no or "")
        bill_no = remember(card, key, st.text_input("Bill number *", key=key))
        if not (draft.bill_no or "").strip() and draft.source != "manual":
            st.caption("⚠️ No bill number found on the bill.")
        st.button("Auto-number", key=f"nb_autonum_{k}", on_click=_auto_number_cb, args=(key,),
                  help=f"Next sequential number like {config.BILL_NO_PREFIX}/2026-27/001")
    with c3:
        # draft.txn_type is written back below, so it already carries the user's last choice
        txn = st.radio("Type", ["sale", "purchase"], index=0 if draft.txn_type == "sale" else 1, horizontal=True,
                       key=f"nb_txn_{k}", format_func=lambda s: "Sale (party owes us → Dr)" if s == "sale" else "Purchase (we owe party → Cr)")
        draft.txn_type = txn  # type: ignore[assignment]
    return d, bill_no


def _items_section(card: dict) -> None:
    draft: BillDraft = card["draft"]
    st.markdown("**3 · Items**")
    st.caption("Fix anything the reader got wrong. Blank *Amount* is calculated as weight × rate (or qty × rate). Tick *Struck* for crossed-out lines.")
    ekey = f"nb_items_{card['k']}"
    if card.get("items_df") is None or ekey not in st.session_state:
        # No editor state (first render, or it was dropped while another page was shown): start the
        # editor from the draft's items, which carry the user's last edits.
        card["items_df"] = items_to_df(draft.items)
    # The editor must receive the *same* base frame on every rerun while it is alive; feeding the
    # edited result back in changes the widget identity and silently drops every second edit.
    df = st.data_editor(card["items_df"], num_rows="dynamic", width="stretch", hide_index=True,
                        column_config=ITEM_COLUMN_CONFIG, key=ekey)
    draft.items = df_to_items(df)
    # Total line directly under the table so the user can tick it against the bill in one glance.
    from engine import validator as _val

    probe = draft.model_copy(deep=True)
    _val.fill_missing_amounts(probe)
    total = probe.computed_total
    n_active = len(probe.active_items)
    written = draft.total_written
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Items total ({n_active} line{'s' if n_active != 1 else ''}): {money(total)}**")
    c2.markdown(f"Written on bill: **{money(written)}**")
    if total is not None and written is not None:
        diff = round(written - total, 2)
        c3.markdown("✅ **matches**" if abs(diff) <= 1 else f"⚠️ difference **{money(diff)}**")
    elif total is None:
        c3.markdown("⚠️ a line has no amount")


def _item_check_section(card: dict) -> tuple[dict[str, dict], list[str]]:
    """Check every item description against the items master. Returns (decisions, unresolved names)."""
    draft: BillDraft = card["draft"]
    k = card["k"]
    st.markdown("**3b · Item check**")
    ck = (k, tuple(sorted({it.item.strip().lower() for it in draft.items if it.item.strip() and not it.struck_out})),
          db.items_fingerprint())
    resolutions, all_names = _cache_get(
        st.session_state.nb_item_cache, ck, lambda: (service.resolve_items(draft), [i["name"] for i in db.list_items()])
    )
    decisions: dict[str, dict] = {}
    unresolved: list[str] = []
    if not resolutions:
        st.caption("No items yet.")
        return decisions, unresolved
    known = [r for r in resolutions if r.kind == "known"]
    todo = [r for r in resolutions if r.kind != "known"]
    if known:
        bits = [f"**{r.raw}**" + (f" → {r.canonical}" if r.canonical and r.canonical != r.raw else "") for r in known]
        st.success("Known items: " + " · ".join(bits), icon="✅")
        for r in known:
            decisions[r.raw] = {"action": "use", "name": r.canonical or r.raw}
    if not todo:
        return decisions, unresolved
    st.warning(f"{len(todo)} item description(s) are not in the items master – confirm each one:", icon="🆕")
    all_name_set = set(all_names)
    for r in todo:
        key = f"nb_ic_{k}_{hashlib.md5(r.raw.lower().encode('utf-8'), usedforsecurity=False).hexdigest()[:12]}"
        with st.container(border=True):
            options: list[str] = [c[0] for c in r.candidates] + [ADD_NEW]
            if all_names:
                options.append(USE_EXISTING)
            labels = {c[0]: f"Use existing: {c[0]} · match {c[1]:.0f}%" for c in r.candidates}
            labels[ADD_NEW] = f"➕ Add “{r.raw}” as a NEW item"
            labels[USE_EXISTING] = "🔍 Use another existing item"
            title = f"“{r.raw}”" + (" – similar items found" if r.candidates else " – not in the items master")
            prev = remembered(card, key, None)
            choice = remember(card, key, st.radio(title, options, index=options.index(prev) if prev in options else None,
                                                  format_func=lambda x: labels[x], key=key))
            if choice is None or choice not in labels:
                unresolved.append(r.raw)
                continue
            if choice == ADD_NEW:
                c1, c2, c3 = st.columns([3, 1, 2])
                name = remember(card, key + "_name", c1.text_input("Item name", value=remembered(card, key + "_name", r.raw), key=key + "_name"))
                unit_opts = [None, *UNITS]
                seen_unit = remembered(card, key + "_unit", canon_unit(r.unit))
                u_idx = unit_opts.index(seen_unit) if seen_unit in unit_opts else 0
                unit = remember(card, key + "_unit", c2.selectbox("Unit", unit_opts, index=u_idx, key=key + "_unit",
                                                                  format_func=lambda u: "—" if u is None else u))
                group = remember(card, key + "_group", c3.text_input("Stock group (optional)", value=remembered(card, key + "_group", ""),
                                                                     key=key + "_group"))
                if not name.strip():
                    unresolved.append(r.raw)
                    continue
                existing = db.get_item_by_name_or_alias(name)
                if existing:
                    st.caption(f"“{existing['name']}” already exists – it will be used.")
                    decisions[r.raw] = {"action": "use", "name": existing["name"]}
                else:
                    decisions[r.raw] = {"action": "new", "name": name.strip(), "unit": unit, "group": group.strip() or None}
            elif choice == USE_EXISTING:
                prev_pick = remembered(card, key + "_pick", None)
                pick = remember(card, key + "_pick", st.selectbox("Existing item", all_names, key=key + "_pick", placeholder="Type to search…",
                                                                  index=all_names.index(prev_pick) if prev_pick in all_name_set else None))
                if pick is None or pick not in all_name_set:
                    unresolved.append(r.raw)
                    continue
                decisions[r.raw] = {"action": "use", "name": pick}
            else:
                decisions[r.raw] = {"action": "use", "name": choice}
    return decisions, unresolved


def _totals_section(card: dict, check: BillDraft, rep) -> float | None:
    draft: BillDraft = card["draft"]
    k = card["k"]
    st.markdown("**4 · Totals & checks**")
    active = check.active_items
    if active:
        st.caption("Line amounts: " + " · ".join(f"{it.item} {money(it.effective_amount)}" for it in active))
    c1, c2, c3 = st.columns(3)
    c1.metric("Sum of lines", money(rep.computed_total))
    tw = c2.number_input("Total written on bill", min_value=0.0, step=1.0,
                         value=max(0.0, float(draft.total_written)) if draft.total_written is not None else 0.0,
                         key=f"nb_tw_{k}")
    draft.total_written = tw if tw > 0 else None
    book = rep.computed_total
    if draft.total_written is not None and rep.computed_total is not None and abs(draft.total_written - rep.computed_total) > config.AMOUNT_TOLERANCE:
        bkey = f"nb_book_{k}"
        pick = remember(card, bkey, c3.radio("Amount to book", ["Sum of lines", "Written total"], key=bkey,
                                             index=1 if remembered(card, bkey, "") == "Written total" else 0))
        book = rep.computed_total if pick == "Sum of lines" else draft.total_written
    elif rep.computed_total is None and draft.total_written is not None:
        book = draft.total_written
    c3.metric("Amount to book", money(book))
    for w in rep.warnings:
        st.warning(w, icon="⚠️")
    for m in rep.missing:
        st.error(m, icon="⛔")
    return book


def _render_card(card: dict) -> CardState:
    draft: BillDraft = card["draft"]
    k = card["k"]
    if card["image"]:
        left, right = st.columns([3, 2])
        with right:
            data, name = card["image"]
            if name.lower().endswith(".pdf"):
                st.caption(f"PDF: {name}")
            else:
                try:
                    st.image(data, caption=name, width="stretch")
                except Exception:  # noqa: BLE001
                    st.caption(f"{name} – preview not available")
    else:
        left = st.container()
    with left:
        party, kind = _party_section(card)
        bill_date, bill_no = _details_section(card)
    _items_section(card)
    decisions, unresolved_items = _item_check_section(card)
    check = draft.model_copy(deep=True, update={"bill_date": bill_date, "bill_no": bill_no or None})
    rep = service.draft_to_review(check, party_resolved=party is not None, require_bill_no=False)
    if not bill_no.strip():
        rep.missing.append("Bill number is missing.")
    elif party is not None and party.id and db.bill_no_exists(party.id, bill_no):
        rep.missing.append(f"Bill number “{bill_no}” already exists for {party.name}.")
    for nm in unresolved_items:
        rep.missing.append(f"Item “{nm}”: pick an existing item or confirm it as new (section 3b).")
    amount = _totals_section(card, check, rep)
    # (a toggle, not an expander: expanders cannot be nested inside the card's expander)
    if st.toggle("Show what the reader saw (raw transcription & notes)", key=f"nb_raw_{k}"):
        for n in draft.notes or []:
            st.markdown(f"- {n}")
        st.code(draft.raw_text or "(none)")
    return CardState(party=party, bill_date=bill_date, bill_no=bill_no, amount=amount, decisions=decisions,
                     missing=list(rep.missing), warnings=list(rep.warnings), computed_total=rep.computed_total,
                     match_kind=kind)


def _save_card(card: dict, state: CardState) -> Optional[Bill]:
    try:
        bill = service.commit_bill(card["draft"], state.party, state.bill_date, state.bill_no, state.amount,
                                   image=card["image"], item_decisions=state.decisions)
    except ValueError as e:
        card["error_save"] = str(e)
        return None
    except Exception as e:  # noqa: BLE001
        card["error_save"] = f"Could not save: {e}"
        return None
    card["saved"] = bill
    card["image"] = None  # the photo is on disk now – don't keep 20 photos' bytes in the session
    card["items_df"] = None
    card.pop("error_save", None)
    st.session_state.nb_saved_ids.append(bill.id)
    return bill


def _status_of(card: dict) -> str:
    if card.get("saved"):
        return "✅ saved"
    if card.get("error"):
        return "❌ could not read"
    st_ = card.get("state")
    if st_ is None:
        return "…"
    return "🟢 ready to save" if st_.can_save else "🟠 needs input"


def _summary_table(slot, cards: list[dict]) -> None:
    rows = []
    for i, c in enumerate(cards, start=1):
        d: BillDraft = c["draft"]
        s: CardState | None = c.get("state")
        b: Bill | None = c.get("saved")
        rows.append({
            "#": i, "File": c["label"], "Party on bill": d.party_name_raw or "",
            "Party": (b and db.get_party(b.party_id).name) or (s.party.name if s and s.party else ""),
            "Date": (b.bill_date if b else (s.bill_date if s else d.bill_date)),
            "Bill no": (b.bill_no if b else (s.bill_no if s else d.bill_no or "")),
            "Lines": len([it for it in d.items if not it.struck_out]),
            "Sum of lines": (b.amount if b else (s.computed_total if s else d.computed_total)),
            "Written total": d.total_written,
            "Status": _status_of(c),
            "Issues": "; ".join((s.missing if s else [])[:3]) if not b else "",
        })
    df = pd.DataFrame(rows)
    with slot:
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                                    "Sum of lines": st.column_config.NumberColumn("Sum of lines", format="%.2f"),
                                    "Written total": st.column_config.NumberColumn("Written total", format="%.2f")})


def _review_stage() -> None:
    ss = st.session_state
    cards: list[dict] = ss.nb_cards
    n_saved = sum(1 for c in cards if c.get("saved"))
    top_l, top_m, top_r = st.columns([3, 1, 1])
    top_l.markdown(f"**{len(cards)} bill(s) read · {n_saved} saved**")
    if top_m.button("▲ Hide the input panel" if ss.nb_show_input else "➕ Read more bills", key="nb_more"):
        ss.nb_show_input = not ss.nb_show_input
        st.rerun()
    if top_r.button("✖ Discard unsaved & start over", key="nb_discard"):
        _reset_all()
        st.rerun()

    table_slot = st.container()  # filled after the cards, so it reflects this run's widget values
    save_all_slot = st.container()
    export_slot = st.container()

    # One tab per bill. Labels stay static (no status icon): Streamlit re-mounts the tab strip when a
    # label changes, which would throw the user back to the first tab after every save.
    labels = [f"{i}. {card['label'][:28]}" for i, card in enumerate(cards, start=1)]
    st.caption("Open a tab, check the bill, save it — then move to the next tab. "
               "**Save all ready** above saves every tab that has no open issue.")
    tabs = st.tabs(labels)
    for tab, (i, card) in zip(tabs, enumerate(cards, start=1)):
        d: BillDraft = card["draft"]
        title = f"{i}. {card['label']} — {d.party_name_raw or 'party?'}"
        with tab:
            if card.get("saved"):
                b: Bill = card["saved"]
                st.success(f"✅ {title} · bill {b.bill_no} · {money(b.amount)} {b.drcr} — saved. "
                           "Tally files for all saved bills are offered above the table.")
                continue
            if card.get("error"):
                st.error(f"❌ {title} – could not read: {card['error']}")
                if card["image"] and not card["image"][1].lower().endswith(".pdf"):
                    try:
                        st.image(card["image"][0], width=300)
                    except Exception:  # noqa: BLE001
                        pass
                c1, c2 = st.columns(2)
                if c1.button("🔁 Retry reading", key=f"nb_retry_{card['k']}"):
                    new = _read_files([(card["image"][1], card["image"][0])])[0]
                    cards[i - 1] = new
                    st.rerun()
                if c2.button("📝 Enter this bill manually", key=f"nb_manual_{card['k']}"):
                    cards[i - 1] = _new_card(BillDraft(source="manual", bill_date=date.today()), card["image"])
                    st.rerun()
                card["state"] = None
                continue
            st.markdown(f"{_status_of(card)} — **{title}**")
            state = _render_card(card)
            card["state"] = state
            if card.get("error_save"):
                st.error(card["error_save"])
            if st.button("💾 Save this bill", type="primary", disabled=not state.can_save, key=f"nb_save_{card['k']}"):
                if _save_card(card, state):
                    toast_ok(f"Saved bill {state.bill_no}.")
                    st.rerun()

    _summary_table(table_slot, cards)
    ready = [c for c in cards if not c.get("saved") and not c.get("error") and c.get("state") and c["state"].can_save]
    with save_all_slot:
        c1, c2 = st.columns([1, 3])
        if c1.button(f"💾 Save all ready ({len(ready)})", type="primary", disabled=not ready, key="nb_save_all"):
            ok = 0
            for c in ready:
                if _save_card(c, c["state"]):
                    ok += 1
            toast_ok(f"Saved {ok} bill(s).")
            st.rerun()
        pending = [c for c in cards if not c.get("saved") and not c.get("error") and c.get("state") and not c["state"].can_save]
        if pending:
            c2.caption(f"{len(pending)} bill(s) still need input (party, bill number or item confirmation) – see the cards below.")
    if ss.nb_saved_ids:
        with export_slot:
            _quick_export_block(list(ss.nb_saved_ids))


# --------------------------------------------------------------------------- #
# Tally files for the bills saved in this batch
# --------------------------------------------------------------------------- #
def _quick_export_block(bill_ids: list[int]) -> None:
    ss = st.session_state
    cache = ss.setdefault("nb_quick_export_cache", {})
    try:
        settings = tally_export.TallySettings.load()
    except Exception as e:  # noqa: BLE001
        st.warning(f"Could not read the Tally settings: {e}")
        return
    bills = [b for b in (db.get_bill(i) for i in bill_ids) if b is not None]
    if not bills:
        ss.nb_saved_ids = []
        return
    sig = (tuple(sorted(b.id for b in bills)), tuple(sorted(b.bill_no for b in bills)), settings)
    if cache.get("sig") != sig:
        try:
            bundle = tally_export.build_bundle(bills, list(tally_export.FORMATS), s=settings,
                                               stem=f"Bills_{_slug(bills[0].bill_no)}" if len(bills) == 1 else f"Bills_{len(bills)}")
        except Exception as e:  # noqa: BLE001
            st.warning(f"Could not prepare the Tally files: {e}")
            return
        cache.clear()
        cache.update({"sig": sig, "files": bundle.files, "warnings": bundle.warnings})
    with st.container(border=True):
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"**Send {len(bills)} saved bill(s) to Tally** – voucher XML (Import → Transactions), Excel (Tally's "
                    "Accounting Voucher template + row layout), CSV, and the masters XML for new ledgers / stock items.")
        if c2.button("Dismiss", key="nb_qe_dismiss"):
            ss.nb_saved_ids = []
            st.rerun()
        for w in cache["warnings"]:
            st.warning(w)
        download_buttons(cache["files"], key_prefix=f"nb_qe_{len(bills)}_{bills[-1].id}", zip_name="bills_tally.zip")


def _slug(s: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9._-]+", "_", s or "").strip("_") or "bill"


# --------------------------------------------------------------------------- #
def render() -> None:
    _init_state()
    st.title("📄 New Bill")
    ss = st.session_state
    if not ss.nb_cards:
        _input_stage()
        return
    # The review cards stay rendered while more bills are added: Streamlit drops the state of every
    # widget that is not rendered in a run, so hiding the cards would wipe the edits made on them.
    if ss.nb_show_input:
        with st.container(border=True):
            _input_stage()
    _review_stage()
