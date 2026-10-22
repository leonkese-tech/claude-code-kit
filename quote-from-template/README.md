# quote-from-template

Upload (point at) any quotation / invoice style Excel template. The tool
**analyses its layout**, then **fills it with line items** - keeping your styles,
merged cells, row heights, logo and print setup, and keeping every amount,
subtotal, VAT and total as a **live formula** instead of a pasted number.

It ships with no company template, no price list and no customer data. You bring
the template; it learns the layout from the template itself.

- Single file: `quote_template.py` (Python 3.9+, only dependency: `openpyxl`)
- Three commands: `analyze`, `fill`, `verify`
- License: MIT

## Install

```
pip install openpyxl
pip install pillow      # optional, but needed to keep logos / images (see Limits)
```

## Quick start

```
python examples/make_sample_template.py        # writes examples/sample_template.xlsx

python quote_template.py analyze examples/sample_template.xlsx --out profile.json

python quote_template.py fill examples/sample_template.xlsx \
    --items examples/items.json --fields examples/fields.json --out result.xlsx

python quote_template.py verify result.xlsx --items examples/items.json
```

Then **open `result.xlsx` and look at it** (see Safety).

### `analyze TEMPLATE.xlsx [--sheet NAME] [--out profile.json]`

Detects and prints a short summary with a confidence (0-1) for each element, and
writes the full profile as JSON (to `--out`, or to stdout). Exit code 3 if no item
table was found at all.

```
Header row: 6 (confidence 0.95)
Columns: no=A(1.00), name=B(1.00), spec=C(1.00), qty=D(1.00), ...
Item slots: rows 7-11 (5 slot(s), confidence 0.90)
Totals: subtotal row 12 (0.95), VAT row 13, total row 14, value column F
Label/value cells: 'Customer'->B3(empty), 'Date'->F3(empty), ...
Overall confidence: 0.90
```

If anything is ambiguous the summary says `NEEDS REVIEW` and lists why. You then
edit the profile (it is plain JSON), set `"needs_review": false`, and pass it back
with `--profile`.

### `fill TEMPLATE.xlsx --items items.json --out result.xlsx [options]`

```
--profile profile.json     use a (possibly hand-edited) profile instead of auto-detection
--fields fields.json       {"label": "value"} written next to the detected label cells
--vat-rate 0.1             default: profile value (read from a "(10%)" label) or 10%
--vat-round 0              wrap the VAT formula in ROUND(..., digits); default: no rounding
--allow-missing-fields     warn instead of failing when a field label is not found
--ignore-unmapped-fields   drop item keys (spec/note/unit) the template has no column for
--clear-unmapped           empty table columns no role is mapped to (all slot rows)
--keep-unmapped            leave such columns untouched (verify then only warns)
--clear-metadata           blank author / last-modified-by / title / description in the output
```

`fill` also writes `<result>.profile.json` next to the result (for `result.xlsx`:
`result.profile.json`); `verify` picks it up automatically.

**Index column.** A header such as `No`, `No.`, `#`, `S/N`, `Serial No`, `번호`,
`순번` is treated as an index column: `fill` writes plain sequential numbers
1..n (they are labels, not computed amounts) and clears the numbers in unused
slots. **Unmapped columns:** `analyze` warns about every column inside or next
to the table that no role is mapped to, with sample content. If such a column
holds content in the item rows, `fill` refuses (exit 2) until you map it in the
profile or pass `--clear-unmapped` / `--keep-unmapped`. Unused slot rows are
cleared across the whole table width.

`items.json` is a list of objects:

```json
[
  {"name": "Network switch 24-port", "spec": "SW-24", "qty": 2, "unit_price": 450000, "note": "rack ears"},
  {"name": "Installation", "qty": 1, "unit_price": 1800000}
]
```

Allowed keys: `name` (required), `qty` and `unit_price` (required, JSON numbers -
`1200`, not `"1,200"`), `spec`, `note`, `unit`.

`fields.json` keys are matched (case/space/punctuation-insensitive) against the
label cells found by `analyze`: `"Customer"`, `"Date"`, `"Quote No"`, `"수신"`,
`"견적일자"`. You can also use a plain cell address such as `"B3"`. The value goes
into the empty (or placeholder-looking) cell to the right of the label. A field
that matches nothing, or matches two labels, is an **error**, not a silent skip.

What `fill` does:

| Situation | Behaviour |
|---|---|
| items == slots | writes into the slots |
| more items than slots | **inserts rows** before the subtotal: style, borders, number formats, merged ranges and row height are copied; everything below (totals, footer, images, print area, outside formulas) is shifted |
| fewer items than slots | the unused slots are **cleared** (no stale sample data) |
| amount cell | formula `=QTY*PRICE` of the same row |
| subtotal | `=SUM(first_slot:last_slot)` over the full item range |
| VAT | `=SUBTOTAL*rate` (or `ROUND(...)` with `--vat-round`) |
| total | `=SUBTOTAL+VAT` |
| text such as `=1+1` in a name | stored as text, never as a formula |

There is no row cap anywhere: the end of the item area is the subtotal row.
The workbook is flagged `fullCalcOnLoad`, so Excel / LibreOffice recalculates on
open. The template file itself is never modified (`--out` must differ).

### `verify RESULT.xlsx [--profile P] [--items items.json] [--vat-rate R]`

An independent check that does not reuse the fill logic:

- every amount / subtotal / VAT / total cell is a formula (literals are errors)
- amount formulas reference the right columns of their own row
- the SUM covers every filled item row and does not include header or totals
- no orphan / stale rows (numbers without a description, any non-empty cell in an
  unused slot across the table width, including unmapped columns); the index
  column must be filled for every item row
- VAT and total reference the subtotal; total = subtotal + VAT
- with `--items`: the rows match the items, and the subtotal is recomputed **in
  pure Python** from the formulas' inputs and compared with qty x price
- a number or formula in the amount column just below the item slots that is not
  the subtotal, VAT or total row is an error when it is a literal (a stale
  total that was never recognised) and a warning when it is a formula
- warns if `fullCalcOnLoad` is not set

Without `--profile`, `verify` uses `<result>.profile.json` if it sits next to the
file (written by `fill`). If neither exists it re-analyses the result and prints a
warning: item names such as `Total`, `Subtotal` or `VAT` can then be mistaken for
totals rows and produce false errors - pass `--profile` in that case. A profile
saved from the *template* has the template's row numbers; after rows were
inserted use the sidecar (or re-run `analyze` on the result).

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success (`verify`: no errors) |
| 1 | `verify` found problems |
| 2 | refused / invalid input: bad items or profile, ambiguous detection, unmapped columns with content, unsupported file, overwrite of the template |
| 3 | `analyze` found no item table |

## How detection works

1. **Header row** - every row is scored by how many *kinds* of column headers it
   contains, using synonym tables (Korean + English): description
   (`품명/품목/내역/Description/Item`), spec (`규격/Spec/Model`), qty
   (`수량/Qty/Quantity`), unit price (`단가/Unit Price/Price`), amount
   (`금액/공급가액/Amount/Total`), remarks (`비고/Remarks/Note`), plus optional
   number and unit columns. A header needs a description column and at least one
   numeric column. If two or more rows score about equally the result is flagged
   ambiguous (low confidence) instead of guessing.
2. **Totals** - below the header, rows whose label matches
   `소계/합계/부가세/VAT/총액/Subtotal/Total/Grand Total ...` (labels such as
   "Total (VAT excluded)" are handled), or whose amount cell already holds a
   `SUM(` formula, mark the end of the item area. The first such row is the
   subtotal; the following VAT and total rows are classified by label and order.
   A subtotal found only through its SUM formula is accepted with lower confidence.
3. **Item slots** = rows between the header and the subtotal; the style source for
   inserted rows is the middle slot. Merged ranges inside the item area, number
   formats and footer text rows (everything below the totals) are recorded.
4. **Label/value cells** - short text cells that look like labels (known words or a
   trailing colon) followed by an empty, placeholder-looking (`[ ]`, `____`,
   `{{x}}`, `YYYY-MM-DD`) or already filled cell.
5. **Confidence** per element; overall = the minimum over what is required for
   filling. Below 0.6, or with any listed reason, `needs_review` is true and
   `fill` refuses to run without an explicitly reviewed profile.

Profile fields you will most likely edit: `header_row`, `columns` (letters),
`first_item_row`, `last_item_row`, `style_row`, `totals` (`subtotal_row`,
`vat_row`, `total_row`, `value_col`), `vat_rate`, `placeholders[*].value_cell`.

## Tests

```
python -I -m unittest
```

All templates in the tests are synthetic and built with openpyxl: Korean and
English headers, different column orders, fewer / more slots than items, merged
description cells, SUM-only subtotal, two-header ambiguity, a 120-item case with
no row cap, images, and tampering cases for `verify`.

## Limits

- `.xlsx` / `.xlsm` only. **No legacy `.xls`** (save as `.xlsx` first).
- **No pivot tables**; **charts may be lost or distorted** - openpyxl does not
  preserve everything it reads. Data validation / conditional formatting ranges
  below an insertion point are *not* shifted (a warning is printed).
- **Images / logos are kept only if Pillow is installed** (`pip install pillow`);
  without it openpyxl silently drops them. Shapes, text boxes and form controls
  are not preserved.
- **openpyxl does not evaluate formulas.** The output has no cached values until
  Excel / LibreOffice opens it (`fullCalcOnLoad` is set). Viewers that do not
  recalculate (some previewers, scripts that read values) will show blanks.
  `verify` recomputes only simple formulas (`+ - * /`, `SUM`, `ROUND`, `%`).
- Detection needs a **single-row header** and a **subtotal / SUM row below the
  item slots**. Two-row headers, vertical (transposed) layouts, tables without
  any totals row, and per-line tax columns are not handled (the tool says so).
- The value for a label is taken from the cell immediately to its right (after a
  merged label). Values placed below the label, or two cells away, need a cell
  address key in `fields.json`.
- Items are written top-down into the slots; item groups, section headings and
  sub-totals per group are not supported.
- Rounding: no rounding unless `--vat-round` is given; money display formats come
  from the template.

## Safety

**Never trust the output blindly.** Open the result in Excel / LibreOffice, check
the item rows, the totals, the footer and the print preview, and compare the
grand total with an independent calculation before the document leaves your
hands. `verify` finds structural mistakes; it cannot know whether the template's
labels, rates, currency or terms are right for this customer. Keep the original
template unchanged and treat the generated file as a draft.

**Totals the tool cannot name are never skipped silently.** If a number or formula
sits in the amount column right below the item slots but its row is not
recognised as subtotal / VAT / total (unusual label), `analyze` reports
`needs_review`, `fill` refuses (also with a hand-written profile) and `verify`
reports an error for a literal. Name the row in `totals.subtotal_row` / `vat_row`
/ `total_row` of the profile, or list a cell that must stay as it is in
`"ignore_rows": [15]`. The check looks only a few rows below the last known totals
row, so a second table further down is not affected.

**Document properties are copied from the template.** The output keeps the
template's author, last-modified-by, title and description. Clear them before the
file goes to a customer: pass `--clear-metadata` to `fill`, or use
File > Info > Inspect Document in Excel.

## License

MIT - see `LICENSE`.
