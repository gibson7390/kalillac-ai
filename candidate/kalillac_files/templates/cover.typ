// The cover artwork alone, as an image for formats that place editable text over it.
#import "art.typ": cover-art
#let navy = rgb("#0b1737")
#let blue = rgb("#0069ff")
#let rtl = sys.inputs.rtl == "true"
#if sys.inputs.page == "wide" {
  // 16:9 slides: the same gesture, scaled to the wider, shorter page, and mirrored for right-to-left
  // languages so the text column (on the reading side) stays clear of it.
  set page(width: 338.67mm, height: 190.5mm, margin: 0pt, fill: navy)
  let side = if rtl { top + left } else { top + right }
  let x(offset) = if rtl { -offset } else { offset }
  place(side, dx: x(92mm), dy: -118mm, circle(radius: 150mm,
    fill: gradient.radial(blue.transparentize(25%), blue.transparentize(100%),
      center: (if rtl { 62% } else { 38% }, 62%), radius: 62%)))
  place(side, dx: x(58mm), dy: -84mm, circle(radius: 98mm, stroke: 0.6pt + white.transparentize(84%)))
  place(side, dx: x(76mm), dy: -102mm, circle(radius: 126mm, stroke: 0.6pt + white.transparentize(91%)))
} else {
  set page(paper: if sys.inputs.page == "letter" { "us-letter" } else { "a4" }, margin: 0pt, fill: navy)
  cover-art(rtl, navy, blue)
}
