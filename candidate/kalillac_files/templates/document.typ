// Kalillac document design system.
//
// The document arrives as JSON in sys.inputs.doc, already validated by
// kalillac_files.spec. Everything in it is data: text is placed with
// text(...) and never evaluated as markup, and nothing here reads a file
// or a package.

#import "art.typ": cover-art
#let data = json(bytes(sys.inputs.at("doc")))
#let opt(d, key) = d.at(key, default: none)

// --- tokens -----------------------------------------------------------------------------
#let ink = rgb("#0e1526")
#let navy = rgb("#0b1737")
#let blue = rgb("#0069ff")
#let blue-deep = rgb("#0053cf")
#let blue-soft = rgb("#eaf3ff")
#let muted = rgb("#5c677e")
#let hairline = rgb("#e2e9f4")
#let rule = rgb("#cdd8ea")
#let paper = rgb("#f6f8fc")
#let body-ink = rgb("#1d2538")

#let body-fonts = data.fonts.body
#let display-fonts = data.fonts.display
#let mono-fonts = data.fonts.mono
#let editorial = data.theme == "editorial"
#let margin-x = 22mm

// --- direction ----------------------------------------------------------------------------
// Everything positional is expressed against the document's reading direction, so a
// right-to-left document is a true mirror image, not a left-to-right page with reversed text.
#let rtl-doc = data.rtl
#let start-edge = if rtl-doc { right } else { left }
#let toward-end(v) = if rtl-doc { -v } else { v }
#let start-stroke(st) = if rtl-doc { (right: st) } else { (left: st) }
#let end-radius(r) = if rtl-doc { (left: r) } else { (right: r) }
#let start-inset(v, ..rest) = if rtl-doc { (right: v, ..rest.named()) } else { (left: v, ..rest.named()) }
// A paragraph, cell or item whose own script runs the other way keeps its own direction.
#let directed(d, body) = if d == none { body } else {
  set text(dir: if d == "rtl" { rtl } else { ltr })
  body
}

#set document(title: data.plain-title, author: data.meta-author, date: none)
#set text(font: body-fonts, size: if editorial { 10.5pt } else { 10pt }, fill: body-ink, lang: data.lang,
  hyphenate: editorial, number-type: "lining")
#set par(justify: editorial, leading: if editorial { 0.7em } else { 0.74em },
  spacing: if editorial { 1.05em } else { 1.1em })

// --- rich text ----------------------------------------------------------------------------
#let rich(runs) = {
  for r in runs {
    if r.at("par", default: false) { parbreak() }
    else if r.at("br", default: false) { linebreak() }
    else if r.at("code", default: false) {
      box(fill: paper, stroke: 0.5pt + hairline, inset: (x: 3pt, y: 0pt), outset: (y: 2.6pt), radius: 2pt,
        text(font: mono-fonts, size: 0.88em, fill: ink, r.text))
    } else {
      text(weight: if r.at("bold", default: false) { "semibold" } else { "regular" },
        style: if r.at("italic", default: false) { "italic" } else { "normal" },
        fill: if r.at("bold", default: false) { ink } else { body-ink }, r.text)
    }
  }
}

// --- headings -----------------------------------------------------------------------------
#show heading: set text(font: display-fonts, fill: navy, hyphenate: false)
#show heading.where(level: 1): it => block(above: 1.9em, below: 1.0em, sticky: true, {
  text(size: 19pt, weight: "bold", tracking: -0.012em, it.body)
  v(-0.42em)
  line(length: 30pt, stroke: (paint: blue, thickness: 2.4pt, cap: "round"))
})
#show heading.where(level: 2): it => block(above: 1.55em, below: 0.75em, sticky: true,
  text(size: 13.5pt, weight: "semibold", tracking: -0.008em, it.body))
#show heading.where(level: 3): it => block(above: 1.35em, below: 0.6em, sticky: true,
  text(font: body-fonts, size: 10.5pt, weight: "semibold", fill: blue-deep, it.body))

// --- lists --------------------------------------------------------------------------------
#set list(marker: box(baseline: -0.32em, circle(radius: 1.7pt, fill: blue)), indent: 2pt, body-indent: 9pt,
  spacing: 0.72em)
#set enum(numbering: n => text(fill: blue-deep, weight: "semibold", number-width: "tabular", str(n) + "."),
  indent: 2pt, body-indent: 8pt, spacing: 0.72em)

// --- raw / code ---------------------------------------------------------------------------
#show raw: set text(font: mono-fonts, size: 8.4pt, fill: ink)

// --- figures ------------------------------------------------------------------------------
#show figure.caption: it => block(width: 100%, above: 0.7em, align(start, text(size: 8.5pt, fill: muted, {
  text(weight: "semibold", fill: navy, [#it.supplement #context it.counter.display(it.numbering)])
  h(0.5em)
  it.body
})))
#set figure(gap: 0pt)

// --- blocks -------------------------------------------------------------------------------
#let tones = (
  info: (accent: blue, deep: blue-deep, soft: blue-soft),
  success: (accent: rgb("#12a15c"), deep: rgb("#0b7a44"), soft: rgb("#e7f7ee")),
  warning: (accent: rgb("#e08a00"), deep: rgb("#8a5600"), soft: rgb("#fff5df")),
  danger: (accent: rgb("#d92d20"), deep: rgb("#a1221a"), soft: rgb("#fdeeec")),
  note: (accent: rgb("#8793ab"), deep: rgb("#3b4660"), soft: paper),
)

#let callout(b) = {
  let tone = tones.at(b.tone)
  // A callout written in the other direction from the document reads, and is marked, from its own side.
  let reversed = if opt(b, "dir") == "rtl" { not rtl-doc } else if opt(b, "dir") == "ltr" { rtl-doc } else { false }
  let from-right = rtl-doc != reversed
  directed(opt(b, "dir"), block(width: 100%, above: 1.35em, below: 1.35em, fill: tone.soft,
    radius: if from-right { (left: 5pt) } else { (right: 5pt) },
    stroke: if from-right { (right: 3pt + tone.accent) } else { (left: 3pt + tone.accent) },
    inset: (left: 14pt, right: 14pt, top: 11pt, bottom: 12pt), {
      if opt(b, "title") != none {
        block(below: 0.7em, text(weight: "semibold", fill: tone.deep, b.title))
      }
      rich(b.runs)
    }))
}

#let trend-mark(trend, color) = box(baseline: -0.5pt, {
  if trend == "up" { polygon(fill: color, (0pt, 5.2pt), (3.2pt, 0pt), (6.4pt, 5.2pt)) }
  else if trend == "down" { polygon(fill: color, (0pt, 0pt), (3.2pt, 5.2pt), (6.4pt, 0pt)) }
  else { rect(width: 6.4pt, height: 1.6pt, fill: color) }
})

// Cards in a row share the height of the tallest one, measured at the real column width.
#let card-row(items, gutter, card) = layout(size => {
  let n = items.len()
  let width = (size.width - gutter * (n - 1)) / n
  let height = calc.max(..items.map(item => measure(width: width, card(item, auto)).height))
  grid(columns: (width,) * n, column-gutter: gutter, ..items.map(item => card(item, height)))
})

#let metric-card(m, height) = block(width: 100%, height: height, inset: (x: 13pt, top: 12pt, bottom: 13pt),
  radius: 6pt, stroke: 0.75pt + hairline, fill: white, {
    set par(justify: false, leading: 0.5em, spacing: 0pt)
    block(below: 9pt, text(size: 7.3pt, weight: "medium", fill: muted, tracking: 0.055em, upper(m.label)))
    block(above: 0pt, below: 0pt, text(font: display-fonts, size: m.size * 1pt, weight: "bold", fill: navy,
      tracking: -0.02em, m.value))
    if opt(m, "detail") != none {
      let trend = opt(m, "trend")
      let color = if trend == "up" { rgb("#0b7a44") } else if trend == "down" { rgb("#a1221a") } else { muted }
      block(above: 9pt, below: 0pt, text(size: 8pt, fill: color, weight: "medium", {
        if trend != none { trend-mark(trend, color); h(4pt) }
        m.detail
      }))
    }
  })

#let metrics(b) = block(width: 100%, above: 1.4em, below: 1.4em, breakable: false,
  card-row(b.items.map(m => m + (size: b.size)), 9pt, metric-card))

#let data-table(b) = {
  // Quantities align on the physical right in either direction, so their digits line up.
  let aligns = b.align.map(a => if a == "right" { right } else if a == "center" { center } else { start })
  let dirs = opt(b, "dirs")
  let cell-dir(i, j) = if dirs == none or dirs.at(i).at(j) == "" { none } else { dirs.at(i).at(j) }
  let count = b.rows.len()
  let total = b.at("total", default: false)
  // A short table stays on one page; a long one breaks between rows and repeats its header.
  block(width: 100%, above: 1.4em, below: 1.4em, breakable: not b.at("keep", default: false), {
    if opt(b, "caption") != none {
      block(below: 0.75em, sticky: true, text(size: 8.5pt, fill: muted, weight: "medium", b.caption))
    }
    set text(size: b.size * 1pt)
    set par(justify: false, leading: 0.58em)
    // Tabular figures only where quantities line up: the feature also widens hyphens in prose.
    let figures(j, body) = if b.align.at(j) == "right" { text(number-width: "tabular", body) } else { body }
    table(
      columns: b.widths.map(w => if w == "auto" { auto } else { w * 1fr }),
      inset: (x: if b.header.len() > 8 { 4.5pt } else { 8pt }, y: 6.6pt),
      align: (x, y) => aligns.at(x) + top,
      fill: (x, y) => if total and y == count { paper } else { none },
      stroke: (x, y) => if y == 0 { (bottom: 1pt + navy) }
        else if total and y == count { (top: 0.9pt + navy, bottom: 0.9pt + navy) }
        else if y == count { (bottom: 0.9pt + rule) }
        else { (bottom: 0.5pt + hairline) },
      table.header(..b.header.map(h => text(size: b.size * 0.85pt, weight: "semibold", fill: navy,
        tracking: 0.035em, upper(h)))),
      ..b.rows.enumerate().map(((i, row)) => row.enumerate().map(((j, cell)) => directed(cell-dir(i, j),
        figures(j, if total and i == count - 1 { text(weight: "semibold", fill: ink, cell) }
          else if j == 0 { text(fill: ink, cell) } else { cell })))).flatten(),
    )
  })
}

#let chart(b) = {
  let art = image(bytes(b.svg), format: "svg", width: 100%, alt: b.alt)
  block(width: 100%, above: 1.5em, below: 1.5em, breakable: false, {
    if opt(b, "title") != none {
      block(below: 0.8em, text(font: display-fonts, size: 10.5pt, weight: "semibold", fill: navy, b.title))
    }
    if opt(b, "caption") != none { figure(art, caption: b.caption) } else { art }
  })
}

#let pull-quote(b) = block(width: 100%, above: 1.5em, below: 1.5em,
  inset: if rtl-doc { (right: 18pt, left: 24pt, y: 4pt) } else { (left: 18pt, right: 24pt, y: 4pt) },
  stroke: start-stroke(2.4pt + blue), {
    set par(justify: false, leading: 0.7em)
    block(below: 0pt, directed(opt(b, "dir"), text(font: display-fonts, size: 13pt, weight: "medium", fill: navy,
      tracking: -0.006em, b.text)))
    if opt(b, "attribution") != none {
      block(above: 10pt, below: 0pt, text(size: 8.5pt, fill: muted, [\u{2014} #b.attribution]))
    }
  })

#let code-block(b) = block(width: 100%, above: 1.3em, below: 1.3em, fill: paper, stroke: 0.6pt + hairline,
  radius: 5pt, inset: (x: 12pt, y: 10pt), {
    set par(justify: false, leading: 0.62em)
    show raw: set text(size: b.size * 1pt)
    raw(b.text, block: true, lang: opt(b, "language"))
  })

#let steps(b) = block(width: 100%, above: 1.4em, below: 1.4em, inset: start-inset(10pt), {
  let last = b.items.len() - 1
  for (i, s) in b.items.enumerate() {
    block(width: 100%, above: 0pt, below: 0pt, breakable: false,
      inset: start-inset(22pt, bottom: if i == last { 0pt } else { 13pt }),
      stroke: start-stroke(1.2pt + (if i == last { white } else { rule })), {
        place(start-edge + top, dx: toward-end(-31.6pt), dy: -1.5pt, circle(radius: 9pt, fill: blue,
          stroke: 2pt + white,
          align(center + horizon, text(font: display-fonts, size: 8.5pt, weight: "bold", fill: white,
            str(i + 1)))))
        block(above: 0pt, below: 0pt, text(weight: "semibold", fill: navy, s.title))
        if opt(s, "text") != none {
          block(above: 5.5pt, below: 0pt, text(size: 9.4pt, s.text))
        }
      })
  }
})

#let comparison-card(c, height) = block(width: 100%, height: height, radius: 6pt, clip: true,
  stroke: 0.75pt + hairline, {
    block(width: 100%, fill: if c.first { blue-soft } else { paper }, inset: (x: 13pt, y: 9pt), below: 0pt,
      text(font: display-fonts, size: 10pt, weight: "semibold", fill: navy, c.title))
    block(width: 100%, inset: (x: 13pt, top: 11pt, bottom: 12pt), above: 0pt, {
      set text(size: 9.2pt)
      set par(justify: false)
      list(..c.items.map(item => [#item]))
    })
  })

#let comparison(b) = block(width: 100%, above: 1.4em, below: 1.4em, breakable: false,
  card-row(b.columns.enumerate().map(((i, c)) => (title: c.title, items: c.items, first: i == 0)), 10pt,
    comparison-card))

#let render(b) = {
  let t = b.t
  if t == "heading" {
    // The running header reads its section name from this marker: the shortened heading.
    if b.level == 1 { [#metadata(b.short) <section>] }
    // The direction is set before the heading, so its alignment and accent rule follow it too.
    directed(opt(b, "dir"), heading(level: b.level, b.text))
  }
  else if t == "paragraph" { directed(opt(b, "dir"), par(rich(b.runs))) }
  else if t == "list" {
    let items = b.items.map(item => directed(opt(item, "dir"), rich(item.runs)))
    if b.ordered { enum(..items) } else { list(..items) }
  }
  else if t == "table" { data-table(b) }
  else if t == "callout" { callout(b) }
  else if t == "metrics" { metrics(b) }
  else if t == "chart" { chart(b) }
  else if t == "quote" { pull-quote(b) }
  else if t == "code" { code-block(b) }
  else if t == "steps" { steps(b) }
  else if t == "comparison" { comparison(b) }
  else if t == "pagebreak" { pagebreak(weak: true) }
}

// --- front matter -------------------------------------------------------------------------
#let meta-line = data.footer
#let paper-name = if data.page == "letter" { "us-letter" } else { "a4" }

#if data.cover {
  page(paper: paper-name, margin: 0pt, header: none, footer: none, fill: navy, {
    cover-art(rtl-doc, navy, blue)
    place(top + start-edge, dx: toward-end(margin-x), dy: 26mm, {
      if opt(data, "organization") != none {
        block(width: 100% - 2 * margin-x, text(size: 8.5pt, weight: "semibold", fill: white.transparentize(18%),
          tracking: 0.14em, upper(data.organization)))
      }
    })
    place(horizon + start-edge, dx: toward-end(margin-x), dy: 14mm, block(width: 100% - 2 * margin-x - 18mm, {
      line(length: 34pt, stroke: (paint: blue.lighten(25%), thickness: 3pt, cap: "round"))
      v(13pt)
      set par(justify: false, leading: 0.5em)
      text(font: display-fonts, size: 37pt, weight: "bold", fill: white, tracking: -0.022em, hyphenate: false,
        data.title)
      if opt(data, "subtitle") != none {
        block(above: 20pt, {
          set par(leading: 0.62em)
          text(size: 14.5pt, fill: rgb("#c3d1ee"), hyphenate: false, data.subtitle)
        })
      }
    }))
    place(bottom + start-edge, dx: toward-end(margin-x), dy: -22mm, block(width: 100% - 2 * margin-x, {
      line(length: 100%, stroke: 0.6pt + white.transparentize(78%))
      v(10pt)
      set text(size: 9pt, fill: rgb("#c3d1ee"))
      grid(columns: (1fr, auto), column-gutter: 14pt, align: (start, end),
        (opt(data, "author"), opt(data, "organization")).filter(v => v != none).join([ #h(0.5em)\u{00B7}#h(0.5em) ]),
        opt(data, "date"))
    }))
  })
}

#if data.contents {
  page(paper: paper-name, margin: (top: 30mm, bottom: 25mm, x: margin-x), header: none, footer: none, {
    text(font: display-fonts, size: 24pt, weight: "bold", fill: navy, tracking: -0.015em, data.labels.contents)
    v(-0.2em)
    line(length: 30pt, stroke: (paint: blue, thickness: 2.4pt, cap: "round"))
    v(1.2em)
    set outline.entry(fill: repeat(text(fill: rule, [.]), gap: 0.34em))
    show outline.entry.where(level: 1): set block(above: 1.25em)
    show outline.entry.where(level: 1): set text(weight: "semibold", fill: navy)
    show outline.entry.where(level: 2): set text(size: 9.4pt)
    outline(title: none, depth: 2, indent: 1.4em)
  })
}

// --- body pages ---------------------------------------------------------------------------
#set page(
  paper: paper-name,
  margin: (top: 27mm, bottom: 25mm, x: margin-x),
  header: context {
    // The section this page continues, or the one that opens at its top.
    let page-number = here().page()
    let all = query(<section>)
    let earlier = all.filter(h => h.location().page() < page-number)
    let here-too = all.filter(h => h.location().page() == page-number)
    let opening = here-too.filter(h => h.location().position().y < 45mm)
    let section = if opening.len() > 0 { opening.first().value }
      else if earlier.len() > 0 { earlier.last().value }
      else if here-too.len() > 0 { here-too.first().value }
      else { none }
    set text(size: 7.6pt, fill: muted)
    grid(columns: (1fr, 1fr), column-gutter: 12pt, align: (start + bottom, end + bottom),
      text(weight: "medium", data.short-title), section)
    v(-5pt)
    line(length: 100%, stroke: 0.5pt + hairline)
  },
  footer: context {
    set text(size: 7.6pt, fill: muted, number-width: "tabular")
    line(length: 100%, stroke: 0.5pt + hairline)
    v(-4pt)
    grid(columns: (1fr, auto), column-gutter: 12pt, align: (start, end),
      meta-line.join([ #h(0.45em)\u{00B7}#h(0.45em) ]),
      [#data.labels.page #counter(page).display() #data.labels.of #counter(page).final().first()])
  },
)
#counter(page).update(1)

#if not data.cover {
  // No cover: the document opens with its title.
  block(below: 1.5em, {
    set par(justify: false, leading: 0.52em)
    text(font: display-fonts, size: 26pt, weight: "bold", fill: navy, tracking: -0.02em, hyphenate: false,
      data.title)
    if opt(data, "subtitle") != none {
      v(9pt, weak: true)
      set par(leading: 0.62em)
      text(size: 12.5pt, fill: muted, hyphenate: false, data.subtitle)
    }
    v(14pt, weak: true)
    line(length: 100%, stroke: 0.8pt + rule)
  })
}

#if opt(data, "summary") != none {
  block(width: 100%, above: 0.4em, below: 1.7em, inset: start-inset(15pt, y: 2pt),
    stroke: start-stroke(2.4pt + blue), {
    text(size: 7.6pt, weight: "semibold", fill: blue-deep, tracking: 0.1em, upper(data.labels.summary))
    v(0.6em, weak: true)
    set par(justify: false, leading: 0.72em)
    text(size: 11.4pt, fill: ink, data.summary)
  })
}

#for b in data.blocks { render(b) }
