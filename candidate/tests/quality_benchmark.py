"""The document-quality benchmark: realistic professional files Kalillac must produce well.

Shared by the automated tests and by tools/render_benchmark.py, which writes every
benchmark file to a folder for a person to open and judge. Content is synthetic.
"""
from __future__ import annotations

from datetime import date

# --- A financial forecasting workbook with cross-sheet calculations -------------------------

MONTHS = 36
START = date(2027, 1, 1)


def _month(index):
    year, month = divmod(START.month - 1 + index, 12)
    return date(START.year + year, month + 1, 1).isoformat()


def _forecast_rows():
    rows = []
    for index in range(MONTHS):
        r = index + 2  # Sheet row of this month.
        previous = r - 1
        customers = ("=Inputs!$B$2" if index == 0 else
                     f"=ROUND(B{previous}*(1+Inputs!$B$3)*(1-Inputs!$B$5),0)")
        rows.append([
            _month(index),
            {"formula": customers},
            {"formula": f"=B{r}*Inputs!$B$4"},
            {"formula": f"=-C{r}*Inputs!$B$6"},
            {"formula": f"=C{r}+D{r}"},
            {"formula": f"=-Inputs!$B$7*(1+Inputs!$B$8)^INT({index}/12)"},
            {"formula": f"=E{r}+F{r}"},
            {"formula": f"=IFERROR(G{r}/C{r},0)"},
            {"formula": f"=YEAR(A{r})"},
        ])
    return rows


def _summary_rows():
    rows = []
    for offset, year in enumerate((2027, 2028, 2029)):
        r = offset + 2
        rows.append([
            str(year),
            {"formula": f"=SUMIF(Forecast!$I$2:$I$37,{year},Forecast!$C$2:$C$37)"},
            {"formula": f"=SUMIF(Forecast!$I$2:$I$37,{year},Forecast!$E$2:$E$37)"},
            {"formula": f"=SUMIF(Forecast!$I$2:$I$37,{year},Forecast!$G$2:$G$37)"},
            {"formula": f"=IFERROR(D{r}/B{r},0)"},
            {"formula": "=0" if offset == 0 else f"=IFERROR(B{r}/B{r - 1}-1,0)"},
        ])
    rows.append(["Total", {"formula": "=SUM(B2:B4)"}, {"formula": "=SUM(C2:C4)"}, {"formula": "=SUM(D2:D4)"},
                 {"formula": "=IFERROR(D5/B5,0)"}, None])
    return rows


def _loan_rows():
    rows = []
    for period in range(1, 25):
        r = period + 1
        opening = "=Inputs!$B$10" if period == 1 else f"=F{r - 1}"
        rows.append([
            period,
            {"formula": opening},
            {"formula": f"=-PMT(Inputs!$B$11/12,Inputs!$B$12,Inputs!$B$10)"},
            {"formula": f"=-IPMT(Inputs!$B$11/12,A{r},Inputs!$B$12,Inputs!$B$10)"},
            {"formula": f"=-PPMT(Inputs!$B$11/12,A{r},Inputs!$B$12,Inputs!$B$10)"},
            {"formula": f"=MAX(0,ROUND(B{r}-E{r},2))"},
        ])
    rows.append(["Total", None, {"formula": "=SUM(C2:C25)"}, {"formula": "=SUM(D2:D25)"},
                 {"formula": "=SUM(E2:E25)"}, None])
    return rows


FORECAST_WORKBOOK = {
    "title": "Lumen Analytics: Three-Year Operating Forecast",
    "author": "Finance",
    "summary": "A monthly operating forecast for 2027 to 2029 driven entirely by the assumptions on the Inputs "
               "sheet. Change any yellow cell and every figure, total and chart updates. The Loan sheet "
               "schedules the equipment loan; the Dashboard summarises the plan.",
    "sheets": [
        {"name": "Dashboard", "kind": "dashboard", "title": "Operating plan 2027–2029",
         "description": "Key results of the forecast. Every figure is a live formula.",
         "metrics": [
             {"label": "Revenue, 3 years", "formula": "=Summary!B5", "format": "currency"},
             {"label": "EBITDA, 3 years", "formula": "=Summary!D5", "format": "currency"},
             {"label": "EBITDA margin", "formula": "=Summary!E5", "format": "percent"},
             {"label": "Customers at end", "formula": "=Forecast!B37", "format": "integer"},
             {"label": "Revenue growth 2029", "formula": "=Summary!F4", "format": "percent"},
             {"label": "NPV of EBITDA", "formula": "=NPV(Inputs!B9/12,Forecast!G2:G37)", "format": "currency"},
             {"label": "Loss-making months", "formula": "=COUNTIF(Forecast!G2:G37,\"<0\")", "format": "integer"},
             {"label": "Loan interest", "formula": "=Loan!D26", "format": "currency"}],
         "charts": [
             {"kind": "column", "sheet": "Summary", "title": "Revenue and EBITDA by year", "category": "Year",
              "series": ["Revenue", "EBITDA"]},
             {"kind": "line", "sheet": "Forecast", "title": "Monthly revenue", "category": "Month",
              "series": ["Revenue"]}]},
        {"name": "Inputs", "description": "Every assumption the model uses. Change the yellow cells.",
         "columns": [{"header": "Assumption", "format": "text", "width": 34},
                     {"header": "Value", "format": "number", "role": "input", "width": 16},
                     {"header": "Note", "format": "text", "width": 46}],
         "rows": [
             ["Customers at launch", {"value": 1200, "format": "integer"}, "Paying accounts on 1 January 2027"],
             ["Monthly new-customer growth", {"value": 0.06, "format": "percent"}, "Before churn"],
             ["Average revenue per customer", {"value": 49, "format": "currency"}, "Per month"],
             ["Monthly churn", {"value": 0.021, "format": "percent"}, "Share of customers lost each month"],
             ["Cost of revenue", {"value": 0.22, "format": "percent"}, "Hosting, support and payment fees"],
             ["Operating costs", {"value": 95000, "format": "currency"}, "Per month in 2027"],
             ["Annual cost inflation", {"value": 0.08, "format": "percent"}, "Applied each January"],
             ["Discount rate", {"value": 0.12, "format": "percent"}, "Annual, for net present value"],
             ["Equipment loan", {"value": 180000, "format": "currency"}, "Drawn on 1 January 2027"],
             ["Loan interest rate", {"value": 0.075, "format": "percent"}, "Annual, fixed"],
             ["Loan term (months)", {"value": 24, "format": "integer"}, "Repaid monthly"]],
         "validation": [{"column": "Value", "min": 0}]},
        {"name": "Forecast", "description": "Month by month. Costs are negative.",
         "columns": [{"header": "Month", "format": "date"}, {"header": "Customers", "format": "integer"},
                     {"header": "Revenue", "format": "currency"}, {"header": "Cost of revenue", "format": "currency"},
                     {"header": "Gross profit", "format": "currency"},
                     {"header": "Operating costs", "format": "currency"}, {"header": "EBITDA", "format": "currency"},
                     {"header": "EBITDA margin", "format": "percent"}, {"header": "Year", "format": "integer"}],
         "rows": _forecast_rows(),
         "conditional": [{"column": "EBITDA", "rule": "negative"}, {"column": "EBITDA margin", "rule": "color_scale"},
                         {"column": "Revenue", "rule": "data_bar"}],
         "charts": [{"kind": "line", "title": "Revenue and EBITDA", "category": "Month",
                     "series": ["Revenue", "EBITDA"]}]},
        {"name": "Summary", "description": "Annual totals from the Forecast sheet.", "total_row": True,
         "columns": [{"header": "Year", "format": "text"}, {"header": "Revenue", "format": "currency"},
                     {"header": "Gross profit", "format": "currency"}, {"header": "EBITDA", "format": "currency"},
                     {"header": "EBITDA margin", "format": "percent"}, {"header": "Revenue growth", "format": "percent"}],
         "rows": _summary_rows(),
         "charts": [{"kind": "column", "title": "EBITDA by year", "category": "Year", "series": ["EBITDA"]}]},
        {"name": "Loan", "description": "Equipment loan amortisation.", "total_row": True,
         "columns": [{"header": "Period", "format": "integer"}, {"header": "Opening balance", "format": "currency"},
                     {"header": "Payment", "format": "currency"}, {"header": "Interest", "format": "currency"},
                     {"header": "Principal", "format": "currency"}, {"header": "Closing balance", "format": "currency"}],
         "rows": _loan_rows()},
    ],
}


def expected_forecast():
    """The forecast computed independently in plain Python, for checking the workbook's values."""
    customers, growth, arpu, churn, cogs = 1200, 0.06, 49.0, 0.021, 0.22
    opex, inflation = 95000.0, 0.08
    months = []
    for index in range(MONTHS):
        if index:
            value = customers * (1 + growth) * (1 - churn)
            customers = int(value + 0.5) if value >= 0 else -int(-value + 0.5)
        revenue = customers * arpu
        gross = revenue - revenue * cogs
        operating = opex * (1 + inflation) ** (index // 12)
        months.append({"customers": customers, "revenue": revenue, "gross": gross, "ebitda": gross - operating})
    years = {}
    for index, month in enumerate(months):
        year = START.year + index // 12
        bucket = years.setdefault(year, {"revenue": 0.0, "gross": 0.0, "ebitda": 0.0})
        for key in bucket:
            bucket[key] += month[key]
    return months, years


def expected_loan():
    principal, rate, term = 180000.0, 0.075 / 12, 24
    payment = principal * rate / (1 - (1 + rate) ** -term)
    balance, interest_total = principal, 0.0
    for _ in range(term):
        interest = balance * rate
        interest_total += interest
        balance -= payment - interest
    return payment, interest_total


# --- An executive presentation with visual storytelling ------------------------------------

EXECUTIVE_DECK = {
    "title": "Northwind 2027: From One Roastery to Two",
    "subtitle": "The case for doubling capacity, and how we will do it",
    "organization": "Northwind Coffee Roasters",
    "author": "Dana Whitfield, Chief Executive",
    "date": "Board meeting, December 2026",
    "slides": [
        {"layout": "title", "title": "Northwind 2027: From One Roastery to Two",
         "subtitle": "The case for doubling capacity, and how we will do it",
         "notes": "Open with the decision we need today: approval of the capital plan and the Riverside lease."},
        {"layout": "statement", "eyebrow": "The decision",
         "text": "Approve $4.2M to build a second roastery and keep growing at 20% a year."},
        {"layout": "section", "title": "Where we are", "subtitle": "A record year that has run out of room"},
        {"layout": "metrics", "title": "2026 was the strongest year in our history",
         "metrics": [{"label": "Revenue", "value": "$18.4M", "detail": "23% vs 2025", "trend": "up"},
                     {"label": "Gross margin", "value": "41.6%", "detail": "2.1 points", "trend": "up"},
                     {"label": "Subscribers", "value": "31,200", "detail": "18% vs 2025", "trend": "up"},
                     {"label": "Monthly churn", "value": "2.4%", "detail": "0.6 points", "trend": "down"}],
         "note": "Growth came from every channel; wholesale is now more than half of revenue."},
        {"layout": "chart", "title": "Quarterly revenue rose every quarter",
         "chart": {"kind": "column", "categories": ["Q1", "Q2", "Q3", "Q4"], "prefix": "$", "suffix": "M",
                   "series": [{"name": "2025", "values": [3.31, 3.62, 3.84, 4.23]},
                              {"name": "2026", "values": [4.05, 4.41, 4.69, 5.25]}]},
         "takeaway": "Q4 2026 revenue was 24% above Q4 2025, and the gap widened in every quarter."},
        {"layout": "chart", "title": "Wholesale now funds the rest of the business",
         "chart": {"kind": "donut", "categories": ["Wholesale", "Subscriptions", "Cafes", "Online store"],
                   "prefix": "$", "suffix": "M", "series": [{"name": "Revenue", "values": [9.57, 5.15, 2.39, 1.29]}]},
         "takeaway": "52% of revenue at the lowest acquisition cost of any channel."},
        {"layout": "points", "title": "Three things drove the year",
         "lead": "None of them depended on discounting.",
         "points": ["Forty-two new wholesale accounts, including two regional grocery chains",
                    "Flexible delivery schedules cut subscription churn from 3.0% to 2.4%",
                    "A 4% list-price increase in March held without measurable volume loss"]},
        {"layout": "section", "title": "The constraint", "subtitle": "Capacity, not demand, now limits growth"},
        {"layout": "chart", "title": "The roastery is close to its ceiling",
         "chart": {"kind": "line", "categories": ["Q1 25", "Q2 25", "Q3 25", "Q4 25", "Q1 26", "Q2 26", "Q3 26",
                                                  "Q4 26"], "suffix": "%",
                   "series": [{"name": "Capacity used", "values": [64, 68, 71, 76, 79, 83, 87, 91]}]},
         "takeaway": "At 91% utilisation, 2027 growth is capped at about 8% without new capacity."},
        {"layout": "two_columns", "title": "Two ways to add capacity",
         "left": {"heading": "Build a second roastery",
                  "points": ["Adds 2,400 tonnes a year", "$4.2M over two years", "Payback in 3.4 years",
                             "Shorter routes to southern accounts"]},
         "right": {"heading": "Outsource overflow roasting",
                   "points": ["No capital required", "Margin falls 9 points on overflow volume",
                              "Quality depends on a third party", "Short contracts only"]}},
        {"layout": "table", "title": "Building wins on every measure that lasts", "total_row": False,
         "header": ["Measure", "Build", "Outsource"],
         "rows": [["Capital required", "$4.2M", "$0"], ["Gross margin on new volume", "41%", "32%"],
                  ["Five-year profit", "$11.8M", "$6.1M"], ["Control of quality", "Full", "Contractual"],
                  ["Time to first output", "9 months", "2 months"]]},
        {"layout": "timeline", "title": "From approval to full capacity in twelve months",
         "steps": [{"title": "Secure the site", "text": "Sign the Riverside lease and complete permits"},
                   {"title": "Order equipment", "text": "Two 60 kg drum roasters, 26-week lead time"},
                   {"title": "Hire and train", "text": "Six production roles trained in our roastery"},
                   {"title": "Commission", "text": "First production in Q3 2027"},
                   {"title": "Full capacity", "text": "By December 2027"}]},
        {"layout": "quote", "quote": "We stopped treating wholesale as the low-margin channel and started treating "
                                     "it as the channel that pays for everyone else's growth.",
         "attribution": "Dana Whitfield, Chief Executive"},
        {"layout": "closing", "title": "We ask the board to approve the plan",
         "subtitle": "$4.2M capital budget and the Riverside lease"},
    ],
}
