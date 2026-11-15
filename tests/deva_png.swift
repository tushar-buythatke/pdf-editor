// Renders one line of text to a transparent PNG with CoreText shaping (correct Devanagari matras).
// usage: swift deva_png.swift "text" "PostScriptFontName" pointSize out.png [r g b]
import AppKit
import CoreText

let a = CommandLine.arguments
guard a.count >= 5 else { FileHandle.standardError.write("usage: text fontName size out.png\n".data(using: .utf8)!); exit(2) }
let text = a[1]
let fontName = a[2]
let size = CGFloat(Double(a[3]) ?? 40)
let outPath = a[4]
let rgb: [CGFloat] = a.count >= 8 ? [CGFloat(Double(a[5]) ?? 0), CGFloat(Double(a[6]) ?? 0), CGFloat(Double(a[7]) ?? 0)] : [0, 0, 0]

guard let font = NSFont(name: fontName, size: size) else {
    FileHandle.standardError.write("font not found: \(fontName)\n".data(using: .utf8)!); exit(3)
}
let color = NSColor(srgbRed: rgb[0], green: rgb[1], blue: rgb[2], alpha: 1)
let s = NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: color])
let sz = s.size()
let w = max(Int(ceil(sz.width)) + 8, 1), h = max(Int(ceil(sz.height)) + 8, 1)
guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8,
                                 samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                 colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(4) }
rep.size = NSSize(width: w, height: h)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
s.draw(at: NSPoint(x: 4, y: 4))
NSGraphicsContext.restoreGraphicsState()
guard let png = rep.representation(using: .png, properties: [:]) else { exit(5) }
try png.write(to: URL(fileURLWithPath: outPath))
print("\(w)x\(h)")
