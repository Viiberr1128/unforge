import AppKit

// Vector source keeps the app icon reproducible without external assets.
let destination = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels,
                                      pixelsHigh: pixels, bitsPerSample: 8,
                                      samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                      colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
        let context = NSGraphicsContext.current!.cgContext
        context.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
        let backdrop = NSBezierPath(roundedRect: NSRect(x: 50, y: 50, width: 924, height: 924),
                                   xRadius: 205, yRadius: 205)
        NSColor(calibratedRed: 0.16, green: 0.27, blue: 0.23, alpha: 1).setFill()
        backdrop.fill()
        let mark = NSBezierPath()
        mark.lineWidth = 96
        mark.lineCapStyle = .round
        mark.move(to: NSPoint(x: 315, y: 707))
        mark.line(to: NSPoint(x: 315, y: 455))
        mark.curve(to: NSPoint(x: 709, y: 455), controlPoint1: NSPoint(x: 315, y: 235),
                   controlPoint2: NSPoint(x: 709, y: 235))
        mark.line(to: NSPoint(x: 709, y: 600))
        NSColor(calibratedRed: 0.97, green: 0.94, blue: 0.86, alpha: 1).setStroke()
        mark.stroke()
        NSColor(calibratedRed: 0.83, green: 0.74, blue: 0.48, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: 657, y: 682, width: 104, height: 104)).fill()
        NSGraphicsContext.restoreGraphicsState()
        let suffix = scale == 2 ? "@2x" : ""
        let file = destination.appendingPathComponent("icon_\(size)x\(size)\(suffix).png")
        try bitmap.representation(using: .png, properties: [:])!.write(to: file)
    }
}
