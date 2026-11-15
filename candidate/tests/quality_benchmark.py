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


# --- A research document ----------------------------------------------------------------------

RESEARCH_DOCUMENT = {
    "title": "Does Delivery Flexibility Reduce Subscription Churn?",
    "subtitle": "Evidence from a twelve-month field experiment with 6,400 coffee subscribers",
    "author": "Priya Raman and Tomás Ibarra, Customer Insights", "organization": "Northwind Coffee Roasters",
    "date": "October 2026", "theme": "editorial", "page": "a4", "cover": True, "contents": True,
    "summary": "Subscribers offered a choice of delivery day and the option to skip a delivery cancelled 21% less "
               "often over twelve months than subscribers on a fixed schedule (2.4% against 3.0% a month; "
               "difference 0.62 points, 95% interval 0.41 to 0.83). The effect was largest among subscribers "
               "in their first three months and did not reduce order volume.",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Background"},
        {"type": "paragraph", "text": "Subscription churn is the largest single cost in our direct business: at "
                                      "3.0% a month, half of a year's new subscribers are gone within two years. "
                                      "Exit surveys name delivery timing as a reason in 38% of cancellations, more "
                                      "than price (24%) or taste (17%)."},
        {"type": "paragraph", "text": [{"text": "This study tests one intervention: "},
                                       {"text": "letting subscribers choose their delivery day and skip a delivery",
                                        "italic": True},
                                       {"text": " without contacting support."}]},
        {"type": "heading", "level": 1, "text": "Method"},
        {"type": "paragraph", "text": "All 6,400 subscribers who joined between 1 October and 30 November 2025 were "
                                      "assigned at random, in equal numbers, to the flexible schedule or to the fixed "
                                      "schedule then in use. Assignment was stratified by plan size. Subscribers were "
                                      "followed for twelve months; the primary outcome was monthly cancellation."},
        {"type": "table", "caption": "Characteristics of the two groups at the start", "header":
            ["Characteristic", "Flexible", "Fixed"],
         "rows": [["Subscribers", "3,200", "3,200"], ["Median age (years)", "36", "37"],
                  ["Two-bag plan (%)", "41.2", "40.8"], ["Previous customer (%)", "18.5", "19.1"],
                  ["Urban address (%)", "62.0", "61.4"]]},
        {"type": "callout", "tone": "note", "title": "Pre-registration",
         "text": "The outcome, the analysis and the twelve-month horizon were fixed before the first delivery."},
        {"type": "heading", "level": 1, "text": "Results"},
        {"type": "metrics", "items": [
            {"label": "Monthly churn, flexible", "value": "2.38%", "detail": "0.62 pts lower", "trend": "down"},
            {"label": "Monthly churn, fixed", "value": "3.00%", "detail": "Control group", "trend": "flat"},
            {"label": "Bags per subscriber", "value": "1.62", "detail": "No change", "trend": "flat"}]},
        {"type": "chart", "kind": "line", "title": "Subscribers still active, by month",
         "caption": "Share of each group still subscribed at the end of each month.", "suffix": "%",
         "categories": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
         "series": [{"name": "Flexible", "values": [97.4, 95.0, 92.7, 90.6, 88.5, 86.4, 84.4, 82.4, 80.5, 78.6, 76.8,
                                                     75.0]},
                    {"name": "Fixed", "values": [96.6, 93.4, 90.4, 87.6, 84.9, 82.3, 79.8, 77.4, 75.1, 72.9, 70.8,
                                                  68.7]}]},
        {"type": "paragraph", "text": "The difference opened early and kept widening. By month twelve, 75.0% of the "
                                      "flexible group remained against 68.7% of the fixed group: 202 more subscribers "
                                      "per 3,200 enrolled."},
        {"type": "table", "caption": "Monthly churn by tenure", "header":
            ["Months subscribed", "Flexible", "Fixed", "Difference", "95% interval"],
         "rows": [["1 to 3", "2.61%", "3.43%", "0.82", "0.49 to 1.15"],
                  ["4 to 6", "2.37%", "3.02%", "0.65", "0.31 to 0.99"],
                  ["7 to 12", "2.27%", "2.77%", "0.50", "0.18 to 0.82"],
                  ["All months", "2.38%", "3.00%", "0.62", "0.41 to 0.83"]], "total_row": True},
        {"type": "chart", "kind": "bar", "title": "Why subscribers used the flexibility",
         "caption": "Share of flexible-group subscribers using each option at least once.", "suffix": "%",
         "categories": ["Changed delivery day", "Skipped a delivery", "Both", "Neither"],
         "series": [{"name": "Subscribers", "values": [46, 31, 19, 42]}]},
        {"type": "heading", "level": 1, "text": "Discussion"},
        {"type": "paragraph", "text": "Flexibility lowered churn without lowering volume: skipped deliveries were "
                                      "made up within the following two months. The effect is concentrated where "
                                      "churn is highest, among new subscribers."},
        {"type": "heading", "level": 2, "text": "Limitations"},
        {"type": "list", "ordered": False, "items": [
            "One region and one product category; the effect may differ elsewhere.",
            "Subscribers knew which schedule they had, so some of the effect may come from feeling consulted.",
            "Twelve months is long enough to measure churn, not lifetime value."]},
        {"type": "heading", "level": 2, "text": "Recommendation"},
        {"type": "callout", "tone": "success", "title": "Recommendation",
         "text": "Offer the flexible schedule to every subscriber from January 2027."},
        {"type": "heading", "level": 1, "text": "References"},
        {"type": "list", "ordered": True, "items": [
            "Customer Insights. Exit survey of cancelled subscribers, January to September 2025. Internal report.",
            "Kohavi R, Tang D, Xu Y. Trustworthy Online Controlled Experiments. Cambridge University Press; 2020.",
            "Fader PS, Hardie BGS. How to project customer retention. Journal of Interactive Marketing. 2007;21(1)."]},
    ],
}

# --- A Word proposal --------------------------------------------------------------------------

WORD_PROPOSAL = {
    "title": "Proposal: Café Fit-Out and Opening Support for Harbour Street",
    "subtitle": "Prepared for Lena Okafor, Managing Director, Okafor Hospitality",
    "author": "Studio Meridian Design", "organization": "Studio Meridian Design", "date": "12 November 2026",
    "page": "letter", "cover": True, "contents": False,
    "summary": "We propose to design, fit out and open your Harbour Street café in 14 weeks for a fixed fee of "
               "$186,500, with the opening date guaranteed or the final stage fee waived.",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Your goals"},
        {"type": "paragraph", "text": "You want a 60-seat café open before the spring tourist season, with a fit-out "
                                      "that reflects the harbour setting and a counter that can serve 140 covers an "
                                      "hour at peak."},
        {"type": "list", "ordered": False, "items": [
            [{"text": "Open by 1 March 2027. ", "bold": True}, {"text": "Every week later costs about $9,000 in "
                                                                          "lost trade."}],
            [{"text": "Hold the budget. ", "bold": True}, {"text": "A fixed fee, with changes priced before work "
                                                                     "starts."}],
            [{"text": "A space people return to. ", "bold": True}, {"text": "Durable materials, warm light, good "
                                                                              "acoustics."}]]},
        {"type": "heading", "level": 1, "text": "Our approach"},
        {"type": "comparison", "columns": [
            {"title": "What we do", "items": ["Concept and layout", "Permits and contractor management",
                                              "Furniture, lighting and signage", "Staff training for opening week"]},
            {"title": "What you provide", "items": ["Access to the site from 4 December", "Decisions within three "
                                                    "working days", "Your brand guidelines and menu"]}]},
        {"type": "heading", "level": 1, "text": "Timeline"},
        {"type": "steps", "items": [
            {"title": "Weeks 1 to 2: Concept", "text": "Layout, materials board and budget confirmed with you."},
            {"title": "Weeks 3 to 5: Permits", "text": "Building and food-premises approvals lodged and tracked."},
            {"title": "Weeks 6 to 12: Build", "text": "Fit-out by our contractor team, weekly site reviews."},
            {"title": "Weeks 13 to 14: Opening", "text": "Snagging, staff training and a soft opening."}]},
        {"type": "heading", "level": 1, "text": "Fees"},
        {"type": "table", "caption": "Fixed fees by stage, in US dollars, excluding sales tax", "total_row": True,
         "header": ["Stage", "Paid on", "Fee"],
         "rows": [["Concept and design", "Signing", "$24,500"], ["Permits", "Approval lodged", "$11,000"],
                  ["Fit-out and furniture", "Weeks 6, 9 and 12", "$138,000"],
                  ["Opening support", "Opening day", "$13,000"], ["Total", "", "$186,500"]]},
        {"type": "callout", "tone": "info", "title": "Our guarantee",
         "text": "If the café is not ready to open on 1 March 2027 for reasons within our control, the opening "
                 "support fee is waived."},
        {"type": "heading", "level": 1, "text": "Why Studio Meridian"},
        {"type": "quote", "text": "They opened our second site three days early and under budget.",
         "attribution": "Marcus Hale, Owner, Hale & Daughter Bakery"},
        {"type": "paragraph", "text": "This proposal is valid until 15 December 2026. To accept, reply in writing "
                                      "and we will schedule the concept workshop within a week."},
    ],
}

# --- A multilingual document --------------------------------------------------------------------

MULTILINGUAL_DOCUMENT = {
    "title": "Guest Information · Información para huéspedes · معلومات للضيوف · ご宿泊のご案内",
    "subtitle": "Harbour Street Hotel", "organization": "Harbour Street Hotel", "date": "2027",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Welcome"},
        {"type": "paragraph",
         "text": "Breakfast is served from 7:00 to 10:30 in the Harbour Room on the ground floor."},
        {"type": "table", "header": ["Service", "Hours", "Extension"],
         "rows": [["Reception", "24 hours", "0"], ["Breakfast", "7:00–10:30", "21"], ["Spa", "9:00–20:00", "35"]]},
        {"type": "heading", "level": 1, "text": "Bienvenidos"},
        {"type": "paragraph", "text": "El desayuno se sirve de 7:00 a 10:30 en el salón Harbour, en la planta baja."},
        {"type": "heading", "level": 1, "text": "أهلاً وسهلاً"},
        {"type": "paragraph", "text": "يُقدَّم الإفطار من الساعة 7:00 حتى 10:30 في قاعة هاربر بالطابق الأرضي."},
        {"type": "callout", "tone": "info", "title": "خدمة الغرف",
         "text": "اطلب خدمة الغرف على الرقم 9 على مدار الساعة."},
        {"type": "heading", "level": 1, "text": "ようこそ"},
        {"type": "paragraph", "text": "朝食は1階のハーバールームにて、7時から10時30分までご用意しております。"},
        {"type": "heading", "level": 1, "text": "स्वागत है"},
        {"type": "paragraph", "text": "नाश्ता भूतल पर हार्बर रूम में सुबह 7:00 से 10:30 बजे तक परोसा जाता है।"},
        {"type": "heading", "level": 1, "text": "Добро пожаловать"},
        {"type": "paragraph", "text": "Завтрак подаётся с 7:00 до 10:30 в зале Harbour на первом этаже."},
    ],
}

# --- A complex edit -----------------------------------------------------------------------------
#
# Applied to the business report (tests/test_document_design.py REPORT) as tracked changes, and to the
# forecast workbook: the figures are restated after an audit, a paragraph explains why, a table row is
# added, and a workbook input is changed so every dependent figure is recalculated.


def report_edit(outline):
    """The operations of the complex edit, with addresses read from the report's outline."""
    def paragraph(starts):
        return next(u.address for u in outline.units if u.kind == "paragraph" and u.text.startswith(starts)
                    and "\n" not in u.text)
    table = next(u.address for u in outline.units if u.kind == "cell" and u.text == "Line item").split(".")[0]
    return [
        {"op": "replace_text", "find": "$18.4M", "replace": "$18.6M"},
        {"op": "replace_text", "find": "18,400", "replace": "18,600", "expect": 1},
        {"op": "insert_paragraph", "after": paragraph("2026 was the strongest year"),
         "text": "Figures restated on 14 November 2026 after the year-end audit moved $0.2M of December wholesale "
                 "revenue into 2026."},
        {"op": "add_row", "after": f"{table}.r3", "cells": ["Audit adjustment", "0", "200", "n/a"]},
        {"op": "replace_text", "find": "strongest", "replace": "strongest and most profitable", "expect": 1},
    ]


WORKBOOK_EDIT = [{"op": "set_cell", "at": "Inputs!B2", "value": 1500},
                 {"op": "replace_text", "find": "Paying accounts on 1 January 2027",
                  "replace": "Paying accounts on 1 January 2027, after the pre-launch campaign"}]
