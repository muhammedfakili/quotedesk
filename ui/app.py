"""QuoteDesk review screen.

Quote requests arrive by email -> the API drafts a quote -> a person checks the flagged
lines, edits, approves (or rejects) -> only then the reply goes to the outbox.

Run:  uv run streamlit run ui/app.py   (API must be running on QUOTEDESK_API_URL)
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.client import ApiClient, ApiError

st.set_page_config(page_title="QuoteDesk", page_icon=":material/request_quote:", layout="wide")

STATUS_COLOR = {"needs_review": "orange", "sent": "green", "won": "green", "lost": "gray", "rejected": "red",
                "approved": "blue"}
FLAG_LABEL = {
    "not_in_catalogue": "Not in catalogue", "low_confidence": "Uncertain match", "spec_mismatch": "Size conflict",
    "unclear_line": "Unclear line", "unit_mismatch": "Unit problem", "unit_assumed": "Unit assumed",
    "missing_quantity": "No quantity", "below_floor": "Below margin floor", "out_of_stock": "Low stock",
    "rounded_up": "Rounded up", "category_differs": "Category differs", "no_decision": "No decision",
    "invented_sku": "Invented SKU", "possible_injection": "Injection attempt", "new_customer": "New customer",
    "cover_replaced": "Cover note replaced", "unreadable_attachment": "Unreadable attachment",
    "no_lines": "No lines",
}


@st.cache_resource
def get_client() -> ApiClient:
    return ApiClient()


api = get_client()
st.session_state.setdefault("reviewer", "Alex (sales desk)")


def money(v: float | None, currency: str = "") -> str:
    return "" if v is None else f"{currency} {v:,.2f}".strip()


def flag_text(flags: list[dict], open_only: bool = True) -> str:
    return ", ".join(FLAG_LABEL.get(f["code"], f["code"]) for f in flags
                     if not open_only or (f["blocking"] and not f["resolved"]))


# --- sidebar: inbox --------------------------------------------------------------------------------
with st.sidebar:
    st.title(":material/request_quote: QuoteDesk")
    st.caption("Northgate Industrial Supply (fictional) - sales desk")
    health = api.health()
    if health.get("status") != "ok":
        st.error(health.get("detail", "API down"), title="API not reachable")
        st.stop()
    st.caption(f"Model: {health['model']}")
    st.session_state.reviewer = st.text_input("Reviewer", st.session_state.reviewer)

    with st.container(border=True):
        st.markdown("**Inbox**")
        samples = api.samples()
        picked = st.multiselect("Demo: drop sample emails into the inbox", samples, default=[],
                                placeholder="r01, r12, n02...")
        if st.button("Add to inbox", icon=":material/move_to_inbox:", disabled=not picked, width="stretch"):
            api.load_samples(picked)
            st.toast(f"{len(picked)} email(s) added to the inbox")
        if st.button("Process new emails", icon=":material/play_arrow:", type="primary", width="stretch"):
            with st.spinner("Reading emails, matching, pricing... (about 20 s per request on the free tier)"):
                try:
                    results = api.poll()
                except ApiError as exc:
                    st.error(str(exc))
                    results = []
            for r in results:
                if r.get("error"):
                    st.error(f"{r['email_id']}: {r['error']}")
                elif r.get("ref"):
                    st.success(f"{r['ref']} drafted ({r['open_flags']} open flags)")
                else:
                    st.info(f"Not a quote request: {r['email_class']}")
            if not results:
                st.caption("No new emails.")


review_page, outbox_page = st.tabs([":material/fact_check: Review", ":material/outbox: Outbox & log"])

# The outbox renders first: the review tab may stop the script when there is nothing to review.
with outbox_page:
    st.subheader("Outbox")
    st.caption("Mock outbox: each message is an .eml file in data/outbox. Nothing leaves this machine.")
    for msg in api.outbox():
        with st.expander(f"{msg['subject']}  ->  {msg['to']}", icon=":material/mail:"):
            st.text(msg["body"])
            st.caption("Attachments: " + (", ".join(msg["attachments"]) or "none"))
    st.subheader("All emails")
    st.dataframe(pd.DataFrame(api.emails()), hide_index=True, alt="Received emails")
    st.subheader("Recent events")
    st.dataframe(pd.DataFrame(api.events()), hide_index=True, alt="Event log")

with review_page:
    status_filter = st.segmented_control("Show", ["needs_review", "sent", "won", "lost", "rejected", "all"],
                                         default="needs_review", format_func=lambda s: s.replace("_", " "))
    quotes = api.quotes(None if status_filter in (None, "all") else status_filter)
    if not quotes:
        st.info("No quotes here yet. Add sample emails to the inbox and process them (sidebar).")
        st.stop()
    labels = {q["ref"]: f"{q['ref']} - {q['company']}" for q in quotes}  # stable text: never a status
    ref = st.selectbox("Quote", list(labels), format_func=labels.__getitem__)
    quote = api.quote(ref)
    cur = quote["currency"]
    editable = quote["status"] == "needs_review"

    head = st.container(horizontal=True, vertical_alignment="center")
    head.subheader(f"{quote['ref']} - {quote['customer']['company']}")
    head.badge(quote["status"].replace("_", " "), color=STATUS_COLOR.get(quote["status"], "gray"))
    if not quote["customer"].get("known"):
        head.badge("new customer", color="violet")
    cols = st.columns(5)
    cols[0].metric("Customer", quote["customer"]["type"], help=f"Payment terms: {quote['customer']['terms']}")
    cols[1].metric("Subtotal", money(quote["subtotal"], cur))
    cols[2].metric("VAT", money(quote["vat"], cur))
    cols[3].metric("Total", money(quote["total"], cur))
    cols[4].metric("Open flags", len(quote["open_flags"]))

    for f in quote["flags"]:
        if f["resolved"]:
            continue
        box = st.warning if f["blocking"] else st.info
        with st.container(horizontal=True, vertical_alignment="center"):
            box(f["message"], title=FLAG_LABEL.get(f["code"], f["code"]), icon=":material/flag:")
            if f["blocking"] and editable and st.button("Checked", key=f"qflag-{f['code']}"):
                api.resolve_flag(ref, f["code"], st.session_state.reviewer)
                st.rerun()

    lines_tab, email_tab, pdf_tab, log_tab = st.tabs(["Lines", "Original email", "Quote PDF", "Log"])

    with lines_tab:
        table = pd.DataFrame([{
            "#": line["line_no"], "Customer wrote": line["description"],
            "Asked": f"{line['requested_qty'] or ''} {line['requested_unit'] or ''}".strip(),
            "Product": "(excluded)" if line["excluded"] else (line["product_name"] or "-"),
            "Qty": line["sell_qty"], "Sold as": line["sell_unit"], "Unit price": line["unit_price"],
            "Total": line["line_total"], "Margin": line["margin"],
            "Needs a look": flag_text(line["flags"]),
        } for line in quote["lines"]])
        st.dataframe(table, hide_index=True, alt="Quote lines", column_config={
            "Unit price": st.column_config.NumberColumn(format="%.2f"),
            "Total": st.column_config.NumberColumn(format="%.2f"),
            "Margin": st.column_config.NumberColumn(format="percent"),
        })

        for line in quote["lines"]:
            open_flags = [f for f in line["flags"] if f["blocking"] and not f["resolved"]]
            icon = ":material/error:" if open_flags else ":material/check_circle:"
            title = f"{line['line_no']}. {line['description']}"
            with st.expander(title, expanded=bool(open_flags) and editable, icon=icon):
                for f in line["flags"]:
                    if f["resolved"]:
                        st.caption(f":material/done: {FLAG_LABEL.get(f['code'], f['code'])} - checked")
                    elif f["blocking"]:
                        st.warning(f["message"], title=FLAG_LABEL.get(f["code"], f["code"]))
                    else:
                        st.info(f["message"], title=FLAG_LABEL.get(f["code"], f["code"]))
                if line["match_reason"]:
                    st.caption(f"Matcher ({line['match_confidence']}): {line['match_reason']}")
                if line["candidates"]:
                    st.dataframe(pd.DataFrame([{"SKU": c["sku"], "Name": c["name"], "Sold as": c["unit"],
                                                "Stock": c["stock"], "Why": c["why"]} for c in line["candidates"]]),
                                 hide_index=True, alt=f"Catalogue candidates for line {line['line_no']}")
                if not editable:
                    continue
                with st.form(f"line-{ref}-{line['line_no']}"):
                    options = list(dict.fromkeys([line["sku"] or "", *[c["sku"] for c in line["candidates"]]]))
                    names = {c["sku"]: c["name"] for c in line["candidates"]}
                    names[line["sku"] or ""] = line["product_name"] or "(no product)"
                    c1, c2, c3 = st.columns([3, 1, 1])
                    sku = c1.selectbox("Product", options, format_func=lambda s, n=names: f"{s} {n.get(s, '')}".strip())
                    other = c1.text_input("...or another SKU (search below)", "", placeholder="NG-10xxx")
                    qty = c2.number_input("Quantity", value=float(line["requested_qty"] or 0), min_value=0.0)
                    unit = c3.text_input("Unit", line["requested_unit"] or "")
                    c4, c5, c6 = st.columns([1, 1, 3])
                    margin = c4.number_input("Margin %", value=float(round((line["margin"] or 0.0) * 100, 1)),
                                             min_value=0.0, max_value=89.0, step=0.5)
                    excluded = c5.checkbox("Exclude line", value=line["excluded"])
                    resolvable = [f["code"] for f in open_flags if f["code"] != "below_floor"]
                    resolve = c6.multiselect("Mark as checked", resolvable,
                                             format_func=lambda c: FLAG_LABEL.get(c, c))
                    if st.form_submit_button("Save line", icon=":material/save:"):
                        changes: dict = {"reviewer": st.session_state.reviewer, "excluded": excluded}
                        chosen = other.strip().upper() or sku
                        if chosen and chosen != line["sku"]:
                            changes["sku"] = chosen
                        if qty != (line["requested_qty"] or 0):
                            changes["quantity"] = qty
                        if unit != (line["requested_unit"] or ""):
                            changes["unit"] = unit
                        if line["margin"] is not None and abs(margin - line["margin"] * 100) > 0.05:
                            changes["margin_override"] = round(margin / 100, 4)
                        if resolve:
                            changes["resolve"] = resolve
                        try:
                            api.edit_line(ref, line["line_no"], **changes)
                            st.rerun()
                        except ApiError as exc:
                            st.error(str(exc))

        if editable:
            with st.expander("Search the catalogue", icon=":material/search:"):
                q = st.text_input("Search", placeholder="e.g. 316 stainless washer M12")
                if q:
                    st.dataframe(pd.DataFrame([{"SKU": h["sku"], "Name": h["name"], "Sold as": h["unit"],
                                                "Stock": h["stock"], "Why": h["why"]} for h in api.search(q)]),
                                 hide_index=True, alt="Catalogue search results")

        st.divider()
        if editable:
            with st.container(border=True):
                st.markdown("**Approve and send**")
                for b in quote["blockers"]:
                    st.caption(f":material/block: {b}")
                below = any(f["code"] == "below_floor" and not f["resolved"] for line in quote["lines"]
                            for f in line["flags"] if not line["excluded"])
                reason = st.text_input("Reason for the below-floor margin", "") if below else None
                left, right = st.columns(2)
                if left.button("Approve and send to customer", type="primary", icon=":material/send:",
                               width="stretch"):
                    try:
                        done = api.approve(ref, st.session_state.reviewer, reason or None)
                        st.success(f"Sent to the outbox. Supplier enquiries: {len(done['supplier_enquiries'])}")
                        st.rerun()
                    except ApiError as exc:
                        st.error(str(exc), title="Not sent")
                with right.popover("Reject", icon=":material/cancel:", width="stretch"):
                    why = st.text_input("Why?", key=f"reject-{ref}")
                    if st.button("Reject quote", disabled=not why):
                        api.reject(ref, st.session_state.reviewer, why)
                        st.rerun()
        elif quote["status"] in ("sent", "won", "lost"):
            with st.container(horizontal=True):
                st.caption(f"Approved by {quote['approved_by']}. Did the customer order?")
                if st.button("Won", icon=":material/thumb_up:"):
                    api.outcome(ref, "won")
                    st.rerun()
                if st.button("Lost", icon=":material/thumb_down:"):
                    api.outcome(ref, "lost")
                    st.rerun()
            for e in quote["exceptions"]:
                st.caption(f"Exception: {e}")

    with email_tab:
        original = api.original_email(ref)
        st.markdown(f"**From:** {original['from']}  \n**Subject:** {original['subject']}")
        st.text(original["body"])
        for att in original["attachments"]:
            st.caption(f":material/attach_file: {att['filename']} ({att['size'] // 1024} KB)")
            try:
                st.image(api.attachment_preview(ref, att["index"]), alt=f"Attachment {att['filename']}")
            except ApiError:
                st.caption("No preview for this file type.")

    with pdf_tab:
        st.download_button("Download PDF", api.quote_pdf(ref), file_name=f"{ref}.pdf", mime="application/pdf",
                           icon=":material/download:")
        if editable:
            if quote["cover_not_quoted"] != [line["description"] for line in quote["lines"]
                                             if line["excluded"] or line["line_total"] is None]:
                st.info("Lines changed since this note was drafted. Unless you edit it, the template note "
                        "with the current list is sent.", icon=":material/info:")
            with st.form(f"cover-{ref}"):
                subject = st.text_input("Email subject", quote["cover_subject"])
                body = st.text_area("Email text (no prices: they are in the PDF)", quote["cover_body"], height=260)
                if st.form_submit_button("Save email text", icon=":material/save:"):
                    try:
                        api.edit_cover(ref, subject, body, st.session_state.reviewer)
                        st.rerun()
                    except ApiError as exc:
                        st.error(str(exc))
        else:
            st.markdown(f"**Subject:** {quote['cover_subject']}")
            st.text(quote["cover_body"])
        st.image(api.quote_preview(ref), alt=f"Quote {ref} page 1")

    with log_tab:
        st.dataframe(pd.DataFrame(api.events(ref)), hide_index=True, alt="Event log for this quote")
