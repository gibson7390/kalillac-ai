// The cover artwork alone, as an image for formats that place editable text over it.
#import "art.typ": cover-art
#let navy = rgb("#0b1737")
#let blue = rgb("#0069ff")
#set page(paper: if sys.inputs.page == "letter" { "us-letter" } else { "a4" }, margin: 0pt, fill: navy)
#cover-art(sys.inputs.rtl == "true", navy, blue)
