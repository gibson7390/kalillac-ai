// One chart, rasterised for formats that need an image. The SVG arrives as data.
#set page(width: float(sys.inputs.width) * 1pt, height: float(sys.inputs.height) * 1pt, margin: 0pt, fill: white)
#image(bytes(sys.inputs.svg), format: "svg", width: 100%)
