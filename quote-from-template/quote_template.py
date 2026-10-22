#!/usr/bin/env python3
"""quote_template - analyse a quotation / invoice style Excel template and fill it.

Three commands:

    analyze TEMPLATE.xlsx [--sheet NAME] [--out profile.json]
    fill    TEMPLATE.xlsx --items items.json --out result.xlsx [--profile P] [--fields F]
    verify  RESULT.xlsx [--profile P] [--items items.json]

Design rules that the code tries hard to keep:

* The end of the item area is defined by the subtotal row (or by an existing SUM
  formula), never by a fixed row count. No scan or write is capped.
* Amount, subtotal, VAT and total cells are always formulas, never literals.
* More items than pre-formatted slots -> rows are inserted (style, merges, row
  height copied; everything below is shifted, SUM range is extended).
* Fewer items than slots -> the unused slots are cleared (no stale sample data).
* When detection is ambiguous the tool says so and refuses to guess: edit the
  profile by hand and pass it back with --profile.

Only openpyxl is required. Formulas are NOT evaluated by openpyxl; the output is
flagged so Excel / LibreOffice recalculates when the file is opened. ``verify``
recomputes simple formulas in pure Python as an independent check.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime
import json
import math
import os
import re
import sys
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.formula import Tokenizer
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.cell_range import CellRange

PROFILE_VERSION = 1
DEFAULT_VAT_RATE = 0.1
LOW_CONFIDENCE = 0.6


class QuoteError(Exception):
    """A problem the user must fix (bad input, ambiguous template, ...)."""


# --------------------------------------------------------------------------- #
# Text normalisation and synonym tables
# --------------------------------------------------------------------------- #

_STRIP = re.compile(r"[\s()\[\]{}<>:：.,;/\\_\-*·'’\"`~!?&|+=#%]+")


def norm(text):
    """Lower-case, NFKC-fold and drop spaces / punctuation for comparisons."""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    return _STRIP.sub("", s)


HEADER_SYNONYMS = {
    # index / serial-number column: fill() renumbers it 1..n
    "no": ["no", "번호", "순번", "순서", "seq", "lineno", "itemno", "line", "연번",
           "일련번호", "sn", "sno", "slno", "srno", "serial", "serialno",
           "serialnumber", "index", "idx"],
    "name": [
        "품명", "품목", "내역", "항목", "상품명", "제품명", "명세", "적요", "내용",
        "품명및규격", "품명규격", "품목및규격", "품목명", "서비스명",
        "description", "item", "items", "itemdescription", "product",
        "productname", "particulars", "service", "details", "descriptionofgoods",
        "descriptionspec",
    ],
    "spec": [
        "규격", "사양", "모델", "모델명", "품번", "spec", "specs", "specification",
        "model", "partno", "partnumber", "sku",
    ],
    "unit": ["단위", "unit", "uom"],
    "qty": ["수량", "qty", "quantity", "qtyea", "개수", "수량ea", "quantities"],
    "unit_price": [
        "단가", "공급단가", "판매단가", "unitprice", "price", "unitcost",
        "priceperunit", "가격", "판매가", "unitrate",
    ],
    "amount": [
        "금액", "공급가액", "합계금액", "합계", "소계", "amount", "total",
        "totalprice", "lineamount", "linetotal", "extendedprice", "lineprice",
        "extendedamount", "totalamount",
    ],
    "note": [
        "비고", "remarks", "remark", "note", "notes", "memo", "comment",
        "comments", "참고", "특이사항",
    ],
}
# A per-line tax column cannot be filled by this tool; detect it to warn.
TAX_HEADERS = ["세액", "부가세", "tax", "vat", "taxamount", "vatamount"]

_HEADER_NORM = {c: [norm(s) for s in syns] for c, syns in HEADER_SYNONYMS.items()}
_TAX_NORM = [norm(s) for s in TAX_HEADERS]
CORE_CATS = ("name", "spec", "qty", "unit_price", "amount", "note")

_VAT_EXCL = re.compile(
    r"(vat|부가세|부가가치세|tax)(별도|제외|미포함|excl\w*|exclusive|excluded)"
    r"|excl\w*(vat|tax)|exvat|extax|before(vat|tax)|세전"
)

TOTAL_WORDS = [
    ("grand", ["grandtotal", "총합계", "총액", "총계", "합계금액", "청구금액",
               "vat포함", "부가세포함", "부가가치세포함", "totalincl", "totaldue",
               "amountdue", "totalwithvat", "totalwithtax", "inclvat", "inctax",
               "totalpayable", "최종금액"]),
    ("vat", ["부가세", "부가가치세", "vat", "tax", "세액", "salestax", "gst"]),
    ("sub", ["소계", "subtotal", "sub", "subtot", "subtotals", "공급가액", "공급가",
             "공급가액합계", "공급가합계", "소계금액", "순액", "netamount",
             "nettotal"]),
    ("generic", ["합계", "total"]),
]

LABEL_WORDS = [
    "customer", "client", "billto", "soldto", "shipto", "to", "attn", "attention",
    "contact", "project", "subject", "date", "issuedate", "quotedate",
    "invoicedate", "validuntil", "validity", "validfor", "docno", "documentno",
    "quoteno", "quotationno", "invoiceno", "ref", "reference", "refno",
    "preparedby", "company", "companyname", "name", "title", "from", "tel",
    "phone", "email", "address", "paymentterms", "deliveryterms", "delivery",
    "수신", "수신처", "고객", "고객사", "업체명", "상호", "담당자", "담당", "사업명",
    "건명", "제목", "일자", "날짜", "견적일", "견적일자", "작성일", "발행일",
    "견적번호", "문서번호", "번호", "유효기간", "납기", "결제조건", "참조", "연락처",
    "회사명", "발신", "프로젝트",
]
_LABEL_NORM = sorted({norm(w) for w in LABEL_WORDS})

PLACEHOLDER_RE = re.compile(
    r"^\s*(\{\{.*\}\}|\[.*\]|<.*>|\(.*\)|_{2,}|\.{3,}|[xX○◯□■]{2,}"
    r"|y{2,}[-./]?m{2,}.*|YYYY.*)\s*$"
)

EXPECTED_ITEM_KEYS = ("name", "spec", "qty", "unit_price", "note", "unit")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _col_idx(letter):
    return column_index_from_string(letter)


def _is_formula(cell):
    return (not isinstance(cell, MergedCell)) and cell.data_type == "f"


def _formula_text(cell):
    v = cell.value
    return v if isinstance(v, str) else ""


def _merge_maps(ws):
    """Return (top_left_coord -> CellRange, set of non-top-left coords)."""
    tl, covered = {}, set()
    for rng in ws.merged_cells.ranges:
        tl[f"{get_column_letter(rng.min_col)}{rng.min_row}"] = rng
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    covered.add((r, c))
    return tl, covered


def _writable(ws, row, col):
    """Cell for writing; merged continuation cells resolve to the top-left."""
    cell = ws.cell(row, col)
    if isinstance(cell, MergedCell):
        for rng in ws.merged_cells.ranges:
            if (rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col):
                return ws.cell(rng.min_row, rng.min_col)
    return cell


def _put(cell, value, formula=False):
    """Write a value. Text starting with '=' is stored as text unless formula=True."""
    if isinstance(cell, MergedCell):
        raise QuoteError(
            f"cannot write to merged cell {cell.coordinate}; fix the profile "
            "(column mapping) or unmerge the template")
    cell.value = value
    if isinstance(value, str) and value.startswith("=") and not formula:
        cell.data_type = "s"


def match_header(text):
    """Return (category, strength) for a header cell, or None."""
    raw = str(text).strip()
    if raw in ("#", "№"):
        return "no", 1.0
    n = norm(raw)
    if not n or len(n) > 40:
        return None
    best = None
    for cat, syns in _HEADER_NORM.items():
        for s in syns:
            if not s:
                continue
            if n == s:
                cand = (len(s) + 100, cat, 1.0)
            elif (len(s) >= (4 if s.isascii() else 2)
                  and (n.startswith(s) or n.endswith(s)) and len(n) <= len(s) + 8):
                cand = (len(s), cat, 0.7)
            else:
                continue
            if best is None or cand[0] > best[0]:
                best = cand
    if best:
        return best[1], best[2]
    return None


def _is_tax_header(text):
    return norm(text) in _TAX_NORM


def classify_total_label(text):
    """Classify a label cell as grand/vat/sub/generic, or None."""
    kind = _classify_norm(_VAT_EXCL.sub("", norm(text)))
    if kind:
        return kind
    # retry without parenthetical suffixes: "Sub (excl. fees)", "Tax (10%)"
    stripped = _PAREN.sub(" ", unicodedata.normalize("NFKC", str(text)))
    if stripped.strip() and norm(stripped) != norm(text):
        return _classify_norm(_VAT_EXCL.sub("", norm(stripped)))
    return None


_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
# "Total (incl. VAT)", "합계 (VAT 포함)": a total word plus an inclusion marker
_TOTAL_HEAD = re.compile(r"^(총합계|합계|총계|총액|total)")
_INCL = re.compile(r"포함|incl[a-z]*|inc(?=vat|tax)")


def _classify_norm(n):
    if not n or len(n) > 30:
        return None
    head = _TOTAL_HEAD.match(n)
    if head and _INCL.search(n[head.end():]):
        return "grand"
    for kind, words in TOTAL_WORDS:
        for w in words:
            w = norm(w)
            if n == w:
                return kind
            if w.isascii() and len(w) <= 4:
                if re.fullmatch(re.escape(w) + r"\d+", n):
                    return kind
                continue
            slack = 4 if len(w) <= 3 else 10
            if n.startswith(w) and len(n) <= len(w) + slack:
                return kind
    return None


def _is_label(text):
    t = text.strip()
    if not t or len(t) > 40:
        return False
    if t.endswith(":") or t.endswith("："):
        return True
    n = norm(t)
    if not n:
        return False
    for w in _LABEL_NORM:
        if n == w:
            return True
        if (len(w) >= (3 if w.isascii() else 2) and n.startswith(w)
                and len(n) <= len(w) + 6):
            return True
    return False


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def _header_candidates(ws):
    """Rows that look like an item-table header: [(row, score, cats)]."""
    out = []
    for row in ws.iter_rows():
        cats = {}
        taxes = []
        for cell in row:
            if isinstance(cell, MergedCell) or not isinstance(cell.value, str):
                continue
            m = match_header(cell.value)
            if m:
                cats.setdefault(m[0], []).append((cell.column, m[1], cell.value.strip()))
            elif _is_tax_header(cell.value):
                taxes.append(cell.column)
        if "name" not in cats:
            continue
        if not any(c in cats for c in ("qty", "unit_price", "amount")):
            continue
        score = sum(1.0 for c in cats if c in CORE_CATS) \
            + sum(0.5 for c in cats if c in ("no", "unit"))
        if score < 2:
            continue
        out.append({"row": row[0].row, "score": score, "cats": cats, "tax_cols": taxes})
    return out


def _pick_sheet(wb, name):
    if name:
        if name not in wb.sheetnames:
            raise QuoteError(f"sheet {name!r} not found; sheets: {wb.sheetnames}")
        return wb[name]
    best, best_score = wb.active, -1.0
    order = [wb.active] + [w for w in wb.worksheets if w is not wb.active]
    for ws in order:
        cands = _header_candidates(ws)
        s = max((c["score"] for c in cands), default=0.0)
        if s > best_score:
            best, best_score = ws, s
    return best


def _scan_totals(ws, header_row, amount_col):
    """Find the first totals-looking row and classify later labelled rows."""
    amt_idx = _col_idx(amount_col) if amount_col else None
    rows = []  # (row, kind or None, label, has_sum)
    for row in ws.iter_rows(min_row=header_row + 1):
        r = row[0].row
        kind, label, label_col = None, None, None
        for cell in row:
            if isinstance(cell, MergedCell) or not isinstance(cell.value, str) \
                    or cell.data_type == "f":
                continue
            k = classify_total_label(cell.value)
            if k:
                kind, label, label_col = k, cell.value.strip(), cell.column
                break
        has_sum = False
        if amt_idx:
            ac = ws.cell(r, amt_idx)
            if _is_formula(ac) and _formula_text(ac).upper().lstrip("=").lstrip().startswith("SUM("):
                has_sum = True
        if kind or has_sum:
            rows.append({"row": r, "kind": kind, "label": label,
                         "label_col": label_col, "has_sum": has_sum})
    return rows


def _resolve_totals(rows):
    """Decide subtotal / vat / total rows from the labelled rows."""
    notes, sub, vat, tot = [], None, None, None
    if not rows:
        return None, None, None, 0.0, notes
    base = rows[0]["row"]
    block = [r for r in rows if r["row"] <= base + 8]  # totals block is compact
    for r in block:
        if r["kind"] == "vat" and vat is None:
            vat = r
        elif r["kind"] == "grand" and tot is None:
            tot = r
        elif r["kind"] == "sub" and sub is None:
            sub = r
    if sub is None:
        cand = [r for r in block if r is not vat and r is not tot
                and r["kind"] in ("generic", None)]
        if cand:
            sub = cand[0]
    if tot is None:
        rest = [r for r in block if r["kind"] == "generic" and r is not sub
                and r["row"] > (sub["row"] if sub else 0)
                and (vat is None or r["row"] > vat["row"])]
        if rest:
            tot = rest[-1]
    if sub is None and tot is not None and vat is None:
        sub, tot = tot, None
    conf = 0.5
    if sub is not None:
        if sub["kind"] and sub["has_sum"]:
            conf = 0.95
        elif sub["kind"]:
            conf = 0.85
        else:
            conf = 0.7
            notes.append("subtotal row identified only by an existing SUM formula "
                         "(no label)")
    unlabeled = [r for r in block if r["kind"] is None and r["has_sum"]]
    if len(unlabeled) > 1:
        notes.append("several rows hold SUM formulas; the first one was taken as "
                     "the subtotal")
        conf = min(conf, 0.6)
    return sub, vat, tot, conf, notes


def _unnamed_amount_cells(ws, after_row, value_col, named_rows, ignore_rows=()):
    """Cells in the amount column just below the item slots that hold a number or a
    formula but belong to no named totals row (subtotal / VAT / total)."""
    if not value_col:
        return []
    idx = _col_idx(value_col)
    skip = set(named_rows) | {int(x) for x in ignore_rows or ()}
    out = []
    # the totals block: a few rows below the last known totals row (or below the
    # slots when none is known); a second table further down is not scanned
    end = (max(named_rows) if named_rows else after_row + 6) + 2
    for r in range(after_row + 1, min(ws.max_row, end) + 1):
        if r in skip:
            continue
        c = ws.cell(r, idx)
        if isinstance(c, MergedCell) or _is_blank(c.value):
            continue
        if _is_formula(c):
            out.append({"cell": c.coordinate, "row": r, "kind": "formula",
                        "value": _formula_text(c)})
        elif _is_number(c.value):
            out.append({"cell": c.coordinate, "row": r, "kind": "literal",
                        "value": c.value})
    return out


def _scan_placeholders(ws, skip_lo, skip_hi):
    tl, _ = _merge_maps(ws)
    out = []
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell, MergedCell) or not isinstance(cell.value, str):
                continue
            if cell.data_type == "f" or skip_lo <= cell.row <= skip_hi:
                continue
            text = cell.value.strip()
            if not _is_label(text):
                continue
            rng = tl.get(cell.coordinate)
            next_col = (rng.max_col if rng else cell.column) + 1
            nxt = ws.cell(cell.row, next_col)
            if isinstance(nxt, MergedCell):
                continue
            v = nxt.value
            if _is_blank(v):
                state, conf = "empty", 0.9
            elif isinstance(v, str) and PLACEHOLDER_RE.match(v):
                state, conf = "placeholder", 0.8
            elif isinstance(v, str) and _is_label(v):
                continue
            else:
                state, conf = "filled", 0.5
            out.append({"label": text, "label_cell": cell.coordinate,
                        "value_cell": nxt.coordinate, "state": state,
                        "confidence": conf})
    return out


def _header_text(ws, row, col):
    cell = ws.cell(row, col)
    if isinstance(cell, MergedCell) or _is_blank(cell.value):
        return ""
    return str(cell.value).strip()


def _table_columns(ws, header_row, cols):
    """(first_col, last_col) of the table: the mapped columns plus any directly
    adjacent unmapped column that carries a header text."""
    idxs = [_col_idx(c) for c in cols.values()]
    lo, hi = min(idxs), max(idxs)
    while lo > 1 and _header_text(ws, header_row, lo - 1):
        lo -= 1
    while hi < ws.max_column and _header_text(ws, header_row, hi + 1):
        hi += 1
    return lo, hi


def _unmapped_columns(ws, prof):
    """Columns inside or directly next to the table that no role is mapped to
    but that hold a header text or content in the item rows."""
    cols = prof["columns"]
    h, first, last = prof["header_row"], prof["first_item_row"], prof["last_item_row"]
    mapped = {_col_idx(c) for c in cols.values()}
    lo, hi = _table_columns(ws, h, cols)
    out = []
    for c in range(max(1, lo - 1), hi + 2):
        if c in mapped:
            continue
        head = _header_text(ws, h, c)
        samples = []
        for r in range(first, last + 1):
            cell = ws.cell(r, c)
            if not isinstance(cell, MergedCell) and not _is_blank(cell.value):
                samples.append(str(cell.value))
        if not head and not samples:
            continue
        out.append({"column": get_column_letter(c), "header": head or None,
                    "in_table": lo <= c <= hi, "samples": samples[:3],
                    "item_rows_with_content": len(samples)})
    return out


def analyze_sheet(ws):
    """Build the profile dict for one worksheet."""
    reasons, notes, ambiguities = [], [], []
    conf = {}
    prof = {
        "version": PROFILE_VERSION, "sheet": ws.title, "header_row": None,
        "first_item_row": None, "last_item_row": None, "style_row": None,
        "columns": {}, "totals": {"subtotal_row": None, "vat_row": None,
                                   "total_row": None, "value_col": None},
        "vat_rate": None, "footer_rows": [], "merged_in_items": [],
        "number_formats": {}, "placeholders": [], "confidence": conf,
        "overall_confidence": 0.0, "needs_review": True,
        "review_reasons": reasons, "notes": notes,
    }
    cands = _header_candidates(ws)
    if not cands:
        reasons.append("no header row found (need a description column plus "
                       "quantity / unit price / amount)")
        conf["header_row"] = 0.0
        return prof
    cands.sort(key=lambda c: (-c["score"], c["row"]))
    best = cands[0]
    others = [c for c in cands[1:] if c["score"] >= max(best["score"] - 0.5, 2)]
    h = best["row"]
    if others:
        rows = [best["row"]] + [c["row"] for c in others]
        ambiguities.append(f"several rows look like an item header: {sorted(rows)}")
        conf["header_row"] = 0.3
        reasons.append(f"ambiguous header row (candidates {sorted(rows)}); the "
                       "first best one was taken - confirm or set header_row")
    else:
        conf["header_row"] = 0.95 if best["score"] >= 4 else (0.85 if best["score"] >= 3 else 0.5)
        if best["score"] < 3:
            reasons.append("header row matched only two column kinds")
    prof["header_row"] = h

    cols = {}
    for cat, hits in best["cats"].items():
        hits = sorted(hits)
        if len(hits) == 1:
            col, strength, _ = hits[0]
            cols[cat] = get_column_letter(col)
            conf[f"col_{cat}"] = 0.9 * strength + 0.1 * (strength == 1.0)
        else:
            pick = hits[-1] if cat in ("amount", "unit_price") else hits[0]
            cols[cat] = get_column_letter(pick[0])
            conf[f"col_{cat}"] = 0.4
            texts = [f"{get_column_letter(c)}:{t!r}" for c, _, t in hits]
            ambiguities.append(f"several columns look like '{cat}': {texts}")
            reasons.append(f"ambiguous '{cat}' column ({', '.join(texts)})")
    prof["columns"] = cols
    for need in ("name", "qty", "unit_price", "amount"):
        if need not in cols:
            reasons.append(f"missing column: {need}")
            conf[f"col_{need}"] = 0.0
    if best["tax_cols"]:
        reasons.append("a per-line tax column exists; this tool does not fill it")

    amount_col = cols.get("amount")
    prof["totals"]["value_col"] = amount_col
    rows = _scan_totals(ws, h, amount_col)
    sub, vat, tot, tconf, tnotes = _resolve_totals(rows)
    notes.extend(tnotes)
    first = h + 1
    prof["first_item_row"] = first
    if sub is not None:
        last = sub["row"] - 1
        conf["item_area"] = 0.9 if tconf >= 0.85 else 0.7
        conf["subtotal"] = tconf
        prof["totals"]["subtotal_row"] = sub["row"]
        if vat is not None:
            prof["totals"]["vat_row"] = vat["row"]
            conf["vat"] = 0.85 if vat["kind"] == "vat" else 0.5
        if tot is not None:
            prof["totals"]["total_row"] = tot["row"]
            conf["total"] = 0.85 if tot["kind"] == "grand" else 0.7
        if vat is None:
            notes.append("no VAT row detected; no VAT will be written")
        if last < first:
            reasons.append("no pre-formatted item slot between header and subtotal")
        if vat is not None and vat["row"] <= sub["row"]:
            reasons.append("VAT row is not below the subtotal row")
    else:
        conf["item_area"] = 0.3
        conf["subtotal"] = 0.0
        last = h
        r = h + 1
        while r <= ws.max_row and any(
                not _is_blank(ws.cell(r, _col_idx(c)).value) for c in cols.values()):
            last = r
            r += 1
        reasons.append("no subtotal / total row found, so the end of the item "
                       "area is a guess; set last_item_row and totals by hand")
    prof["last_item_row"] = last
    if sub is not None:
        named = [x["row"] for x in (sub, vat, tot) if x]
        for u in _unnamed_amount_cells(ws, last, amount_col, named):
            what = ("a literal number" if u["kind"] == "literal"
                    else f"the formula {u['value']}")
            reasons.append(
                f"cell {u['cell']} below the item slots holds {what} but row "
                f"{u['row']} is not recognised as the subtotal, VAT or total row "
                "(the label may be unusual); name it in totals.subtotal_row / "
                "vat_row / total_row of the profile, or list the row in "
                "\"ignore_rows\"")
    slots = last - first + 1
    if slots >= 1:
        prof["style_row"] = first + slots // 2
        notes.append(f"{slots} pre-formatted item slot(s): rows {first}-{last}")

    last_total = max([x["row"] for x in (sub, vat, tot) if x] or [last])
    if tot is None and vat is None and sub is not None:
        last_total = sub["row"]
    # footer rows
    for row in ws.iter_rows(min_row=last_total + 1):
        texts = [str(c.value).strip() for c in row
                 if not isinstance(c, MergedCell) and isinstance(c.value, str)
                 and c.value.strip()]
        if texts:
            prof["footer_rows"].append({"row": row[0].row, "text": " | ".join(texts)})
    # merges inside the item area
    if slots >= 1:
        prof["merged_in_items"] = sorted(
            r.coord for r in ws.merged_cells.ranges
            if r.min_row >= first and r.max_row <= last)
    # number formats of the style row
    sr = prof["style_row"]
    if sr:
        for cat, col in cols.items():
            prof["number_formats"][cat] = ws[f"{col}{sr}"].number_format
    vcol = amount_col
    if vcol:
        for key in ("subtotal", "vat", "total"):
            rr = prof["totals"][f"{key}_row"]
            if rr:
                prof["number_formats"][key] = ws[f"{vcol}{rr}"].number_format
    # VAT rate hint from the label text
    prof["vat_rate"] = DEFAULT_VAT_RATE
    if vat is not None and vat["label"]:
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", vat["label"])
        if m:
            prof["vat_rate"] = round(float(m.group(1)) / 100.0, 6)
            notes.append(f"VAT rate {m.group(1)}% read from the label text")
    prof["placeholders"] = _scan_placeholders(ws, h, last_total)
    if "name" in cols and "col_name" in conf and conf["col_name"] < LOW_CONFIDENCE:
        reasons.append("weak match on the description column")

    for k in ("header_row", "col_name", "col_qty", "col_unit_price", "col_amount",
              "item_area", "subtotal"):
        if k in conf and conf[k] < LOW_CONFIDENCE and \
                f"low confidence: {k}" not in reasons:
            reasons.append(f"low confidence: {k} ({conf[k]:.2f})")
    req = [conf.get(k, 0.0) for k in ("header_row", "col_name", "col_qty",
                                      "col_unit_price", "col_amount", "item_area",
                                      "subtotal")]
    prof["overall_confidence"] = round(min(req), 2)
    prof["needs_review"] = bool(reasons) or prof["overall_confidence"] < LOW_CONFIDENCE
    prof["ambiguities"] = ambiguities
    prof["unmapped_columns"] = []
    prof["warnings"] = []
    if prof["last_item_row"] >= prof["first_item_row"]:
        prof["unmapped_columns"] = _unmapped_columns(ws, prof)
        for u in prof["unmapped_columns"]:
            what = f"header {u['header']!r}" if u["header"] else "no header"
            msg = (f"column {u['column']} ({what}) is not mapped to any role"
                   + (" (inside the table)" if u["in_table"] else " (next to the table)"))
            if u["samples"]:
                msg += (f"; item rows hold {u['samples']!r}"
                        + (" ..." if u["item_rows_with_content"] > len(u["samples"]) else ""))
                if u["in_table"]:
                    msg += ("; fill refuses unless --clear-unmapped or "
                            "--keep-unmapped is given")
            prof["warnings"].append(msg)
    return prof


def analyze(path, sheet=None):
    _check_ext(path)
    wb = load_workbook(path)
    ws = _pick_sheet(wb, sheet)
    prof = analyze_sheet(ws)
    prof["source_file"] = os.path.basename(path)
    if not sheet and len(wb.sheetnames) > 1:
        prof["notes"].append(f"sheet {ws.title!r} chosen automatically among {wb.sheetnames}")
    return prof


def summarize(prof):
    c = prof["confidence"]
    lines = [f"Sheet: {prof['sheet']}"]
    if prof["header_row"] is None:
        lines.append("Header row: NOT FOUND")
    else:
        lines.append(f"Header row: {prof['header_row']} (confidence {c.get('header_row', 0):.2f})")
        cols = ", ".join(f"{k}={v}({c.get('col_' + k, 0):.2f})"
                         for k, v in prof["columns"].items())
        lines.append(f"Columns: {cols}")
        n = prof["last_item_row"] - prof["first_item_row"] + 1
        lines.append(f"Item slots: rows {prof['first_item_row']}-{prof['last_item_row']} "
                     f"({max(n, 0)} slot(s), confidence {c.get('item_area', 0):.2f})")
        t = prof["totals"]
        lines.append("Totals: subtotal row {} ({:.2f}), VAT row {}, total row {}, value column {}".format(
            t["subtotal_row"], c.get("subtotal", 0), t["vat_row"], t["total_row"], t["value_col"]))
        lines.append(f"Footer text rows: {len(prof['footer_rows'])}; merged ranges in "
                     f"item area: {len(prof['merged_in_items'])}")
        ph = ", ".join(f"{p['label']!r}->{p['value_cell']}({p['state']})"
                       for p in prof["placeholders"]) or "none"
        lines.append(f"Label/value cells: {ph}")
    for n in prof["notes"]:
        lines.append(f"Note: {n}")
    for w in prof.get("warnings", []):
        lines.append(f"WARNING: {w}")
    lines.append(f"Overall confidence: {prof['overall_confidence']:.2f}")
    if prof["needs_review"]:
        lines.append("NEEDS REVIEW - do not rely on this detection blindly:")
        for r in prof["review_reasons"]:
            lines.append(f"  - {r}")
        lines.append("  Edit the profile JSON, set \"needs_review\": false, and pass it "
                     "with --profile.")
    else:
        lines.append("Detection looks consistent (still open the result and look at it).")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Formula reference shifting (used when rows are inserted)
# --------------------------------------------------------------------------- #

_REF_RE = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)(\d+)")


def _shift_ref_text(ref, at_row, count):
    def sub(m):
        row = int(m.group(4))
        if row >= at_row:
            row += count
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{row}"
    return _REF_RE.sub(sub, ref)


def shift_formula(formula, host_title, target_title, at_row, count):
    """Shift row references >= at_row by count (Excel 'insert rows' semantics)."""
    try:
        tok = Tokenizer(formula)
    except Exception:
        return formula
    out = []
    for t in tok.items:
        v = t.value
        if t.type == "OPERAND" and t.subtype == "RANGE":
            if "!" in v:
                pre, ref = v.rsplit("!", 1)
                if pre.strip("'").replace("''", "'") == target_title:
                    v = pre + "!" + _shift_ref_text(ref, at_row, count)
            elif host_title == target_title:
                v = _shift_ref_text(v, at_row, count)
        out.append(v)
    return "=" + "".join(out)


def _shift_coord(coord, at_row, count):
    m = re.match(r"^([A-Z]+)(\d+)$", coord)
    if not m:
        return coord
    r = int(m.group(2))
    return f"{m.group(1)}{r + count if r >= at_row else r}"


def _shift_anchor(obj, at_row, count):
    a = getattr(obj, "anchor", None)
    if a is None:
        return
    if isinstance(a, str):
        obj.anchor = _shift_coord(a, at_row, count)
        return
    for attr in ("_from", "to"):
        marker = getattr(a, attr, None)
        if marker is not None and marker.row + 1 >= at_row:
            marker.row += count


def insert_rows(wb, ws, at_row, count, style_row):
    """Insert ``count`` rows before ``at_row`` copying the style of ``style_row``.

    Moves cells, merged ranges, row heights, images, page breaks and the print
    area, and rewrites every formula that points at shifted rows. Returns a list
    of warnings about things that were not shifted.
    """
    warnings = []
    max_row, max_col = ws.max_row, ws.max_column
    merged = [r for r in list(ws.merged_cells.ranges) if r.min_row >= at_row]
    straddle = [r.coord for r in ws.merged_cells.ranges
                if r.min_row < at_row <= r.max_row]
    if straddle:
        warnings.append(f"merged ranges straddle the insertion point: {straddle}")
    for r in merged:
        ws.merged_cells.remove(r)
    if max_row >= at_row:
        ws.move_range(f"A{at_row}:{get_column_letter(max_col)}{max_row}", rows=count)
    for r in merged:
        r.shift(row_shift=count)
        ws.merged_cells.add(r)
    # formulas in all sheets
    for sh in wb.worksheets:
        for row in sh.iter_rows():
            for cell in row:
                if not isinstance(cell, MergedCell) and cell.data_type == "f" \
                        and isinstance(cell.value, str):
                    cell.value = shift_formula(cell.value, sh.title, ws.title,
                                               at_row, count)
    # row heights / hidden flags
    dims = ws.row_dimensions
    for r in range(max_row, at_row - 1, -1):
        if r in dims:
            d = dims[r]
            nd = dims[r + count]
            nd.height, nd.hidden, nd.outlineLevel = d.height, d.hidden, d.outlineLevel
        elif (r + count) in dims:
            nd = dims[r + count]
            nd.height, nd.hidden = None, False
    src_dim = dims[style_row] if style_row in dims else None
    for r in range(at_row, at_row + count):
        nd = dims[r]
        nd.height = src_dim.height if src_dim is not None else None
        nd.hidden = False
    # style / merges of the new rows
    row_merges = [m for m in list(ws.merged_cells.ranges)
                  if m.min_row == m.max_row == style_row]
    for r in range(at_row, at_row + count):
        for c in range(1, max_col + 1):
            src = ws.cell(style_row, c)
            dst = ws.cell(r, c)
            if isinstance(dst, MergedCell):
                continue
            dst._style = copy.copy(src._style)
            dst.value = None
        for m in row_merges:
            ws.merge_cells(start_row=r, end_row=r, start_column=m.min_col,
                           end_column=m.max_col)
    # images, charts, page breaks, print area
    for img in list(getattr(ws, "_images", [])):
        _shift_anchor(img, at_row, count)
    for ch in list(getattr(ws, "_charts", [])):
        _shift_anchor(ch, at_row, count)
    try:
        for b in ws.row_breaks.brk:
            if b.id >= at_row:
                b.id += count
    except Exception:
        pass
    pa = ws.print_area
    if pa:
        try:
            parts = []
            for piece in str(pa).split(","):
                ref = piece.split("!")[-1].replace("$", "")
                cr = CellRange(ref)
                if cr.max_row >= at_row:
                    cr.expand(down=count)
                parts.append(cr.coord)
            ws.print_area = ",".join(parts)
        except Exception:
            warnings.append("print area could not be extended; check it")
    if ws.conditional_formatting and len(ws.conditional_formatting):
        warnings.append("conditional formatting ranges were not shifted; check them")
    if ws.data_validations and ws.data_validations.dataValidation:
        warnings.append("data validation ranges were not shifted; check them")
    return warnings


# --------------------------------------------------------------------------- #
# Fill
# --------------------------------------------------------------------------- #

def _check_ext(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xls":
        raise QuoteError("legacy .xls is not supported; save the template as .xlsx")
    if ext not in (".xlsx", ".xlsm"):
        raise QuoteError(f"unsupported file type {ext!r}; use .xlsx")
    if not os.path.isfile(path):
        raise QuoteError(f"file not found: {path}")


def load_items(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return validate_items(data)


def validate_items(data):
    if not isinstance(data, list):
        raise QuoteError("items must be a JSON list of objects")
    out = []
    for i, it in enumerate(data, 1):
        if not isinstance(it, dict):
            raise QuoteError(f"item {i}: must be an object")
        unknown = set(it) - set(EXPECTED_ITEM_KEYS)
        if unknown:
            raise QuoteError(f"item {i}: unknown key(s) {sorted(unknown)}; allowed: "
                             f"{list(EXPECTED_ITEM_KEYS)}")
        if "name" not in it or _is_blank(it["name"]):
            raise QuoteError(f"item {i}: 'name' is required")
        for k in ("qty", "unit_price"):
            if k not in it or not _is_number(it[k]):
                raise QuoteError(f"item {i}: '{k}' must be a finite number "
                                 f"(got {it.get(k)!r}); write 1200 not \"1,200\"")
        out.append(it)
    return out


def _validate_profile(prof, wb):
    for k in ("sheet", "header_row", "first_item_row", "last_item_row", "columns", "totals"):
        if prof.get(k) in (None, {}):
            raise QuoteError(f"profile is missing '{k}'")
    if prof["sheet"] not in wb.sheetnames:
        raise QuoteError(f"profile sheet {prof['sheet']!r} not in workbook")
    cols = prof["columns"]
    for need in ("name", "qty", "unit_price", "amount"):
        if need not in cols:
            raise QuoteError(f"profile has no '{need}' column; cannot build amount formulas")
    if prof["last_item_row"] < prof["first_item_row"]:
        raise QuoteError("profile has no item slot (last_item_row < first_item_row); "
                         "the template needs at least one pre-formatted item row")
    if not prof.get("style_row"):
        sl = prof["last_item_row"] - prof["first_item_row"] + 1
        prof["style_row"] = prof["first_item_row"] + sl // 2


def _parse_rate(v):
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        v = float(s[:-1]) / 100.0 if s.endswith("%") else float(s)
    v = float(v)
    if v > 1:
        v /= 100.0
    if v < 0 or v > 1:
        raise QuoteError("VAT rate must be between 0 and 1 (or 0-100 percent)")
    return v


def _fmt_rate(rate):
    return format(rate, ".10g")


def _clear_row(ws, row, span):
    n = 0
    for c in span:
        cell = ws.cell(row, c)
        if isinstance(cell, MergedCell):
            continue
        if cell.value is not None:
            cell.value = None
            n += 1
    return n


def fill(template, items, out, profile=None, fields=None, vat_rate=None,
         vat_round=None, allow_missing_fields=False, ignore_unmapped_fields=False,
         unmapped=None, clear_metadata=False):
    """Fill ``template`` and save to ``out``. Returns a report dict.

    ``clear_metadata``: blank the document properties (author, last modified by,
    title, description, ...) that the template carries over into the output.

    ``unmapped``: what to do with content in table columns that no role is mapped
    to - ``None`` (refuse when such content exists), ``"clear"`` or ``"keep"``.
    """
    if unmapped not in (None, "clear", "keep"):
        raise QuoteError("unmapped must be None, 'clear' or 'keep'")
    _check_ext(template)
    if os.path.abspath(template) == os.path.abspath(out):
        raise QuoteError("refusing to overwrite the template; choose another --out")
    items = validate_items(items)
    keep_vba = template.lower().endswith(".xlsm")
    wb = load_workbook(template, keep_vba=keep_vba)
    if profile is None:
        ws0 = _pick_sheet(wb, None)
        profile = analyze_sheet(ws0)
        if profile["needs_review"]:
            raise QuoteError(
                "detection is not confident enough to fill automatically:\n  - "
                + "\n  - ".join(profile["review_reasons"])
                + "\nRun 'analyze', fix the profile by hand, set \"needs_review\": "
                  "false and pass it with --profile.")
    else:
        profile = copy.deepcopy(profile)
        if profile.get("needs_review", False):
            raise QuoteError(
                "the profile still has \"needs_review\": true. Check the profile "
                "against the template, then set it to false.")
    _validate_profile(profile, wb)
    ws = wb[profile["sheet"]]
    cols = profile["columns"]
    tt = profile["totals"]
    orphans = _unnamed_amount_cells(
        ws, profile["last_item_row"], tt.get("value_col") or cols["amount"],
        [tt.get(k) for k in ("subtotal_row", "vat_row", "total_row") if tt.get(k)],
        profile.get("ignore_rows"))
    if orphans:
        raise QuoteError(
            "the template has number/formula cells below the item slots that the "
            "profile does not name as subtotal, VAT or total row (they would be "
            "left stale):\n  - " + "\n  - ".join(
                f"{u['cell']}: {u['value']!r}" for u in orphans)
            + "\nName the row in totals.subtotal_row / vat_row / total_row of the "
              "profile, or list it in \"ignore_rows\" if it must stay as it is.")
    rate = _parse_rate(vat_rate) if vat_rate is not None else \
        (_parse_rate(profile.get("vat_rate")) if profile.get("vat_rate") is not None
         else DEFAULT_VAT_RATE)
    report = {"items": len(items), "inserted_rows": 0, "cleared_slots": 0,
              "cleared_unmapped_cells": 0, "warnings": [], "fields_written": []}

    # items referring to columns the template does not have
    for key, cat in (("spec", "spec"), ("note", "note"), ("unit", "unit")):
        if cat not in cols and any(not _is_blank(i.get(key)) for i in items):
            msg = f"items use '{key}' but the template has no such column"
            if ignore_unmapped_fields:
                report["warnings"].append(msg + " (ignored)")
            else:
                raise QuoteError(msg + "; add the column to the profile or pass "
                                       "--ignore-unmapped-fields")

    first, last = profile["first_item_row"], profile["last_item_row"]
    slots = last - first + 1
    n = len(items)
    t = profile["totals"]
    # table columns that no role is mapped to: if they hold content the caller decides
    um = _unmapped_columns(ws, profile)
    um_content = [u for u in um if u["in_table"] and u["samples"]]
    if um_content and unmapped is None:
        raise QuoteError(
            "the table has column(s) no role is mapped to, with content in the item "
            "rows:\n  - " + "\n  - ".join(
                f"column {u['column']} (header {u['header']!r}): {u['samples']!r}"
                for u in um_content)
            + "\nMap them in the profile (an index column as \"no\"), or pass "
              "--clear-unmapped (empty them) or --keep-unmapped (leave them as is).")
    for u in um:
        if not u["in_table"] and u["samples"]:
            report["warnings"].append(
                f"column {u['column']} next to the table holds {u['samples']!r} in the "
                "item rows; it was left untouched")
    profile["unmapped_policy"] = unmapped
    # insert rows when the template has too few slots
    if n > slots:
        extra = n - slots
        at = last + 1
        w = insert_rows(wb, ws, at, extra, profile["style_row"])
        report["warnings"].extend(w)
        report["inserted_rows"] = extra
        for key in ("subtotal_row", "vat_row", "total_row"):
            if t.get(key) and t[key] >= at:
                t[key] += extra
        for fr in profile.get("footer_rows", []):
            if fr["row"] >= at:
                fr["row"] += extra
        for p in profile.get("placeholders", []):
            p["label_cell"] = _shift_coord(p["label_cell"], at, extra)
            p["value_cell"] = _shift_coord(p["value_cell"], at, extra)
        last += extra
        profile["last_item_row"] = last
    # column span of the item table
    idxs = [_col_idx(c) for c in cols.values()]
    mapped = set(idxs)
    lo, hi = _table_columns(ws, profile["header_row"], cols)
    # whole table width (mapped columns + header-bearing neighbours); with
    # --keep-unmapped only the mapped columns are touched
    span = sorted(mapped) if unmapped == "keep" else list(range(lo, hi + 1))
    fmts = profile.get("number_formats", {})
    qc, pc, ac = cols["qty"], cols["unit_price"], cols["amount"]

    for i in range(first, last + 1):
        idx = i - first
        if idx < n:
            it = items[idx]
            for c in span:
                if c not in mapped:
                    cell = ws.cell(i, c)
                    if not isinstance(cell, MergedCell) and cell.value is not None:
                        cell.value = None
                        report["cleared_unmapped_cells"] += 1
            if "no" in cols:
                _put(ws[f"{cols['no']}{i}"], idx + 1)
            _put(ws[f"{cols['name']}{i}"], it["name"])
            for cat, key in (("spec", "spec"), ("note", "note"), ("unit", "unit")):
                if cat in cols:
                    v = it.get(key)
                    _put(ws[f"{cols[cat]}{i}"], None if _is_blank(v) else v)
            _put(ws[f"{qc}{i}"], it["qty"])
            _put(ws[f"{pc}{i}"], it["unit_price"])
            _put(ws[f"{ac}{i}"], f"={qc}{i}*{pc}{i}", True)
            for cat in ("qty", "unit_price", "amount"):
                cell = ws[f"{cols[cat]}{i}"]
                if cell.number_format == "General" and fmts.get(cat, "General") != "General":
                    cell.number_format = fmts[cat]
        else:
            report["cleared_unmapped_cells"] += _clear_row(ws, i, span)
            report["cleared_slots"] += 1

    # totals
    vcol = t.get("value_col") or ac
    sub_row = t.get("subtotal_row")
    if sub_row:
        sub = _writable(ws, sub_row, _col_idx(vcol))
        _put(sub, f"=SUM({ac}{first}:{ac}{last})", True)
        subref = f"{vcol}{sub_row}"
        if profile["totals"].get("vat_row"):
            vat = _writable(ws, t["vat_row"], _col_idx(vcol))
            if vat_round is None:
                _put(vat, f"={subref}*{_fmt_rate(rate)}", True)
            else:
                _put(vat, f"=ROUND({subref}*{_fmt_rate(rate)},{int(vat_round)})", True)
            for r in (t["vat_row"],):
                for c in ws[r]:
                    if isinstance(c.value, str) and c.data_type != "f":
                        m = re.search(r"(\d+(?:\.\d+)?)\s*%", c.value)
                        if m and abs(float(m.group(1)) / 100 - rate) > 1e-9:
                            report["warnings"].append(
                                f"label {c.value!r} says {m.group(1)}% but the VAT "
                                f"rate used is {rate * 100:g}%; edit the label")
        if t.get("total_row"):
            tot = _writable(ws, t["total_row"], _col_idx(vcol))
            if t.get("vat_row"):
                _put(tot, f"={subref}+{vcol}{t['vat_row']}", True)
            else:
                _put(tot, f"={subref}", True)
        for key, rr in (("subtotal", sub_row), ("vat", t.get("vat_row")),
                        ("total", t.get("total_row"))):
            if rr:
                cell = _writable(ws, rr, _col_idx(vcol))
                if cell.number_format == "General" and fmts.get(key, "General") != "General":
                    cell.number_format = fmts[key]
    else:
        report["warnings"].append("no subtotal row: totals were not written")

    # fields
    for key, value in (fields or {}).items():
        try:
            target = _field_target(ws, profile, key)
        except QuoteError as exc:
            if allow_missing_fields:
                report["warnings"].append(str(exc))
                continue
            raise
        cell = _writable(ws, *target)
        if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value) \
                and any(ch in cell.number_format.lower() for ch in "yd") \
                and cell.number_format != "General":
            value = datetime.date.fromisoformat(value)
        _put(cell, value)
        report["fields_written"].append(f"{key} -> {cell.coordinate}")

    wb.calculation.fullCalcOnLoad = True
    if clear_metadata:
        pr = wb.properties
        pr.creator = ""
        pr.lastModifiedBy = None
        pr.title = None
        pr.description = None
        pr.subject = None
        pr.keywords = None
        pr.category = None
        report["metadata_cleared"] = True
    d = os.path.dirname(os.path.abspath(out))
    os.makedirs(d, exist_ok=True)
    wb.save(out)
    report["profile"] = profile
    return report


def _field_target(ws, profile, key):
    k = norm(key)
    phs = profile.get("placeholders", [])
    exact = [p for p in phs if norm(p["label"]) == k]
    pool = exact
    if not pool and len(k) >= 2:
        pool = [p for p in phs if k in norm(p["label"]) or
                (len(norm(p["label"])) >= 2 and norm(p["label"]) in k)]
    if len(pool) > 1:
        raise QuoteError(f"field {key!r} is ambiguous: matches "
                         f"{[p['label_cell'] for p in pool]}; use a cell address "
                         "like \"B3\" as the key")
    if len(pool) == 1:
        coord = pool[0]["value_cell"]
    elif re.match(r"^[A-Za-z]{1,3}\d+$", key.strip()):
        coord = key.strip().upper()
    else:
        raise QuoteError(f"field {key!r}: no matching label cell found; use a cell "
                         "address like \"B3\" or add it to the profile placeholders")
    m = re.match(r"^([A-Z]+)(\d+)$", coord)
    return int(m.group(2)), _col_idx(m.group(1))


# --------------------------------------------------------------------------- #
# Pure-Python formula evaluation (for verify)
# --------------------------------------------------------------------------- #

class Unsupported(Exception):
    pass


def _round_half_up(x, nd):
    q = Decimal(1).scaleb(-int(nd))
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


class Evaluator:
    def __init__(self, ws):
        self.ws = ws
        self.cache = {}
        self.active = set()

    def cell_value(self, coord):
        coord = coord.replace("$", "").upper()
        if coord in self.cache:
            return self.cache[coord]
        if coord in self.active:
            raise Unsupported(f"circular reference at {coord}")
        c = self.ws[coord]
        v = c.value
        if not isinstance(c, MergedCell) and c.data_type == "f" and isinstance(v, str):
            self.active.add(coord)
            try:
                v = self.eval_formula(v)
            finally:
                self.active.discard(coord)
        elif v is None:
            v = 0.0
        self.cache[coord] = v
        return v

    def range_values(self, rng):
        cr = CellRange(rng.replace("$", "").upper())
        out = []
        for r in range(cr.min_row, cr.max_row + 1):
            for c in range(cr.min_col, cr.max_col + 1):
                v = self.cell_value(f"{get_column_letter(c)}{r}")
                if _is_number(v):
                    out.append(float(v))
        return out

    def eval_formula(self, formula):
        tok = Tokenizer(formula)
        parts = []
        for t in tok.items:
            ty, st, v = t.type, t.subtype, t.value
            if ty == "WSPACE":
                continue
            if ty == "OPERAND":
                if st == "RANGE":
                    if "!" in v:
                        raise Unsupported("cross-sheet reference")
                    parts.append(f"_R({v!r})" if ":" in v else f"_C({v!r})")
                elif st == "NUMBER":
                    parts.append(v)
                else:
                    raise Unsupported(f"operand {v!r}")
            elif ty == "FUNC":
                if st == "OPEN":
                    name = v[:-1].upper()
                    if name not in ("SUM", "ROUND"):
                        raise Unsupported(f"function {name}")
                    parts.append(f"_F_{name}(")
                else:
                    parts.append(")")
            elif ty == "SEP":
                parts.append(",")
            elif ty == "PAREN":
                parts.append(v)
            elif ty in ("OPERATOR-INFIX", "OPERATOR-PREFIX"):
                if v not in ("+", "-", "*", "/"):
                    raise Unsupported(f"operator {v}")
                parts.append(v)
            elif ty == "OPERATOR-POSTFIX" and v == "%":
                parts.append("/100")
            else:
                raise Unsupported(f"token {v!r}")
        expr = "".join(parts)
        try:
            node = ast.parse(expr, mode="eval").body
        except SyntaxError:
            raise Unsupported("unparsable formula")
        return self._ev(node)

    def _ev(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = self._ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = self._ev(node.left), self._ev(node.right)
            if not (_is_number(a) and _is_number(b)):
                raise Unsupported("non-numeric operand")
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if b == 0:
                raise Unsupported("division by zero")
            return a / b
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            args = [self._ev(a) for a in node.args]
            if name == "_C":
                v = self.cell_value(args[0])
                if not _is_number(v):
                    raise Unsupported(f"non-numeric cell {args[0]}")
                return float(v)
            if name == "_R":
                return self.range_values(args[0])
            if name == "_F_SUM":
                total = 0.0
                for a in args:
                    total += math.fsum(a) if isinstance(a, list) else a
                return total
            if name == "_F_ROUND" and len(args) == 2:
                return _round_half_up(args[0], args[1])
        raise Unsupported("unsupported expression")


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #

def _fin(findings, level, code, msg, cell=None):
    findings.append({"level": level, "code": code, "message": msg, "cell": cell})


def _norm_formula(f):
    return re.sub(r"[\s$]", "", f).upper()


def _close(a, b):
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def verify(result, profile=None, items=None, vat_rate=None):
    _check_ext(result)
    wb = load_workbook(result)
    if profile is None:
        ws = _pick_sheet(wb, None)
        profile = analyze_sheet(ws)
        auto = True
    else:
        if profile["sheet"] not in wb.sheetnames:
            raise QuoteError(f"profile sheet {profile['sheet']!r} not in workbook")
        ws = wb[profile["sheet"]]
        auto = False
    F = []
    if auto:
        _fin(F, "WARN", "auto_profile",
             "no profile was given or found next to the file, so the structure was "
             "re-detected from the result; item names such as 'Total', 'Subtotal' or "
             "'VAT' can be mistaken for totals rows and cause false errors. Pass the "
             "profile with --profile ('fill' writes <result>.profile.json next to "
             "the result)")
    if auto and profile["needs_review"]:
        _fin(F, "WARN", "profile_uncertain",
             "structure of the result could not be detected with confidence "
             "(pass the profile used for filling with --profile): "
             + "; ".join(profile["review_reasons"]))
    if profile.get("header_row") is None or "columns" not in profile:
        _fin(F, "ERROR", "no_structure", "no item table found")
        return {"findings": F, "computed": {}, "ok": False}
    cols = profile["columns"]
    for need in ("name", "qty", "unit_price", "amount"):
        if need not in cols:
            _fin(F, "ERROR", "no_column", f"no '{need}' column in the profile")
    if any(f["level"] == "ERROR" for f in F):
        return {"findings": F, "computed": {}, "ok": False}
    first, last = profile["first_item_row"], profile["last_item_row"]
    t = profile["totals"]
    qc, pc, ac = cols["qty"], cols["unit_price"], cols["amount"]
    idxs = [_col_idx(c) for c in cols.values()]
    lo, hi = _table_columns(ws, profile["header_row"], cols)
    span = range(lo, hi + 1)
    keep = profile.get("unmapped_policy") == "keep"
    data_cats = [c for c in ("name", "spec", "qty", "unit_price", "note", "amount", "unit")
                 if c in cols]

    used, blank_seen, gap = [], False, False
    for r in range(first, last + 1):
        vals = {c: ws[f"{cols[c]}{r}"] for c in data_cats}
        has_data = any(not _is_blank(v.value) for v in vals.values())
        if not has_data:
            extra = [ws.cell(r, c).coordinate for c in span
                     if not isinstance(ws.cell(r, c), MergedCell)
                     and not _is_blank(ws.cell(r, c).value)]
            if extra:
                _fin(F, "WARN" if keep else "ERROR", "stale_sample",
                     f"row {r}: content in cells {extra} of an otherwise empty slot "
                     "(stale sample data?)", extra[0])
            blank_seen = True
            continue
        if blank_seen:
            gap = True
        used.append(r)
        if "no" in cols:
            nv = ws[f"{cols['no']}{r}"].value
            if _is_blank(nv):
                _fin(F, "ERROR", "missing_index", f"row {r}: the index column "
                     f"{cols['no']} is empty", f"{cols['no']}{r}")
            elif str(nv).strip() != str(len(used)):
                _fin(F, "WARN", "index_sequence", f"row {r}: index {nv!r}, expected "
                     f"{len(used)}", f"{cols['no']}{r}")
        if _is_blank(vals["name"].value):
            _fin(F, "ERROR", "orphan_row", f"row {r} has numbers or text but no "
                 "description (stale sample row?)", vals["name"].coordinate)
        q, p, a = vals["qty"], vals["unit_price"], vals["amount"]
        for lab, cell in (("qty", q), ("unit price", p)):
            if not _is_number(cell.value):
                _fin(F, "ERROR", "not_numeric", f"{lab} in row {r} is not a number "
                     f"({cell.value!r})", cell.coordinate)
        if a.data_type != "f":
            if _is_blank(a.value):
                _fin(F, "ERROR", "missing_amount", f"amount in row {r} is empty",
                     a.coordinate)
            else:
                _fin(F, "ERROR", "literal_amount", f"amount in row {r} is a literal "
                     f"({a.value!r}), expected a formula", a.coordinate)
        else:
            f = _norm_formula(a.value)
            ok = f in (f"={qc}{r}*{pc}{r}", f"={pc}{r}*{qc}{r}")
            if not ok:
                _fin(F, "ERROR", "wrong_columns", f"amount formula in row {r} is "
                     f"{a.value!r}; expected ={qc}{r}*{pc}{r}", a.coordinate)
    if gap:
        _fin(F, "WARN", "gap", "blank slot(s) between filled item rows")

    vcol = t.get("value_col") or ac
    sub_row = t.get("subtotal_row")
    ev = Evaluator(ws)
    computed = {}
    if not sub_row:
        _fin(F, "ERROR", "no_subtotal", "no subtotal row known")
    else:
        sc = ws[f"{vcol}{sub_row}"]
        if isinstance(sc, MergedCell):
            sc = _writable(ws, sub_row, _col_idx(vcol))
        if sc.data_type != "f":
            _fin(F, "ERROR", "literal_subtotal", f"subtotal is a literal ({sc.value!r})",
                 sc.coordinate)
        else:
            f = _norm_formula(sc.value)
            m = re.fullmatch(r"=SUM\(([A-Z]+)(\d+):([A-Z]+)(\d+)\)", f)
            if not m:
                _fin(F, "ERROR", "subtotal_not_sum",
                     f"subtotal formula {sc.value!r} is not a plain SUM over a column "
                     "range", sc.coordinate)
            else:
                c1, a1, c2, b1 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
                if c1 != ac or c2 != ac:
                    _fin(F, "ERROR", "sum_wrong_column", f"SUM runs over {c1}:{c2}, the "
                         f"amount column is {ac}", sc.coordinate)
                if b1 >= sub_row or a1 <= profile["header_row"]:
                    _fin(F, "ERROR", "sum_bad_range", f"SUM range {a1}-{b1} includes the "
                         "header or the totals", sc.coordinate)
                need_hi = max(used) if used else first
                need_lo = min(used) if used else first
                if a1 > need_lo or b1 < need_hi:
                    _fin(F, "ERROR", "sum_short", f"SUM range rows {a1}-{b1} does not "
                         f"cover all item rows ({need_lo}-{need_hi})", sc.coordinate)
                elif a1 != first or b1 != last:
                    _fin(F, "WARN", "sum_not_full", f"SUM covers rows {a1}-{b1}, the "
                         f"item area is {first}-{last}", sc.coordinate)
        subref = f"{vcol}{sub_row}"
        try:
            computed["subtotal"] = ev.cell_value(subref)
        except Unsupported as exc:
            _fin(F, "INFO", "cannot_recompute", f"subtotal not recomputed: {exc}")
        vat_row, tot_row = t.get("vat_row"), t.get("total_row")
        for key, rr in (("vat", vat_row), ("total", tot_row)):
            if not rr:
                continue
            cell = ws[f"{vcol}{rr}"]
            if isinstance(cell, MergedCell):
                cell = _writable(ws, rr, _col_idx(vcol))
            if cell.data_type != "f":
                _fin(F, "ERROR", f"literal_{key}", f"{key} is a literal ({cell.value!r})",
                     cell.coordinate)
                continue
            f = _norm_formula(cell.value)
            if subref not in f:
                _fin(F, "ERROR", f"{key}_not_linked", f"{key} formula {cell.value!r} does "
                     f"not reference the subtotal {subref}", cell.coordinate)
            if key == "total" and vat_row and f"{vcol}{vat_row}" not in f:
                _fin(F, "ERROR", "total_not_linked", f"total formula {cell.value!r} does "
                     "not include the VAT cell", cell.coordinate)
            try:
                computed[key] = ev.cell_value(f"{vcol}{rr}")
            except Unsupported as exc:
                _fin(F, "INFO", "cannot_recompute", f"{key} not recomputed: {exc}")
        if "subtotal" in computed and "vat" in computed and "total" in computed:
            if not _close(computed["total"], computed["subtotal"] + computed["vat"]):
                _fin(F, "ERROR", "total_mismatch", "total != subtotal + VAT")
        if vat_rate is not None and "subtotal" in computed and "vat" in computed:
            exp = computed["subtotal"] * _parse_rate(vat_rate)
            if abs(exp - computed["vat"]) > 0.5:
                _fin(F, "ERROR", "vat_rate_mismatch",
                     f"VAT {computed['vat']:,.2f} does not match rate {vat_rate} "
                     f"(expected about {exp:,.2f})")

    if items is not None:
        items = validate_items(items)
        if len(items) != len(used):
            _fin(F, "ERROR", "item_count", f"{len(used)} filled row(s) in the sheet, "
                 f"{len(items)} item(s) expected")
        for r, it in zip(used, items):
            nm = ws[f"{cols['name']}{r}"].value
            if str(nm).strip() != str(it["name"]).strip():
                _fin(F, "ERROR", "item_name", f"row {r}: name {nm!r} != {it['name']!r}")
            for cat, key in (("qty", "qty"), ("unit_price", "unit_price")):
                v = ws[f"{cols[cat]}{r}"].value
                if not (_is_number(v) and _close(float(v), float(it[key]))):
                    _fin(F, "ERROR", "item_value", f"row {r}: {key} {v!r} != {it[key]!r}")
            for cat in ("spec", "note"):
                if cat in cols:
                    v = ws[f"{cols[cat]}{r}"].value
                    exp = it.get(cat)
                    if (_is_blank(v) != _is_blank(exp)) or (not _is_blank(exp)
                                                            and str(v) != str(exp)):
                        _fin(F, "ERROR", "item_value", f"row {r}: {cat} {v!r} != {exp!r}")
        exp_sub = math.fsum(float(i["qty"]) * float(i["unit_price"]) for i in items)
        if "subtotal" in computed:
            if not _close(computed["subtotal"], exp_sub):
                _fin(F, "ERROR", "subtotal_value", f"recomputed subtotal "
                     f"{computed['subtotal']:,.2f} != expected {exp_sub:,.2f}")
        computed["expected_subtotal"] = exp_sub
    named_rows = [t.get(k) for k in ("subtotal_row", "vat_row", "total_row") if t.get(k)]
    for u in _unnamed_amount_cells(ws, last, vcol, named_rows, profile.get("ignore_rows")):
        if u["kind"] == "literal":
            extra = ("" if named_rows and t.get("total_row")
                     else " (and no total row is identified at all)")
            _fin(F, "ERROR", "unrecognised_literal",
                 f"{u['cell']} below the item area holds a literal number "
                 f"({u['value']!r}) that is not a recognised subtotal/VAT/total row"
                 + extra, u["cell"])
        else:
            _fin(F, "WARN", "unrecognised_formula",
                 f"{u['cell']} below the item area holds {u['value']!r} in a row that "
                 "is not a recognised subtotal/VAT/total row", u["cell"])
    if not wb.calculation.fullCalcOnLoad:
        _fin(F, "WARN", "no_full_calc", "fullCalcOnLoad is not set; Excel may show "
             "stale or empty values until a recalculation")
    ok = not any(f["level"] == "ERROR" for f in F)
    return {"findings": F, "computed": computed, "ok": ok,
            "item_rows": len(used)}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def sidecar_path(result):
    """Profile saved next to a filled workbook: result.xlsx -> result.profile.json."""
    return os.path.splitext(result)[0] + ".profile.json"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        raise QuoteError(f"cannot read JSON {path}: {exc}")


def build_parser():
    p = argparse.ArgumentParser(
        prog="quote_template",
        description="Analyse a quotation/invoice Excel template and fill it with items.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="detect the layout and write a profile")
    a.add_argument("template")
    a.add_argument("--sheet")
    a.add_argument("--out", help="write the profile JSON here (default: stdout)")
    f = sub.add_parser("fill", help="fill the template with line items")
    f.add_argument("template")
    f.add_argument("--items", required=True)
    f.add_argument("--out", required=True)
    f.add_argument("--profile")
    f.add_argument("--fields")
    f.add_argument("--vat-rate", help="e.g. 0.1 or 10%% (default: profile or 10%%)")
    f.add_argument("--vat-round", type=int, metavar="DIGITS",
                   help="wrap the VAT formula in ROUND(..., DIGITS)")
    f.add_argument("--allow-missing-fields", action="store_true")
    f.add_argument("--ignore-unmapped-fields", action="store_true")
    f.add_argument("--clear-metadata", action="store_true",
                   help="blank author / last-modified-by / title / description in the "
                        "output (they are otherwise copied from the template)")
    g = f.add_mutually_exclusive_group()
    g.add_argument("--clear-unmapped", action="store_true",
                   help="empty table columns no role is mapped to (all slot rows)")
    g.add_argument("--keep-unmapped", action="store_true",
                   help="leave table columns no role is mapped to untouched")
    v = sub.add_parser("verify", help="independently check a filled workbook")
    v.add_argument("result")
    v.add_argument("--profile")
    v.add_argument("--items")
    v.add_argument("--vat-rate")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "analyze":
            prof = analyze(args.template, args.sheet)
            text = json.dumps(prof, ensure_ascii=False, indent=2)
            if args.out:
                os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
                with open(args.out, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                print(summarize(prof))
                print(f"Profile written to {args.out}")
            else:
                print(summarize(prof), file=sys.stderr)
                print(text)
            return 3 if prof["header_row"] is None else 0
        if args.cmd == "fill":
            prof = _read_json(args.profile) if args.profile else None
            fields = _read_json(args.fields) if args.fields else None
            if fields is not None and not isinstance(fields, dict):
                raise QuoteError("fields must be a JSON object {label: value}")
            rep = fill(args.template, load_items(args.items), args.out, prof, fields,
                       args.vat_rate, args.vat_round, args.allow_missing_fields,
                       args.ignore_unmapped_fields,
                       "clear" if args.clear_unmapped else
                       "keep" if args.keep_unmapped else None,
                       args.clear_metadata)
            sc = sidecar_path(args.out)
            side = dict(rep["profile"])
            side["needs_review"] = False
            with open(sc, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(side, ensure_ascii=False, indent=2) + "\n")
            print(f"Profile of the result written to {sc} (verify uses it "
                  "automatically)")
            print(f"Wrote {args.out}: {rep['items']} item(s), "
                  f"{rep['inserted_rows']} row(s) inserted, "
                  f"{rep['cleared_slots']} unused slot(s) cleared")
            for fw in rep["fields_written"]:
                print(f"  field {fw}")
            for w in rep["warnings"]:
                print(f"WARNING: {w}")
            print("Formulas are recalculated when the file is opened. Open it and "
                  "check it by eye; then run 'verify'.")
            return 0
        if args.cmd == "verify":
            prof = _read_json(args.profile) if args.profile else None
            if prof is None and os.path.isfile(sidecar_path(args.result)):
                prof = _read_json(sidecar_path(args.result))
                print(f"Using profile {sidecar_path(args.result)}")
            items = _read_json(args.items) if args.items else None
            res = verify(args.result, prof, items, args.vat_rate)
            for f in res["findings"]:
                loc = f" [{f['cell']}]" if f["cell"] else ""
                print(f"{f['level']}: {f['code']}{loc}: {f['message']}")
            comp = res.get("computed", {})
            if comp:
                print("Recomputed: " + ", ".join(f"{k}={v:,.2f}" for k, v in comp.items()))
            print("RESULT: " + ("OK" if res["ok"] else "PROBLEMS FOUND"))
            return 0 if res["ok"] else 1
    except (QuoteError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
