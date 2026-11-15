// Cover artwork shared by the PDF cover and the Word cover background.
// A single calm gesture: one large disc of light leaving the top corner, with two hairline orbits.
#let cover-art(rtl, navy, blue) = {
  let far = if rtl { left } else { right }
  let away(v) = if rtl { -v } else { v }
  place(top + far, dx: away(62mm), dy: -78mm, circle(radius: 122mm,
    fill: gradient.radial(blue.transparentize(22%), blue.transparentize(100%),
      center: (if rtl { 60% } else { 40% }, 60%), radius: 62%)))
  place(top + far, dx: away(30mm), dy: -44mm, circle(radius: 74mm, stroke: 0.6pt + white.transparentize(82%)))
  place(top + far, dx: away(46mm), dy: -60mm, circle(radius: 100mm, stroke: 0.6pt + white.transparentize(90%)))
}
