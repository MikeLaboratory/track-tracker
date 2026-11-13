// Disegna l'icona dell'app (una piccola board con le colonne degli stati) in un PNG 1024×1024.
// Uso: swift Scripts/make-icon.swift Resources/AppIcon.png
import AppKit

let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.png"
let size = 1024
guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                 colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

func rgb(_ hex: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255,
            blue: CGFloat(hex & 255) / 255, alpha: a)
}

// sfondo: quadrato arrotondato secondo la griglia delle icone macOS (824 px con margine 100)
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let tilePath = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
NSGraphicsContext.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = rgb(0x000000, 0.45)
shadow.shadowOffset = NSSize(width: 0, height: -12)
shadow.shadowBlurRadius = 28
shadow.set()
rgb(0x1c1c1f).setFill()
tilePath.fill()
NSGraphicsContext.restoreGraphicsState()
NSGradient(starting: rgb(0x2a2a2f), ending: rgb(0x141416))!.draw(in: tilePath, angle: -90)

// tre colonne con le card, nei colori degli stati dell'app
let columns: [(color: Int, cards: Int)] = [(0x0a84ff, 3), (0xff9f0a, 2), (0x30d158, 4)]
let colW: CGFloat = 196, gap: CGFloat = 38
let left = tile.midX - (colW * 3 + gap * 2) / 2
let top = tile.maxY - 150
for (i, col) in columns.enumerated() {
    let x = left + CGFloat(i) * (colW + gap)
    let colRect = NSRect(x: x, y: tile.minY + 130, width: colW, height: top - tile.minY - 130)
    rgb(0xffffff, 0.06).setFill()
    NSBezierPath(roundedRect: colRect, xRadius: 30, yRadius: 30).fill()
    rgb(col.color).setFill()
    NSBezierPath(roundedRect: NSRect(x: x, y: top - 18, width: colW, height: 18), xRadius: 9, yRadius: 9).fill()
    for c in 0..<col.cards {
        let cardY = top - 56 - CGFloat(c + 1) * 112
        let card = NSRect(x: x + 18, y: cardY, width: colW - 36, height: 92)
        rgb(0x3a3a40).setFill()
        NSBezierPath(roundedRect: card, xRadius: 18, yRadius: 18).fill()
        rgb(col.color).setFill()
        NSBezierPath(roundedRect: NSRect(x: card.minX, y: card.minY, width: 14, height: card.height),
                     xRadius: 7, yRadius: 7).fill()
        rgb(0xececf0, 0.75).setFill()
        NSBezierPath(roundedRect: NSRect(x: card.minX + 32, y: card.midY + 6, width: card.width - 60, height: 16),
                     xRadius: 8, yRadius: 8).fill()
        rgb(0x8d8d96, 0.7).setFill()
        NSBezierPath(roundedRect: NSRect(x: card.minX + 32, y: card.midY - 24, width: (card.width - 60) * 0.6, height: 14),
                     xRadius: 7, yRadius: 7).fill()
    }
}

NSGraphicsContext.restoreGraphicsState()
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: out))
print("Icona disegnata: \(out)")
